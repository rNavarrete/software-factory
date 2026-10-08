"""Approve-if-needed and fire one attempt of one task (ENG-176).

``Dispatcher.dispatch(contract)`` is what ``factory dispatch <contract.json>``
runs. It is deterministic: no model calls, and every decision is read from the
ledger at the moment it is made. In order:

1. ``Recovery.recover``: a fire left with no response by a controller that
   stopped mid-launch becomes launch-outcome-unknown. Nothing is re-fired.
2. The contract must be well formed, name the pilot repo, and have a base
   commit that is on the pilot's ``main`` (read from GitHub).
3. A task that already has an attempt for this contract that is still going
   (dispatching, unknown, running, a PR open, waiting on Rolando, merged) is
   not fired again: its recorded state is returned. That makes a repeated
   ``dispatch`` safe.
4. Anything Rolando can't settle by typing a code here (a hold, a rate-limit
   wait, the single lane, a cap, a withdrawn approval) refuses now, before he
   is asked anything. Then, if he hasn't approved this exact contract, he is
   shown it and types the code (the same signed approval as ``factory
   approve``); if its last fire was definitely not launched (a 429, say), he
   signs the re-fire the same way.
5. ``Launcher.fire``: approvals, recovery and the attempt gate are checked;
   the fire text and start key are prepared; the gate reserves the run under
   the ledger's writer lock, re-running the approval and recovery checks
   inside that same lock (so nothing can change between check and reserve);
   the run context is written; one POST is made; its result is recorded
   (launch-outcome-unknown for anything ambiguous, including Ctrl-C); the
   ledger is backed up.

A launched session is not a finished task. It only means a worker started;
the task's outcome is the marker branch and PR on GitHub, which ``reconcile``
reads.

The start key is read from Keychain only after every check has passed and is
never written anywhere. The worker gets the fire text, which holds the
contract and markers and nothing else, and it never has a path to the ledger,
the Keychain or the operator key that signs decisions.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from controller import contract as contracts
from controller.adapter import routine
from controller.approval import ApprovalRefused, Approvals
from controller.attempts import AttemptGate
from controller.attempts import DispatchRefused as GateRefused
from controller.attempts.events import DISPATCH_REFUSED
from controller.attempts.policy import Decision, LedgerView, Notice
from controller.dispatch.base import BaseCheck, BaseUnreadable
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchRequest,
    LaunchResult,
    LedgerEvent,
    LedgerLocked,
    LedgerStore,
    RunId,
    RuntimeAdapter,
    TaskId,
)
from controller.ledger import records
from controller.recovery import PILOT_REPO, PROTECTED_BRANCH, Recovery
from controller.recovery import state as st

FACTORY_ROUTINE = "trig_01CHWbQ267i1CMLGUym1kGd9"
"""The ``factory-worker`` routine on the factory account (created 2026-10-08).
It is bound to the pilot repo only."""

RUNTIME = "cloud-routine"
PROMPT_FILE = Path(__file__).resolve().parent.parent / "adapter" / "routine_prompt.md"

_PROMPT_SEPARATOR = b"\n---\n\n"
_LOCK_TRIES = 50
_LOCK_WAIT_SECONDS = 0.2

# Approval blocks Rolando can settle by approving the contract in front of him.
_APPROVABLE = frozenset({"approval-missing", "approval-expired", "approval-for-different-contract"})

# What dispatch asks Rolando for itself, at the terminal.
_ASKABLE = _APPROVABLE | {"refire-not-authorized"}

# Attempt states where a repeated dispatch returns what is on record.
_GOING = frozenset(
    {
        st.State.DISPATCHING,
        st.State.UNKNOWN,
        st.State.RUNNING,
        st.State.VERIFYING,
        st.State.AWAITING_HUMAN,
        st.State.MERGED,
    }
)


class Refused(Exception):
    """Nothing was sent. ``blocks`` says why, one notice per reason."""

    def __init__(
        self,
        blocks: Sequence[Notice],
        *,
        recorded: bool = False,
        decision: Decision | None = None,
    ) -> None:
        self.blocks = tuple(blocks)
        self.recorded = recorded
        """The refusal is already in the ledger (the gate wrote it), or can't be."""
        self.decision = decision
        """The gate's decision behind it, when the gate decided it, so a cap
        refusal is recorded with its one escalation (G-C5)."""
        super().__init__("; ".join(f"{b.code}: {b.detail}" for b in self.blocks))


def record_refusal(
    store: LedgerStore,
    gate: AttemptGate,
    now: datetime,
    contract: Mapping[str, object],
    e: Refused,
) -> None:
    """Put a refusal raised before the gate in the ledger (G-A1); one the gate
    already recorded is left alone. If it can't be recorded, the refusal still
    stands and says so."""
    if e.recorded:
        return
    e.recorded = True  # once, however many wrappers it passes through
    try:
        if e.decision is not None:
            gate.record_refusal(e.decision, now, before_gate=True)
            return
        task_id = contract.get("task_id") if isinstance(contract, Mapping) else None
        try:
            task = TaskId(task_id) if isinstance(task_id, str) and task_id else None
        except ValueError:
            task = None
        event = LedgerEvent(
            DISPATCH_REFUSED,
            now,
            task,
            data={"blocks": [b.as_data() for b in e.blocks], "alerts": [], "before_gate": True},
        )
        with store.writer_lock():
            store.append(event)
    except Exception as err:  # the refusal itself must still reach Rolando
        e.blocks += (Notice("not-recorded", f"this refusal could not be recorded: {err}"),)


@dataclass(frozen=True)
class Fired:
    run: RunId
    result: LaunchResult
    late: bool = False
    """The answer came after recovery had marked the run unknown; the run stays
    recorded as launch-outcome-unknown."""


Prepare = Callable[[RunId, ContractDigest], Callable[[], LaunchResult]]
"""Given the run about to be reserved, build everything the fire needs (text,
start key) and return the zero-argument call that sends it. Called after the
checks and before the reservation, so a failure here uses up nothing."""


def prompt_revision(path: Path = PROMPT_FILE) -> str:
    """sha256 of the routine's saved prompt: everything below its ``---`` line."""
    text = path.read_bytes()
    if _PROMPT_SEPARATOR not in text:
        raise ValueError(f"{path} has no '---' line")
    return hashlib.sha256(text.split(_PROMPT_SEPARATOR, 1)[1]).hexdigest()


class Launcher:
    """The one fire path. ``Dispatcher`` and the qualification script both use it,
    so no fire can skip a check or go unrecorded."""

    def __init__(
        self,
        store: LedgerStore,
        approvals: Approvals,
        recovery: Recovery,
        gate: AttemptGate,
        *,
        model_config_version: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        backup: Callable[[datetime], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        warn: Callable[[str], None] = lambda text: print(text, file=sys.stderr),
    ) -> None:
        if not model_config_version.strip():
            raise ValueError("say which routine and prompt revision fires")
        self._store = store
        self._approvals = approvals
        self._recovery = recovery
        self._gate = gate
        self._config = model_config_version
        self._now = now
        self._backup = backup
        self._sleep = sleep
        self._warn = warn

    def check(
        self, contract: Mapping[str, object], now: datetime, refire_of: RunId | None = None
    ) -> tuple[RunId | None, ContractDigest | None, tuple[Notice, ...]]:
        """Recovery's blocks plus Rolando's decisions, read from the ledger now."""
        verdict = self._approvals.check(contract, now, refire_of=refire_of)
        automatic = (
            verdict.run is not None
            and self._approvals.automatic_repair(contract, verdict.run.attempt, now) is not None
        )
        blocks = list(self._recovery.blocks(now, automatic_repair=automatic))
        blocks += verdict.blocks
        if verdict.run is None and not blocks:
            blocks.append(Notice("no-run", "There is no attempt to fire."))
        return verdict.run, verdict.digest, tuple(blocks)

    def fire(
        self,
        contract: Mapping[str, object],
        prepare: Prepare,
        *,
        refire_of: RunId | None = None,
    ) -> Fired:
        """One fire, in order: recover, check, prepare, reserve (checks again
        under the lock), record the run context, send, record the result,
        back up. Raises Refused if nothing was sent, with the refusal in the
        ledger (G-A1)."""
        try:
            return self._fire(contract, prepare, refire_of=refire_of)
        except Refused as e:
            record_refusal(self._store, self._gate, self._now(), contract, e)
            raise

    def _fire(
        self,
        contract: Mapping[str, object],
        prepare: Prepare,
        *,
        refire_of: RunId | None = None,
    ) -> Fired:
        now = self._now()
        self._recovery.recover(now)
        run, digest, blocks = self.check(contract, now, refire_of)
        if blocks:
            raise Refused(blocks)
        assert run is not None and digest is not None
        frozen = contracts.freeze(contract)
        send = prepare(run, digest)

        now = self._now()

        def still_allowed(reserved: RunId) -> list[Notice]:
            again, _, blocks = self.check(frozen, now, refire_of)
            out = list(blocks)
            if reserved != run or again != run:
                out.append(
                    Notice(
                        "run-mismatch",
                        f"The fire was prepared for {run}, but the ledger now says {reserved}.",
                    )
                )
            return out

        def context(reserved: RunId) -> list[LedgerEvent]:
            return [
                records.run_context(
                    reserved,
                    digest,
                    str(frozen["version"]),
                    str(frozen["base_commit"]),
                    RUNTIME,
                    self._config,
                    int(frozen["attempt_budget"]),  # type: ignore[call-overload]
                    now,
                )
            ]

        try:
            reserved = self._gate.reserve(
                run.attempt.task,
                digest,
                now,
                refire_of=refire_of,
                precondition=still_allowed,
                with_intent=context,
            )
        except GateRefused as e:
            raise Refused(e.decision.blocks, recorded=True) from None
        except LedgerLocked:
            raise Refused(
                [Notice("ledger-busy", "Another controller command is writing; try again.")],
                recorded=True,  # not recorded: another command holds the lock
            ) from None
        assert reserved == run

        try:
            result = send()
        except routine.LaunchInterrupted as e:
            self._record(run, e.result)
            raise
        except BaseException as e:
            # Sent or not, nobody can say: a session may exist.
            self._record(
                run,
                LaunchResult(
                    LaunchOutcome.OUTCOME_UNKNOWN,
                    detail=f"stopped during the fire: {type(e).__name__}",
                ),
            )
            raise
        late = self._record(run, result)
        self.backup()
        return Fired(run, result, late)

    def backup(self) -> None:
        """Back up the ledger. A failed backup is reported, never fatal: what it
        would copy is already safely in the ledger."""
        if self._backup is None:
            return
        try:
            self._backup(self._now())
        except Exception as e:
            self._warn(f"Warning: the ledger backup failed ({type(e).__name__}: {e}).")

    def _record(self, run: RunId, result: LaunchResult) -> bool:
        """Record ``result``; True if it arrived after recovery had already marked
        the run unknown. If it can't be recorded at all, it is printed, so a
        session URL is never lost with it."""
        late = False

        def record() -> None:
            nonlocal late
            now = self._now()
            try:
                self._gate.record_launch(run, result, now)
            except ValueError:
                # Recovery already marked it unknown; keep the late answer beside it.
                self._recovery.record_late_result(run, result, now)
                late = True

        try:
            for i in range(_LOCK_TRIES):
                try:
                    record()
                    return late
                except LedgerLocked:
                    if i == _LOCK_TRIES - 1:
                        raise
                    self._sleep(_LOCK_WAIT_SECONDS)
        except BaseException:
            self._warn(
                f"The answer to {run} could NOT be recorded: {result.outcome.value},"
                f" HTTP {result.http_status}, session {result.session_url or 'none'}"
                f" ({result.detail or 'no detail'}). Keep this; recovery will mark the run"
                " unknown and the session URL is needed to clear it."
            )
            raise
        return late


@dataclass(frozen=True)
class DispatchResult:
    outcome: str
    """launched, not-launched, launch-outcome-unknown or already-dispatched."""
    attempt: AttemptId
    run: RunId | None
    """The run fired now; None when nothing was fired."""
    launch: LaunchResult | None
    status: st.AttemptStatus | None
    """The attempt as recorded after this command."""
    message: str
    """What happened and what to do next, in plain words."""


AdapterFactory = Callable[[str, Callable[[str], str]], RuntimeAdapter]


def _routine_adapter(trig_id: str, start_key: Callable[[str], str]) -> RuntimeAdapter:
    return routine.RoutineAdapter(trig_id, start_key=start_key)


class Dispatcher:
    def __init__(
        self,
        store: LedgerStore,
        approvals: Approvals,
        recovery: Recovery,
        gate: AttemptGate,
        base_check: BaseCheck,
        *,
        routine_id: str = FACTORY_ROUTINE,
        repo: str = PILOT_REPO,
        base_branch: str = PROTECTED_BRANCH,
        adapter: AdapterFactory = _routine_adapter,
        start_key: Callable[[str], str] = routine.keychain_key,
        model_config_version: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        backup: Callable[[datetime], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._approvals = approvals
        self._recovery = recovery
        self._gate = gate
        self._base = base_check
        self._routine = routine_id
        self._repo = repo
        self._branch = base_branch
        self._adapter = adapter
        self._start_key = start_key
        self._now = now
        config = model_config_version or (
            f"routine {routine_id}; prompt sha256 {prompt_revision()}"
        )
        self.launcher = Launcher(
            store,
            approvals,
            recovery,
            gate,
            model_config_version=config,
            now=now,
            backup=backup,
            sleep=sleep,
            **({} if warn is None else {"warn": warn}),
        )

    @property
    def approvals(self) -> Approvals:
        return self._approvals

    def dispatch(self, contract: Mapping[str, object]) -> DispatchResult:
        """Fire the next attempt of ``contract``'s task, or report the one on record.

        Every refusal is in the ledger (governance map G-A1): the gate records
        its own, and one raised before the gate (no or declined approval, a
        wrong target, a hold seen in the preview) is recorded here."""
        try:
            return self._dispatch(contract)
        except Refused as e:
            record_refusal(self._store, self._gate, self._now(), contract, e)
            raise

    def _dispatch(self, contract: Mapping[str, object]) -> DispatchResult:
        now = self._now()
        self._recovery.recover(now)
        errors = contracts.approval_errors(contract)
        if errors:
            raise Refused([Notice("contract-invalid", "; ".join(errors))])
        contract = contracts.freeze(contract)
        digest = contracts.digest(contract)
        task = TaskId(str(contract["task_id"]))

        refire_of = None
        latest = self._latest(task)
        if latest is not None:
            status = self._recovery.attempt_status(latest.attempt, now)
            last = latest.last_fire
            not_launched = (
                last is not None
                and last.outcome is LaunchOutcome.NOT_LAUNCHED
                and status.state is st.State.AWAITING_HUMAN
                and not status.pull_requests
            )
            # A signed go-ahead for the next attempt means Rolando has moved on
            # from this one, whatever state it was left in.
            next_attempt = AttemptId(task, latest.attempt.number + 1)
            trusted = self._approvals.trusted_events(self._store.events(), digest, now=now)
            repaired = next_attempt in LedgerView.build(trusted).repairs
            moved_on = repaired and status.state is not st.State.MERGED
            if status.state in _GOING and not not_launched and not moved_on:
                return self._on_record(status, digest)
            if moved_on:
                not_launched = False
            if not_launched:
                assert last is not None
                if latest.digest != digest.value:
                    raise Refused(
                        [
                            Notice(
                                "contract-changed",
                                f"{latest.attempt} was not launched with an earlier version of"
                                " this contract. Close it, then approve and dispatch the new"
                                " version as a new attempt.",
                            )
                        ]
                    )
                refire_of = last.run

        self._check_target(contract)
        self._preview(contract, digest, now, refire_of)
        self._approve_if_needed(contract, now, refire_of)
        if refire_of is not None:
            self._authorize_refire(contract, digest, refire_of)

        def prepare(run: RunId, digest: ContractDigest) -> Callable[[], LaunchResult]:
            text = routine.build_fire_text(
                contract, digest, run.attempt, repair=self._repair_brief(contract, run.attempt)
            )
            key = self._start_key(self._routine)
            adapter = self._adapter(self._routine, lambda _trig: key)
            request = LaunchRequest(run, digest, text)
            return lambda: adapter.launch(request)

        fired = self.launcher.fire(contract, prepare, refire_of=refire_of)
        status = self._recovery.attempt_status(fired.run.attempt, self._now())
        return DispatchResult(
            fired.result.outcome.value,
            fired.run.attempt,
            fired.run,
            fired.result,
            status,
            _fired_message(fired),
        )

    # --- steps ----------------------------------------------------------------

    def _check_target(self, contract: Mapping[str, object]) -> None:
        if contract["repository"] != self._repo:
            raise Refused(
                [
                    Notice(
                        "wrong-repository",
                        f"The contract is for {contract['repository']}; the factory worker"
                        f" only works on {self._repo}.",
                    )
                ]
            )
        base = str(contract["base_commit"])
        try:
            on_branch = self._base.on_branch(self._repo, base, self._branch)
        except BaseUnreadable as e:
            raise Refused(
                [Notice("base-unreadable", f"Could not check the base commit: {e}")]
            ) from None
        if not on_branch:
            raise Refused(
                [
                    Notice(
                        "base-not-on-main",
                        f"Base commit {base} is not on {self._repo}'s {self._branch} branch.",
                    )
                ]
            )

    def _latest(self, task: TaskId):
        attempts = LedgerView.build(self._store.events(task)).task_attempts(task)
        return attempts[-1] if attempts else None

    def _on_record(self, status: st.AttemptStatus, digest: ContractDigest) -> DispatchResult:
        note = ""
        if status.digest != digest.value:
            note = (
                " That attempt runs a different version of this contract; this version"
                " was not fired."
            )
        return DispatchResult(
            "already-dispatched",
            status.attempt,
            None,
            None,
            status,
            f"Nothing sent: {status.attempt} is already {status.state.value}"
            f" ({status.detail}).{note} Next: {status.next_step}",
        )

    def _preview(
        self,
        contract: Mapping[str, object],
        digest: ContractDigest,
        now: datetime,
        refire_of: RunId | None,
    ) -> None:
        """Refuse before asking Rolando anything if something he can't settle by
        typing a code here would stop the fire anyway: a hold, a rate-limit
        wait, the single lane, a cap, a missing repair go-ahead, a withdrawn
        approval."""
        task = TaskId(str(contract["task_id"]))
        decision = self._gate.decide(task, digest, now, refire_of=refire_of)
        _, _, checked = self.launcher.check(contract, now, refire_of)
        blocks: list[Notice] = []
        for b in (*decision.blocks, *checked):
            if b.code not in _ASKABLE and b not in blocks:
                blocks.append(b)
        if blocks:
            raise Refused(blocks, decision=replace(decision, blocks=tuple(blocks)))

    def _approve_if_needed(
        self, contract: Mapping[str, object], now: datetime, refire_of: RunId | None
    ) -> None:
        verdict = self._approvals.check(contract, now, refire_of=refire_of)
        if not {b.code for b in verdict.blocks} & _APPROVABLE:
            return
        try:
            self._approvals.approve(contract, now)
        except ApprovalRefused as e:
            raise Refused([Notice("not-approved", str(e))]) from None

    def _authorize_refire(
        self, contract: Mapping[str, object], digest: ContractDigest, prior: RunId
    ) -> None:
        trusted = self._approvals.trusted_events(self._store.events(), digest)
        if prior in LedgerView.build(trusted).refires:
            return
        try:
            self._approvals.authorize_refire(contract, prior, self._now())
        except ApprovalRefused as e:
            raise Refused([Notice("refire-not-authorized", str(e))]) from None

    def _repair_brief(
        self, contract: Mapping[str, object], attempt: AttemptId
    ) -> Mapping[str, object] | None:
        """For an automatic repair (ENG-160): what failed on the attempt before,
        from the signed repair record that lets this attempt start. A repair
        Rolando allowed by hand fires with the contract alone, as before."""
        if attempt.number < 2:
            return None
        record = self._approvals.automatic_repair(contract, attempt, self._now())
        if record is None:
            return None
        prior = AttemptId(attempt.task, attempt.number - 1)
        return routine.repair_brief(
            prior,
            int(str(record["prior_pr"])),
            str(record["prior_head"]),
            record["findings"],
        )

    # --- other commands --------------------------------------------------------

    def reconcile(self, attempt: AttemptId):
        """Read the attempt's branch and PRs from GitHub, record them, back up."""
        found = self._recovery.reconcile(attempt, self._now())
        if found.recorded:
            self.launcher.backup()
        return found


def _fired_message(fired: Fired) -> str:
    r = fired.result
    if fired.late:
        return (
            f"{fired.run} answered late ({r.outcome.value}, session {r.session_url or 'none'})"
            " after the controller had already marked it unknown. Its session is on record;"
            " the single lane stays held until you check it and record a clearing."
        )
    if r.outcome is LaunchOutcome.LAUNCHED:
        return (
            f"Started {fired.run}: {r.session_url}. A worker is running; this is not a"
            " finished task. Its draft PR will appear on the pilot repo."
        )
    if r.outcome is LaunchOutcome.NOT_LAUNCHED:
        wait = ""
        if r.http_status == 429:
            wait = " The routine is rate limited; dispatch again later and it will wait or go."
        return f"Not started (HTTP {r.http_status}): no worker is running.{wait}"
    return (
        f"Unclear whether {fired.run} started ({r.detail or 'no usable response'})."
        " Nothing will be sent again for it. Look for the session in the routine's run list,"
        " then reconcile."
    )


__all__ = [
    "FACTORY_ROUTINE",
    "DispatchResult",
    "Dispatcher",
    "Fired",
    "Launcher",
    "Prepare",
    "Refused",
    "prompt_revision",
]
