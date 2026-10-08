"""The background service: one round at a time, everything through the ledger.

``Service.tick()`` is one round. ``controller.service run`` calls it in a loop
on the host; tests call it directly with fixtures and a fake clock. A round:

1. Onboarding: the mapping file is re-read; a change is recorded. On the
   first round after a start, a gap in the heartbeat is reported as an outage
   on every open ticket and each project's status ticket.
2. Recovery: a fire left without a response by a service that died
   mid-launch is marked launch-outcome-unknown (``Recovery.recover``). Nothing
   is ever re-fired.
3. Intake: poll the authorization source from the saved cursor. Pause and
   resume are applied as factory holds. Each Todo move is accepted (onboarded
   project, intake enabled, not a replay, no other open item for the ticket)
   or refused, and the results go into the ledger in one write with the new
   cursor.
4. Work: for each open item, oldest first: reconcile its running attempt with
   GitHub, start the review once its PR is seen, close it once it is merged
   or finished; otherwise prepare its contract and dispatch it. Dispatch goes
   through ``Dispatcher.dispatch`` only, so the approval check, the attempt
   gate (single lane, caps, holds, rate limits) and recovery all apply, and
   a repeated dispatch never starts a second worker. At most one fire is
   made per round.
5. Outbox: send queued Linear messages. A message that fails is retried
   later with backoff; retrying a message never repeats a launch.
6. Heartbeat and backup.

Every step catches its own failure, so a Linear or GitHub outage delays one
step and nothing else. A step that fails records nothing it hasn't finished.

The service never approves, merges or releases, and never writes a decision:
it holds no terminal, so every ``confirm`` it gives the controller says no.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval import ContractStore
from controller.approval.approval import SOURCE_AUTHORIZATION
from controller.attempts import AttemptGate
from controller.attempts import events as gate_events
from controller.attempts.policy import LedgerView
from controller.dispatch import Dispatcher, Refused
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LedgerEvent,
    LedgerLocked,
    LedgerStore,
)
from controller.ledger.redact import redact
from controller.recovery import FINISHED, Recovery, State
from controller.service import queue as q
from controller.service.onboarding import Onboarding, OnboardingError
from controller.service.seams import (
    Authorization,
    AuthorizationRefused,
    Integrations,
    Prepared,
    PullRequestRef,
    Question,
    Standing,
)

log = logging.getLogger("factory.service")

PAUSE_HOLD = "paused-from-linear"

# Attempt states where the item waits on the worker or on Rolando, and is
# reconciled with GitHub instead of dispatched.
_WATCHED = frozenset(
    {State.DISPATCHING, State.UNKNOWN, State.RUNNING, State.VERIFYING, State.AWAITING_HUMAN}
)

# Refusals that end the item: the contract itself can't go out as it is.
_FINAL_REFUSALS = frozenset(
    {"contract-invalid", "wrong-repository", "base-not-on-main", "contract-changed"}
)

# When the signer may stand in for a missing approval: nothing in force for
# this contract. Never after Rolando rejected or revoked it.
_ASK_SIGNER = frozenset({"approval-missing", "approval-expired", "approval-for-different-contract"})
_NEVER_ASK_SIGNER = frozenset({"approval-rejected", "approval-revoked"})


def never(summary: str, code: str) -> bool:
    """The service's answer to every confirmation prompt: it has no terminal
    and decides nothing."""
    return False


@dataclass(frozen=True)
class Settings:
    reconcile_every: timedelta = timedelta(minutes=5)
    retry_every: timedelta = timedelta(minutes=5)
    """How soon a refused item is tried again."""
    outage_after: timedelta = timedelta(minutes=15)
    """A gap in the heartbeat longer than this is reported as an outage."""
    outbox_retry_after: timedelta = timedelta(minutes=2)
    outbox_retry_max: timedelta = timedelta(hours=1)
    keep_backups: int = 48


@dataclass
class TickReport:
    fired: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    sent: int = 0
    errors: list[str] = field(default_factory=list)
    wrote: bool = False


class Heartbeat:
    """The time of the last finished round, in a file beside the ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> datetime | None:
        try:
            value = datetime.fromisoformat(self.path.read_text().strip())
        except (OSError, ValueError):
            return None
        return value if value.tzinfo is not None else None

    def write(self, now: datetime) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(now.isoformat())
        tmp.replace(self.path)


class Service:
    def __init__(
        self,
        store: LedgerStore,
        dispatcher: Dispatcher,
        recovery: Recovery,
        gate: AttemptGate,
        contracts_store: ContractStore,
        onboarding: Callable[[], Onboarding],
        integrations: Integrations,
        *,
        settings: Settings | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: Heartbeat | None = None,
        backup: Callable[[datetime], object] | None = None,
        instance: str = "service",
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._recovery = recovery
        self._gate = gate
        self._contracts = contracts_store
        self._onboarding = onboarding
        self._x = integrations
        self._s = settings or Settings()
        self._now = now
        self._heartbeat = heartbeat
        self._backup = backup
        self._instance = instance
        self._started = False
        # Read once, before any round can overwrite it, so an outage is
        # reported even if the first round fails to record the start.
        self._last_beat = heartbeat.read() if heartbeat is not None else None
        self._last_reconcile: dict[AttemptId, datetime] = {}
        self._last_try: dict[str, datetime] = {}
        self._backed_up: int | None = None
        """The ledger's last seq at the last successful backup; None until the
        first one, so a fresh start always backs up once."""
        self._config: Onboarding | None = None

    # --- the round ---------------------------------------------------------------

    def tick(self) -> TickReport:
        r = TickReport()
        now = self._now()
        before = self._last_seq()
        self._step(r, "onboarding", lambda: self._load_onboarding(now))
        self._step(r, "start", lambda: self._start(now))
        self._step(r, "recovery", lambda: self._recovery.recover(now))
        if self._config is not None:
            self._step(r, "intake", lambda: self._intake(r, now))
        self._step(r, "work", lambda: self._work(r))
        self._step(r, "outbox", lambda: self._flush(r))
        seq = self._last_seq()
        r.wrote = seq != before
        if self._backup is not None and seq != self._backed_up:
            # Retried every round until it succeeds, whether or not this round
            # wrote anything: what matters is what the last backup is missing.
            self._step(r, "backup", lambda: self._back_up(seq))
        if self._heartbeat is not None and self._started:
            self._step(r, "heartbeat", lambda: self._heartbeat.write(self._now()))
        return r

    def _back_up(self, seq: int) -> None:
        assert self._backup is not None
        self._backup(self._now())
        self._backed_up = seq

    def _step(self, r: TickReport, name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except LedgerLocked:
            r.errors.append(f"{name}: the ledger is busy (a controller command is writing)")
        except Exception as e:  # each step stands alone; the next round retries it
            text = _error(e)
            r.errors.append(f"{name}: {text}")
            log.warning("%s failed: %s", name, text)

    def _last_seq(self) -> int:
        return max((s.seq for s in self._store.events()), default=0)

    def _view(self) -> q.ServiceView:
        return q.ServiceView.build(self._store.events())

    def _append(self, *events: LedgerEvent) -> None:
        if events:
            with self._store.writer_lock():
                self._store.append(*events)

    # --- 0. start-up and outage report --------------------------------------------

    def _start(self, now: datetime) -> None:
        if self._started:
            return
        last = self._last_beat
        gap = None if last is None else now - last
        events = [
            q.started(
                {
                    "instance": self._instance,
                    "last_heartbeat": None if last is None else last.isoformat(),
                    "gap_seconds": None if gap is None else int(gap.total_seconds()),
                },
                now,
            )
        ]
        if gap is not None and gap > self._s.outage_after:
            text = (
                f"The factory was not running from {_when(last)} to {_when(now)}."
                " It is back and is now catching up on anything it missed in Linear and"
                " on GitHub. Nothing was started twice."
            )
            stamp = now.strftime("%Y%m%dT%H%M%SZ")
            for issue in self._notice_targets():
                events.append(q.message(f"outage:{stamp}:{issue}", issue, text, now))
        self._append(*events)
        self._started = True

    def _notice_targets(self) -> list[str]:
        targets = [i.issue_id for i in self._view().open_items()]
        if self._config is not None:
            for p in self._config.projects.values():
                if p.status_issue_id and p.status_issue_id not in targets:
                    targets.append(p.status_issue_id)
        return targets

    # --- 1. onboarding -------------------------------------------------------------

    def _load_onboarding(self, now: datetime) -> None:
        try:
            config = self._onboarding()
        except OnboardingError:
            self._config = None  # nothing is accepted or dispatched without it
            raise
        self._config = config
        if self._view().config_sha256 != config.sha256:
            self._append(
                q.config_seen(config.sha256, sorted(config.projects), config.intake_enabled, now)
            )

    # --- 2. intake -------------------------------------------------------------------

    def _intake(self, r: TickReport, now: datetime) -> None:
        assert self._config is not None
        view = self._view()
        batch = self._x.source.poll(view.cursor)
        events: list[LedgerEvent] = []
        seen = set(view.seen)
        open_issues = {i.issue_id for i in view.open_items()}
        earlier = {i.issue_id: i for i in view.open_items()}

        for c in batch.controls:
            if c.event_id in seen:
                continue
            seen.add(c.event_id)
            applied = self._apply_control(c.action, c.note, now)
            events.append(q.control(c, applied, now))
            target = c.issue_id or self._status_issue()
            if target:
                events.append(q.message(f"control:{c.event_id}", target, applied, now))

        for ref in batch.refusals:
            if ref.event_id in seen:
                continue
            seen.add(ref.event_id)
            events.append(q.refused(ref, ref.reason, now))
            events.append(
                q.message(
                    f"refused:{ref.event_id}",
                    ref.issue_id,
                    f"The factory did not start work on this ticket: {ref.reason}",
                    now,
                )
            )
            r.refused.append(ref.issue_key)

        for a in batch.authorizations:
            if a.event_id in seen:
                continue
            seen.add(a.event_id)
            reason = self._acceptance_problem(a, open_issues)
            old = earlier.pop(a.issue_id, None)
            if reason is not None and old is not None and self._replaceable(old, a):
                # A newer move of a ticket whose older move hasn't started
                # anything: the newer one is what Rolando approved last.
                events += self._replace(old, now)
                open_issues.discard(a.issue_id)
                reason = self._acceptance_problem(a, open_issues)
            if reason is None:
                events.append(q.accepted(a, now))
                open_issues.add(a.issue_id)
                r.accepted.append(a.issue_key)
                text = (
                    "The factory picked this ticket up and queued it. It will post here when"
                    " it starts the work."
                )
            else:
                events.append(q.refused(a, reason, now))
                r.refused.append(a.issue_key)
                text = f"The factory did not start work on this ticket: {reason}"
            events.append(q.message(f"intake:{a.event_id}", a.issue_id, text, now))

        if batch.cursor and batch.cursor != view.cursor:
            events.append(q.cursor(batch.cursor, now))
        self._append(*events)

    def _replaceable(self, old: q.Item, new: Authorization) -> bool:
        return (
            self._config is not None
            and self._config.intake_enabled
            and self._config.project(new.project_id) is not None
            and new.moved_at > datetime.fromisoformat(old.moved_at)
            and not self._owned_attempts(old)
        )

    def _replace(self, old: q.Item, now: datetime) -> list[LedgerEvent]:
        return [
            q.item_closed(old, "replaced", now),
            q.message(
                f"closed:{old.event_id}",
                old.issue_id,
                "You moved this ticket to Todo again before the factory started it. It will"
                " work from that newer move instead.",
                now,
            ),
        ]

    def _owned_attempts(self, item: q.Item) -> set[AttemptId]:
        """The attempts reserved for the item's task after it was accepted."""
        return {
            s.event.attempt
            for s in self._store.events(item.task)
            if s.event.kind == gate_events.ATTEMPT_RESERVED
            and s.seq > item.seq
            and s.event.attempt is not None
        }

    def _standing(self, item: q.Item) -> Standing:
        source = self._x.source
        check = getattr(source, "standing", None)
        if check is not None:
            return check(item.authorization())
        return Standing(withdrawn=source.revalidate(item.authorization()))

    def _acceptance_problem(self, a: Authorization, open_issues: set[str]) -> str | None:
        assert self._config is not None
        if self._config.project(a.project_id) is None:
            return "its Linear project is not onboarded to the factory."
        if not self._config.intake_enabled:
            return "the factory is not taking new tickets yet (intake is switched off)."
        if a.issue_id in open_issues:
            return "this ticket is already queued or in progress."
        return None

    def _apply_control(self, action: str, note: str, now: datetime) -> str:
        holds = LedgerView.build(self._store.events()).holds
        if action == "pause":
            if PAUSE_HOLD in holds:
                return "The factory was already paused."
            self._gate.hold(PAUSE_HOLD, note, now)
            return (
                "The factory is paused: it will not start any new worker until you resume it."
                " A worker that is already running is not stopped by this; stop it from the"
                " routine's run page if you need to."
            )
        if PAUSE_HOLD not in holds:
            return "The factory was not paused from Linear, so there was nothing to resume."
        self._gate.resume(note, now, reason=PAUSE_HOLD)
        rest = sorted(set(holds) - {PAUSE_HOLD})
        if rest:
            return f"Resumed from Linear, but other holds still stop new work: {', '.join(rest)}."
        return "The factory is running again."

    def _status_issue(self) -> str | None:
        if self._config is None:
            return None
        return next(
            (p.status_issue_id for p in self._config.projects.values() if p.status_issue_id), None
        )

    # --- 3. work --------------------------------------------------------------------

    def _work(self, r: TickReport) -> None:
        fired = False
        for item in self._view().open_items():
            try:
                fired = self._advance(item, r, may_fire=not fired) or fired
            except LedgerLocked:
                raise
            except Exception as e:
                r.errors.append(f"{item.issue_key}: {_error(e)}")
                log.warning("%s: %s", item.issue_key, _error(e))

    def _advance(self, item: q.Item, r: TickReport, *, may_fire: bool) -> bool:
        now = self._now()
        stored = self._store.events(item.task)
        # The item owns the attempts reserved after it was accepted.
        owned = {
            s.event.attempt
            for s in stored
            if s.event.kind == gate_events.ATTEMPT_RESERVED and s.seq > item.seq
        }
        attempts = LedgerView.build(stored).task_attempts(item.task)
        mine = [a for a in attempts if a.attempt in owned]
        if len(mine) < len(attempts) and not mine:
            # v1: one ticket is one task with one attempt budget. Work from an
            # earlier Todo move of this ticket can't be started over here.
            self._close(
                item,
                "ticket-already-worked",
                now,
                "The factory already worked on this ticket"
                f" ({attempts[-1].attempt}, {attempts[-1].state}), so it won't start it again."
                " For new work, write a new ticket and move that one to Todo.",
            )
            r.closed.append(item.issue_key)
            return False
        if mine:
            status = self._recovery.attempt_status(mine[-1].attempt, now)
            if status.state in _WATCHED and not _waiting_to_refire(status, mine[-1]):
                self._watch(item, status, now)
                return False
            if status.state is State.MERGED:
                self._close(
                    item, "merged", now, "Rolando merged the PR. This ticket's work is done."
                )
                r.closed.append(item.issue_key)
                return False
            if status.state in FINISHED:
                if item.withdrawn is not None:
                    self._close(
                        item,
                        "withdrawn",
                        now,
                        f"Attempt {status.attempt} ended as {status.state.value}. You had moved"
                        " the ticket out of Todo, so the factory stops here and suggests no"
                        " repair.",
                    )
                    r.closed.append(item.issue_key)
                    return False
                advice = self._x.repair.advise(status.attempt, status.detail)
                if advice is None:
                    self._close(
                        item,
                        status.state.value,
                        now,
                        f"Attempt {status.attempt} ended as {status.state.value}"
                        f" ({status.detail}). The factory has stopped work on this ticket.",
                    )
                    r.closed.append(item.issue_key)
                    return False
                # The repair needs Rolando's signed go-ahead (ENG-160); the item
                # closes so the queue isn't held waiting for it.
                self._close(
                    item,
                    "repair-suggested",
                    now,
                    f"Attempt {status.attempt} ended as {status.state.value}. A repair is"
                    f" suggested: {advice}. It needs your go-ahead before it starts.",
                )
                r.closed.append(item.issue_key)
                return False
        if not may_fire:
            return False
        last = self._last_try.get(item.event_id)
        if last is not None and now - last < self._s.retry_every:
            return False
        self._last_try[item.event_id] = now
        return self._dispatch(item, r)

    def _dispatch(self, item: q.Item, r: TickReport) -> bool:
        now = self._now()
        assert self._config is not None
        project = self._config.project(item.project_id)
        if project is None:
            self._close(
                item,
                "project-removed",
                now,
                "This ticket's project was removed from the factory, so the work will not start.",
            )
            r.closed.append(item.issue_key)
            return False
        if item.withdrawn is not None:
            self._close(
                item,
                "withdrawn",
                now,
                "The factory won't start anything more for this ticket: you moved it out of"
                " Todo after its worker started. For new work, write a new ticket.",
            )
            r.closed.append(item.issue_key)
            return False
        standing = self._standing(item)
        if standing.reason is None and not standing.in_todo:
            standing = Standing(withdrawn="the ticket left Todo before the factory started it")
        if standing.reason is not None:
            again = (
                " To start from the new text, move it out of Todo and back."
                if standing.withdrawn is None
                else ""
            )
            self._close(
                item,
                "authorization-withdrawn",
                now,
                f"The factory stopped before starting: {standing.reason}.{again}",
            )
            r.closed.append(item.issue_key)
            return False
        if standing.waiting_on:
            names = ", ".join(standing.waiting_on)
            self._notice(
                item,
                f"waiting-on:{names}",
                f"Waiting before starting: this ticket is blocked by {names}, which isn't done.",
                now,
            )
            return False
        contract = self._contract(item, project.as_mapping(), project, r)
        if contract is None:
            return False
        if not self._authorized(item, contract, r, now):
            return False
        try:
            result = self._dispatcher.dispatch(contract)
        except Refused as e:
            final = [b for b in e.blocks if b.code in _FINAL_REFUSALS]
            if final:
                self._close(
                    item,
                    final[0].code,
                    now,
                    "The factory can't start this ticket: " + "; ".join(b.detail for b in final),
                )
                r.closed.append(item.issue_key)
                return False
            for b in e.blocks:
                self._notice(item, f"blocked:{b.code}", f"Waiting before starting: {b.detail}", now)
            return False
        if result.run is None:
            return False  # already dispatched; watched next round
        r.fired.append(str(result.run))
        self._notice(item, f"fired:{result.run}", _fired_text(result), now)
        return True

    def _authorized(
        self, item: q.Item, contract: Mapping[str, object], r: TickReport, now: datetime
    ) -> bool:
        """Rolando's Todo move as the approval (his choice, 2026-10-08): if
        the contract has no standing approval, ask the signer, which checks
        the move with Linear itself and signs. The service only appends what
        the signer signed; it can't sign anything."""
        authorizer = self._x.authorizer
        if authorizer is None:
            return True
        _, _, blocks = self._dispatcher.launcher.check(contract, now)
        codes = {b.code for b in blocks}
        if not codes & _ASK_SIGNER:
            return True  # approved already, or blocked for another reason dispatch reports
        if codes & _NEVER_ASK_SIGNER:
            return True  # Rolando rejected or revoked it; dispatch reports that
        try:
            event = authorizer.authorize(item.authorization(), contract)
        except AuthorizationRefused as e:
            if e.final:
                self._close(
                    item,
                    "authorization-refused",
                    now,
                    f"The factory can't count your Todo move as approval: {e.reason}.",
                )
                r.closed.append(item.issue_key)
            else:
                self._notice(item, "authorize-wait", f"Waiting before starting: {e.reason}.", now)
            return False
        d = event.data
        if (
            event.kind != SOURCE_AUTHORIZATION
            or event.task != item.task
            or d.get("event_id") != item.event_id
            or d.get("digest") != contracts.digest(contract).value
        ):
            raise RuntimeError("the signer answered for a different move or contract")
        self._append(event)
        return True

    def _contract(
        self, item: q.Item, entry: Mapping[str, object], project, r: TickReport
    ) -> Mapping[str, object] | None:
        now = self._now()
        if item.digest is not None:
            contract = self._contracts.load(ContractDigest(item.digest))
            return self._within(item, project, contract, r, now)
        out = self._x.preparer.prepare(item.authorization(), entry)
        if isinstance(out, Question):
            self._close(
                item,
                "question",
                now,
                f"Before the factory can start, it needs an answer: {out.text}\n\n"
                "Answer here and move the ticket to Todo again.",
            )
            r.closed.append(item.issue_key)
            return None
        assert isinstance(out, Prepared)
        if self._within(item, project, out.contract, r, now) is None:
            return None
        contract = contracts.freeze(out.contract)
        digest = self._contracts.save(contract)
        self._append(q.item_contract(item, digest.value, now))
        return contract

    def _within(
        self, item: q.Item, project, contract: Mapping[str, object], r: TickReport, now
    ) -> Mapping[str, object] | None:
        """The contract, if the project's onboarding entry (as it is now) allows it."""
        problems = project.contract_problems(contract, item.task.value)
        if not problems:
            return contract
        self._close(
            item,
            "contract-outside-onboarding",
            now,
            "The factory has a task it isn't allowed to run here: " + "; ".join(problems),
        )
        r.closed.append(item.issue_key)
        return None

    def _watch(self, item: q.Item, status, now: datetime) -> None:
        attempt = status.attempt
        last = self._last_reconcile.get(attempt)
        if status.state is not State.DISPATCHING and (
            last is None or now - last >= self._s.reconcile_every
        ):
            self._last_reconcile[attempt] = now
            status = self._dispatcher.reconcile(attempt).status
            if status.state is not State.MERGED:
                # A merge moves the ticket to Done; that is not a withdrawal.
                self._check_standing_while_running(item, now)
        if status.state is State.UNKNOWN:
            self._notice(
                item,
                f"unknown:{status.latest_run}",
                f"It is unclear whether worker {status.latest_run} started. The factory will not"
                " start it again on its own and holds the lane until this is settled. Look for"
                " the session on the factory routine's run page, then record what you found"
                " (see the recovery steps in docs/service.md).",
                now,
            )
        view = self._view()
        if status.pull_requests and str(attempt) not in view.reviews:
            request = view.review_requests.get(str(attempt))
            if request is None:
                # Record the request before making it. If the call or the save
                # after it fails, the next round repeats this same request key,
                # which the reviewer treats as the review already asked for.
                number = status.pull_requests[-1]
                request = (number, f"review:{attempt}:pr{number}")
                self._append(q.review_requested(attempt, number, request[1], now))
            number, key = request
            note = self._x.reviewer.start(PullRequestRef(attempt, number), key)
            self._append(
                LedgerEvent(
                    q.REVIEW_STARTED,
                    now,
                    attempt.task,
                    attempt,
                    data={"pr": number, "key": key, "note": note},
                ),
                q.message(
                    f"review:{attempt}",
                    item.issue_id,
                    f"The worker opened PR #{number}. The independent review has started.",
                    now,
                ),
            )
        if status.state is State.MERGED:
            self._close(item, "merged", now, "Rolando merged the PR. This ticket's work is done.")

    def _check_standing_while_running(self, item: q.Item, now: datetime) -> None:
        """Rolando's move is checked again while the worker runs. A change
        never alters the contract or starts anything; it is reported."""
        if item.withdrawn is not None:
            return
        try:
            standing = self._standing(item)
        except Exception as e:  # Linear down: reconcile still goes on
            log.warning("%s: can't read Linear: %s", item.issue_key, _error(e))
            return
        if standing.withdrawn is not None:
            self._append(
                q.item_withdrawn(item, standing.withdrawn, now),
                q.message(
                    f"{item.event_id}:withdrawn",
                    item.issue_id,
                    f"This ticket no longer stands as approved ({standing.withdrawn}) after the"
                    " worker started. The factory won't start anything more for it, repairs"
                    " included. It can't stop a worker that is already running: if it should"
                    " stop, stop the session from the factory routine's run page, then record"
                    " what you found (docs/service.md, recovery steps). The factory keeps"
                    " checking GitHub for what the worker did.",
                    now,
                ),
            )
        elif standing.changed is not None:
            self._notice(
                item,
                "changed-while-running",
                f"This ticket changed while the worker was running ({standing.changed}). The"
                " worker keeps the task it started with and nothing new starts. If the change"
                " matters, say what you want here, or move the ticket out of Todo and In"
                " Progress to stop further work on it.",
                now,
            )

    def _close(self, item: q.Item, reason: str, now: datetime, text: str) -> None:
        self._append(
            q.item_closed(item, reason, now),
            q.message(f"closed:{item.event_id}", item.issue_id, text, now),
        )

    def _notice(self, item: q.Item, key: str, text: str, now: datetime) -> None:
        full = f"{item.event_id}:{key}"
        if full not in self._view().queued_keys:
            self._append(q.message(full, item.issue_id, text, now))

    # --- 4. outbox --------------------------------------------------------------------

    def _flush(self, r: TickReport) -> None:
        for m in sorted(self._view().outbox.values(), key=lambda m: m.seq):
            now = self._now()
            if m.last_failed_at is not None:
                wait = min(
                    self._s.outbox_retry_after * (2 ** min(m.failures - 1, 10)),
                    self._s.outbox_retry_max,
                )
                if now - m.last_failed_at < wait:
                    continue
            try:
                self._x.reporter.post(m.issue_id, m.key, m.text)
            except Exception as e:
                self._append(q.send_failed(m.key, _error(e), now))
                continue
            self._append(q.sent(m.key, now))
            r.sent += 1


def _waiting_to_refire(status, attempt_state) -> bool:
    """A definite not-launched answer leaves the attempt awaiting Rolando with
    no PR; dispatch is what re-fires it (with its signed go-ahead)."""
    last = attempt_state.last_fire
    return (
        status.state is State.AWAITING_HUMAN
        and not status.pull_requests
        and last is not None
        and last.outcome is LaunchOutcome.NOT_LAUNCHED
    )


def _fired_text(result) -> str:
    if result.outcome == "launched":
        return (
            f"The factory started the worker ({result.run}). Its draft PR will appear on the"
            " repository; the factory will post here when it does."
        )
    if result.outcome == "not-launched":
        return f"The worker did not start ({result.message}). The factory will try again later."
    return f"It is unclear whether the worker started: {result.message}"


def _error(e: BaseException) -> str:
    """An exception for the log and the ledger, with anything secret-looking blanked."""
    return redact(f"{type(e).__name__}: {e}")[:500]


def _when(t: datetime | None) -> str:
    return "an unknown time" if t is None else t.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


__all__ = ["PAUSE_HOLD", "Heartbeat", "Service", "Settings", "TickReport", "never"]
