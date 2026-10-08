"""Attempts to get past Rolando's approval records (ENG-158, approval part).

Each scenario builds a fresh durable ledger (the real SQLite store, in a
temporary folder), records what Rolando honestly decided, then either tampers
with the records (``attack=True``) or does the honest thing instead
(``attack=False``). It returns what the approval check at dispatch said.

The honest version of every scenario must be approved, so a blocked attack
shows the approval code stopped that one change and isn't refusing everything.

"The key" below is a stand-in operator key made up for these fixtures; the
real one never leaves Rolando's Keychain. A scenario that signs a bad record
with it plays someone who got hold of the key or a bug in the writer, to show
which rules still hold then. Offline only: nothing here reaches the network,
the pilot repo or any Claude account.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval import (
    APPROVED,
    HUMAN_DECISION,
    MAX_TTL,
    SCOPE,
    ApprovalRefused,
    Approvals,
    StaticKey,
    Verdict,
)
from controller.approval import approval as approval_module
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.interfaces import (
    AttemptId,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    RunId,
    TaskId,
)
from controller.ledger import SqliteLedgerStore
from redteam import fixtures as fx

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"redteam-operator-key-fixture-0001")
"""A made-up stand-in for Rolando's operator key."""
OTHER_KEY = StaticKey(b"redteam-some-other-key-fixture-02")
OLD_KEY = StaticKey(b"redteam-retired-key-fixture-00003")
WORKER = fx.WORKER
TASK = TaskId(str(fx.CONTRACT["task_id"]))


def plain_contract(**changes: object) -> dict[str, object]:
    """A plain, editable copy of the honest contract."""
    contract = json.loads(fx.EXAMPLE.read_text())
    contract.update(changes)
    return contract


def wider_contract() -> dict[str, object]:
    """The contract edited to also permit the release workflow: a new digest."""
    paths = [*fx.CONTRACT["permitted_paths"], ".github/workflows/release.yml"]
    return plain_contract(permitted_paths=paths)


def launched(n: int = 1) -> LaunchResult:
    return LaunchResult(
        LaunchOutcome.LAUNCHED,
        http_status=200,
        session_id=f"cse_redteam{n}",
        session_url=f"https://claude.ai/code/cse_redteam{n}",
    )


NOT_LAUNCHED = LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=400)
LOST = LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, detail="timeout")


def _typed(summary: str, code: str) -> bool:
    """Rolando typing the code at his terminal."""
    return True


def _not_typed(summary: str, code: str) -> bool:
    """Nobody typed the code (a worker, a script, a closed terminal)."""
    return False


@dataclass(frozen=True)
class Outcome:
    approved: bool
    codes: tuple[str, ...]
    detail: str

    @property
    def text(self) -> str:
        return "\n".join((*self.codes, self.detail))


def verdict_outcome(verdict: Verdict) -> Outcome:
    codes = tuple(b.code for b in verdict.blocks)
    first = verdict.blocks[0].detail if verdict.blocks else f"approved to fire {verdict.run}"
    return Outcome(verdict.approved, codes, first)


class Ledger:
    """A fresh durable ledger with Rolando's desk and the attempt gate."""

    def __init__(self, **desk: object) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="redteam-ledger-")
        self.store = SqliteLedgerStore(Path(self._tmp.name) / "ledger.db")
        self.desk = Approvals(self.store, KEY, confirm=_typed, os_user="rolando", **desk)
        self.gate = AttemptGate(self.store)

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.store.close()
        self._tmp.cleanup()

    def append(self, *events: LedgerEvent) -> None:
        with self.store.writer_lock():
            self.store.append(*events)

    def last(self, kind: str) -> LedgerEvent:
        return [s.event for s in self.store.events() if s.event.kind == kind][-1]

    def count(self) -> int:
        return len(self.store.events())

    def check(self, contract=fx.CONTRACT, at=NOW, refire_of=None) -> Outcome:
        return verdict_outcome(self.desk.check(contract, at, refire_of=refire_of))

    def dispatch(self, contract=fx.CONTRACT, result=None, at=NOW, refire_of=None) -> RunId:
        """An honest dispatch, used to set a scenario up: check, reserve, record."""
        self.gate.record_snapshot(at, 10, 20, 0, at)
        verdict = self.desk.check(contract, at, refire_of=refire_of)
        if not verdict.approved:
            raise AssertionError(f"scenario setup was refused: {verdict.blocks}")
        digest = contracts.digest(contract)
        run = self.gate.reserve(TaskId(str(contract["task_id"])), digest, at, refire_of=refire_of)
        if run != verdict.run:
            raise AssertionError(f"reserve gave {run}, check said {verdict.run}")
        self.gate.record_launch(run, result or launched(run.attempt.number), at)
        return run

    def finish(self, attempt: AttemptId, at=NOW) -> None:
        url = f"https://claude.ai/code/cse_redteam{attempt.number}"
        self.desk.record_clearing(attempt, ev.ClearingBasis.COMPLETED, url, at)


def resign(event: LedgerEvent, key: StaticKey = KEY, **changes: object) -> LedgerEvent:
    """Sign a copy of ``event`` with changes, as someone holding ``key`` would."""
    signature = ("mac", "key_id", "decision_id", "binding_sha256")
    data = {k: v for k, v in event.data.items() if k not in signature}
    base = LedgerEvent(
        event.kind,
        changes.pop("at", event.at),
        event.task,
        changes.pop("attempt", event.attempt),
        changes.pop("run", event.run),
        data | changes,
    )
    return approval_module._sign(base, key)


def copy_with(event: LedgerEvent, **changes: object) -> LedgerEvent:
    """A copy of ``event`` with changed fields and its old signature kept."""
    return LedgerEvent(
        event.kind, event.at, event.task, event.attempt, event.run, {**event.data, **changes}
    )


def unsigned_approval(contract=fx.CONTRACT, identity: str = fx.REVIEWER, **extra) -> LedgerEvent:
    """An approval record written without the operator key, dressed up to look real."""
    bound = contracts.binding(contracts.freeze(contract))
    data = {
        "identity": identity,
        "os_user": "rolando",
        "authenticated_by": "operator key from the Keychain, confirmed by typing the code",
        "decided_at": NOW.isoformat(),
        "digest": bound["digest"],
        "scope": SCOPE,
        "decision": APPROVED,
        "digest_type": "contract",
        "expires_at": (NOW + timedelta(days=3)).isoformat(),
        "binding": bound,
        "decision_id": "0" * 32,
        "note": "Approved by rNavarrete at the terminal.",
    }
    return LedgerEvent(HUMAN_DECISION, NOW, TASK, data=data | extra)


# --- Approval records -------------------------------------------------------------


def approval_written_by_worker(attack: bool) -> Outcome:
    """The worker (or anything without the key) appends an approval record."""
    with Ledger() as led:
        record = unsigned_approval()
        led.append(record if attack else resign(record))
        return led.check()


def approval_signed_with_other_key(attack: bool) -> Outcome:
    with Ledger() as led:
        led.append(resign(unsigned_approval(), OTHER_KEY if attack else KEY))
        return led.check()


def approval_by_lookalike_identity(attack: bool) -> Outcome:
    """Signed with the real key, but naming someone other than Rolando: the bot,
    the bot-suffixed login, a padded login and a case change."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW - timedelta(days=4))  # expired, so it can't help
        if attack:
            for who in (WORKER, f"{fx.REVIEWER}[bot]", f" {fx.REVIEWER}", fx.REVIEWER.lower()):
                led.append(resign(unsigned_approval(identity=who)))
        else:
            led.append(resign(unsigned_approval()))
        return led.check()


def _approval_written_for(contract, decided: datetime, expires: datetime) -> LedgerEvent:
    """Rolando's signed approval, as his writer would have made it."""
    times = {"decided_at": decided.isoformat(), "expires_at": expires.isoformat()}
    return resign(unsigned_approval(contract, **times), at=decided)


def approval_expiry_stretched(attack: bool) -> Outcome:
    """Rolando's approval, made four days ago, expired yesterday. The record is
    edited in the ledger to expire in two days, keeping its signature (the
    honest twin: he signed it with that expiry)."""
    decided, later = NOW - timedelta(days=4), NOW + timedelta(days=2)
    with Ledger() as led:
        if attack:
            signed = _approval_written_for(fx.CONTRACT, decided, NOW - timedelta(days=1))
            led.append(copy_with(signed, expires_at=later.isoformat()))
        else:
            led.append(_approval_written_for(fx.CONTRACT, decided, later))
        return led.check()


def approval_moved_to_edited_contract(attack: bool) -> Outcome:
    """The contract is widened to include the release workflow. Rolando's
    approval record of the original is edited in the ledger to the widened
    contract (digest, bound fields and their hash), keeping its signature."""
    wider = wider_contract()
    decided, expires = NOW - timedelta(hours=1), NOW + timedelta(days=2)
    with Ledger() as led:
        if attack:
            signed = _approval_written_for(fx.CONTRACT, decided, expires)
            bound = contracts.binding(contracts.freeze(wider))
            led.append(
                copy_with(
                    signed,
                    digest=bound["digest"],
                    binding=bound,
                    binding_sha256=approval_module._binding_sha256(bound),
                )
            )
        else:
            led.append(_approval_written_for(wider, decided, expires))
        return led.check(wider)


def approval_replayed_after_revoke(attack: bool) -> Outcome:
    """Rolando revokes his approval; the old approval record is appended again."""
    with Ledger() as led:
        stored = led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=2))
        led.desk.revoke(TASK, fx.DIGEST, "changed my mind", NOW - timedelta(hours=1))
        if attack:
            led.append(stored.event)
        else:
            led.desk.approve(fx.CONTRACT, NOW)
        return led.check()


def approval_replayed_to_revive_old_version(attack: bool) -> Outcome:
    """Rolando approved version 1, then version 2. His version 1 approval is
    appended again, so it is the newest record, and version 1 is dispatched."""
    v2 = plain_contract(version="2")
    with Ledger() as led:
        v1 = led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=2)).event
        led.desk.approve(v2, NOW - timedelta(hours=1))
        if attack:
            led.append(v1)
        else:
            led.desk.approve(fx.CONTRACT, NOW)
        return led.check(fx.CONTRACT)


def approval_replayed_after_reject(attack: bool) -> Outcome:
    """Rolando rejects the contract after approving it; the approval is re-signed
    with its original (earlier) time, so it sorts before the rejection."""
    with Ledger() as led:
        stored = led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=2))
        led.desk.reject(fx.CONTRACT, "scope too wide", NOW - timedelta(hours=1))
        if attack:
            led.append(resign(stored.event))
        else:
            led.desk.approve(fx.CONTRACT, NOW)
        return led.check()


def old_version_after_new_approval(attack: bool) -> Outcome:
    """Rolando approved version 1, then a revised version 2. Version 1 is dispatched."""
    v2 = plain_contract(version="2")
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=1))
        led.desk.approve(v2, NOW)
        return led.check(fx.CONTRACT if attack else v2)


def approval_used_after_expiry(attack: bool) -> Outcome:
    """Dispatch runs after the approval's expiry."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW, ttl=timedelta(days=1))
        return led.check(at=NOW + timedelta(days=2 if attack else 0, hours=1))


def approval_used_before_it_was_made(attack: bool) -> Outcome:
    """The dispatch clock is set back to before Rolando approved."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW)
        return led.check(at=NOW - timedelta(hours=1) if attack else NOW + timedelta(hours=1))


def approval_signed_for_a_year(attack: bool) -> Outcome:
    """Someone holding the key signs an approval that lasts a year."""
    with Ledger() as led:
        expires = NOW + (timedelta(days=365) if attack else MAX_TTL)
        led.append(resign(unsigned_approval(expires_at=expires.isoformat())))
        return led.check()


def approval_with_retired_key(attack: bool) -> Outcome:
    """After a key change, the old key signs a new approval. Records it signed
    before it was retired still count (the honest version)."""
    with Ledger() as led:
        retired_at = led.count()
        led.append(approval_module._sign(unsigned_approval(), OLD_KEY))
        if not attack:
            retired_at = led.count()  # the key was retired after it signed this
        desk = Approvals(
            led.store,
            KEY,
            confirm=_typed,
            os_user="rolando",
            retired_keys=((OLD_KEY, retired_at),),
        )
        return verdict_outcome(desk.check(fx.CONTRACT, NOW))


# --- Repair, re-fire and clearing records -----------------------------------------


def _decision(event: LedgerEvent, digest: object, by: str = fx.REVIEWER) -> LedgerEvent:
    """``event`` with the fields a signed decision carries, not yet signed."""
    common = {
        "identity": by,
        "os_user": "rolando",
        "decided_at": event.at.isoformat(),
        "digest": str(digest),
    }
    return LedgerEvent(
        event.kind, event.at, event.task, event.attempt, event.run, {**event.data, **common}
    )


def repair_record(number: int, contract=fx.CONTRACT, by: str = fx.REVIEWER, at=NOW) -> LedgerEvent:
    attempt = AttemptId(TASK, number)
    event = ev.repair_authorized(attempt, f"attempt {number - 1}'s tests failed", by, at)
    return _decision(event, contracts.digest(contract).value, by)


def _first_attempt_done(led: Ledger, contract=fx.CONTRACT) -> RunId:
    led.desk.approve(contract, NOW - timedelta(hours=4))
    run = led.dispatch(contract, at=NOW - timedelta(hours=3))
    led.finish(run.attempt, NOW - timedelta(hours=2))
    return run


def repair_written_by_worker(attack: bool) -> Outcome:
    """Attempt 1 failed. The worker writes its own go-ahead for attempt 2."""
    with Ledger() as led:
        _first_attempt_done(led)
        if attack:
            led.append(repair_record(2, by=WORKER))
        else:
            led.desk.authorize_repair(fx.CONTRACT, 2, "tests failed", NOW - timedelta(hours=1))
        return led.check()


def repair_unsigned_in_rolandos_name(attack: bool) -> Outcome:
    """The same, but the record names Rolando."""
    with Ledger() as led:
        _first_attempt_done(led)
        if attack:
            led.append(repair_record(2))
        else:
            led.append(resign(repair_record(2)))
        return led.check()


def repair_for_other_contract(attack: bool) -> Outcome:
    """Rolando's repair go-ahead was for version 1; attempt 2 runs a widened
    version that Rolando also approved."""
    wider = wider_contract()
    with Ledger() as led:
        _first_attempt_done(led)
        led.desk.authorize_repair(fx.CONTRACT, 2, "tests failed", NOW - timedelta(hours=1))
        led.desk.approve(wider, NOW - timedelta(minutes=30))
        if not attack:
            led.desk.authorize_repair(wider, 2, "tests failed", NOW - timedelta(minutes=20))
        return led.check(wider)


def repair_signed_in_advance(attack: bool) -> Outcome:
    """A signed go-ahead for attempt 2 written before attempt 1 even started."""
    with Ledger() as led:
        early = resign(repair_record(2, at=NOW - timedelta(hours=5)))
        if attack:
            led.append(early)
        _first_attempt_done(led)
        if not attack:
            led.append(resign(repair_record(2, at=NOW - timedelta(hours=1))))
        return led.check()


def repair_past_the_budget(attack: bool) -> Outcome:
    """A contract approved for 2 attempts runs both, then a signed go-ahead for
    a third is added (the honest twin: the same steps on a contract approved
    for 3). The factory-wide cap of 3 has its own case."""
    contract = plain_contract(attempt_budget=2 if attack else 3)
    with Ledger() as led:
        _first_attempt_done(led, contract)
        led.desk.authorize_repair(contract, 2, "tests failed", NOW - timedelta(minutes=90))
        run = led.dispatch(contract, at=NOW - timedelta(minutes=80))
        led.finish(run.attempt, NOW - timedelta(minutes=70))
        led.append(resign(repair_record(3, contract, at=NOW - timedelta(minutes=5))))
        return led.check(contract)


def refire_after_a_real_launch(attack: bool) -> Outcome:
    """Attempt 1 really launched; a signed re-fire record asks to fire it again."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=3))
        result = launched() if attack else NOT_LAUNCHED
        run = led.dispatch(result=result, at=NOW - timedelta(hours=2))
        event = ev.refire_authorized(run, fx.REVIEWER, NOW - timedelta(hours=1))
        led.append(resign(_decision(event, fx.DIGEST.value)))
        return led.check(refire_of=run)


def refire_written_by_worker(attack: bool) -> Outcome:
    """Attempt 1 was not launched. The worker writes the go-ahead to fire it again."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=3))
        run = led.dispatch(result=NOT_LAUNCHED, at=NOW - timedelta(hours=2))
        if attack:
            event = ev.refire_authorized(run, WORKER, NOW - timedelta(hours=1))
            led.append(_decision(event, fx.DIGEST.value, WORKER))
        else:
            led.desk.authorize_refire(fx.CONTRACT, run, NOW - timedelta(hours=1))
        return led.check(refire_of=run)


def _lost_first_attempt(led: Ledger) -> RunId:
    led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=4))
    return led.dispatch(result=LOST, at=NOW - timedelta(hours=3))


def clearing_written_by_worker(attack: bool) -> Outcome:
    """Attempt 1's launch may have started a session. The worker writes the
    record saying it finished, so attempt 2 can start."""
    with Ledger() as led:
        run = _lost_first_attempt(led)
        at = NOW - timedelta(hours=2)
        url = "https://claude.ai/code/cse_redteam1"
        if attack:
            event = ev.attempt_cleared(run.attempt, ev.ClearingBasis.COMPLETED, url, WORKER, at)
            event = LedgerEvent(event.kind, at, TASK, run.attempt, run, event.data)
            led.append(_decision(event, fx.DIGEST.value, WORKER))
        else:
            led.desk.record_clearing(run.attempt, ev.ClearingBasis.COMPLETED, url, at)
        led.desk.authorize_repair(fx.CONTRACT, 2, "session lost", NOW - timedelta(hours=1))
        return led.check()


def clearing_names_an_older_fire(attack: bool) -> Outcome:
    """Attempt 1 was not launched, was fired again, and that second fire may have
    started a session. A signed clearing names the first fire, not the latest."""
    with Ledger() as led:
        led.desk.approve(fx.CONTRACT, NOW - timedelta(hours=5))
        first = led.dispatch(result=NOT_LAUNCHED, at=NOW - timedelta(hours=4))
        led.desk.authorize_refire(fx.CONTRACT, first, NOW - timedelta(hours=3, minutes=30))
        second = led.dispatch(result=LOST, at=NOW - timedelta(hours=3), refire_of=first)
        at = NOW - timedelta(hours=2)
        url = "https://claude.ai/code/cse_redteam1"
        event = ev.attempt_cleared(first.attempt, ev.ClearingBasis.COMPLETED, url, fx.REVIEWER, at)
        named = first if attack else second
        event = LedgerEvent(event.kind, at, TASK, first.attempt, named, event.data)
        led.append(resign(_decision(event, fx.DIGEST.value)))
        led.desk.authorize_repair(fx.CONTRACT, 2, "session lost", NOW - timedelta(hours=1))
        return led.check()


# --- Writing decisions --------------------------------------------------------------


def _write(
    write: Callable[[Ledger], object],
    setup: Callable[[Ledger], object] | None = None,
    **desk: object,
) -> Outcome:
    """Run one write after ``setup``; refused means it raised and wrote nothing."""
    with Ledger() as led:
        if setup is not None:
            setup(led)
        before = led.count()
        try:
            write(led) if not desk else write(_with_desk(led, **desk))
        except (ApprovalRefused, ValueError) as e:
            wrote = led.count() - before
            return Outcome(wrote > 0, ("refused",), f"refused ({wrote} records written): {e}")
        return Outcome(True, (), f"recorded {led.count() - before} records")


def _with_desk(led: Ledger, **desk: object) -> Ledger:
    led.desk = Approvals(led.store, KEY, **({"confirm": _typed, "os_user": "rolando"} | desk))
    return led


def approve_without_the_code(attack: bool) -> Outcome:
    """approve() is called with nobody typing the code at Rolando's terminal."""
    confirm = _not_typed if attack else _typed
    return _write(lambda led: led.desk.approve(fx.CONTRACT, NOW), confirm=confirm)


def approve_as_the_worker(attack: bool) -> Outcome:
    """The approval writer is set up with the worker's identity."""
    identity = WORKER if attack else fx.REVIEWER
    return _write(lambda led: led.desk.approve(fx.CONTRACT, NOW), identity=identity)


def approve_for_a_year(attack: bool) -> Outcome:
    ttl = timedelta(days=365) if attack else MAX_TTL
    return _write(lambda led: led.desk.approve(fx.CONTRACT, NOW, ttl=ttl))


def approve_a_contract_needing_clarification(attack: bool) -> Outcome:
    """A contract with an open question is put up for approval."""
    contract = plain_contract()
    if attack:
        criteria = [dict(c) for c in contract["acceptance_criteria"]]
        criteria[0] |= {"status": "needs-clarification", "clarification": "Which order?"}
        contract["acceptance_criteria"] = criteria
    return _write(lambda led: led.desk.approve(contract, NOW))


def repair_written_past_the_budget(attack: bool) -> Outcome:
    """A contract approved for 2 attempts runs both; Rolando's writer is asked
    for a go-ahead for a third (the honest twin: a contract approved for 3)."""
    contract = plain_contract(attempt_budget=2 if attack else 3)

    def setup(led: Ledger) -> None:
        _first_attempt_done(led, contract)
        led.desk.authorize_repair(contract, 2, "tests failed", NOW - timedelta(minutes=90))
        run = led.dispatch(contract, at=NOW - timedelta(minutes=80))
        led.finish(run.attempt, NOW - timedelta(minutes=70))

    def write(led: Ledger) -> None:
        led.desk.authorize_repair(contract, 3, "tests failed", NOW)

    return _write(write, setup)
