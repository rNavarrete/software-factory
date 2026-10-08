"""Pure dispatch decisions over ledger events. No I/O, no clock: callers pass ``now``.

Everything here only ever blocks **new** launches. Nothing can stop a cloud
session that is already running, so run-time signals are alerts (G-C9).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from controller.attempts import events as ev
from controller.attempts.limits import PILOT_LIMITS, Limits
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    RunId,
    StoredEvent,
    TaskId,
)


@dataclass(frozen=True)
class Notice:
    """A reason dispatch is refused (a block) or something Rolando should see (an alert)."""

    code: str
    detail: str

    def as_data(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Fire:
    run: RunId
    at: datetime
    outcome: LaunchOutcome | None
    """None while no result is recorded; treated like launch-outcome-unknown."""
    http_status: int | None = None
    reason: str = ""
    session_url: str | None = None


@dataclass
class AttemptState:
    attempt: AttemptId
    digest: str
    reserved_at: datetime
    fires: list[Fire] = field(default_factory=list)
    cleared: ev.ClearingBasis | None = None

    @property
    def last_fire(self) -> Fire | None:
        return self.fires[-1] if self.fires else None

    @property
    def state(self) -> str:
        last = self.last_fire
        if last is not None and last.outcome is LaunchOutcome.NOT_LAUNCHED:
            return "not-launched"
        if self.cleared is not None:
            return f"cleared ({self.cleared.value})"
        if last is None or last.outcome is None:
            return "awaiting-launch-result"
        return last.outcome.value

    @property
    def unresolved(self) -> bool:
        """ADR 0002 section 6.1: a session may exist and nothing has cleared it."""
        return not (self.state == "not-launched" or self.cleared is not None)


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    session_pct: float
    weekly_pct: float
    credits_spent: float


@dataclass
class LedgerView:
    """What the gate needs, rebuilt from the full event list every time."""

    attempts: dict[AttemptId, AttemptState] = field(default_factory=dict)
    all_fires: list[Fire] = field(default_factory=list)
    hold: Mapping[str, object] | None = None
    retry_not_before: datetime | None = None
    snapshot: Snapshot | None = None
    repairs: set[AttemptId] = field(default_factory=set)
    refires: set[RunId] = field(default_factory=set)
    escalations: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, stored: Sequence[StoredEvent]) -> LedgerView:
        view = cls()
        fires_by_run: dict[RunId, int] = {}
        for item in sorted(stored, key=lambda s: s.seq):
            try:
                view._apply(item, fires_by_run)
            except (KeyError, TypeError, ValueError):
                # A malformed record counts for nothing it would grant.
                # Intents and reservations are written only by the gate.
                continue
        return view

    def _apply(self, item: StoredEvent, fires_by_run: dict[RunId, int]) -> None:
        e = item.event
        d = e.data
        if e.kind == ev.ATTEMPT_RESERVED and e.attempt is not None:
            self.attempts.setdefault(
                e.attempt, AttemptState(e.attempt, str(d.get("digest", "")), e.at)
            )
        elif e.kind == ev.FIRE_INTENT and e.run is not None:
            state = self.attempts.setdefault(e.run.attempt, AttemptState(e.run.attempt, "", e.at))
            fire = Fire(e.run, e.at, None)
            fires_by_run[e.run] = len(self.all_fires)
            self.all_fires.append(fire)
            state.fires.append(fire)
        elif e.kind == ev.FIRE_RESULT and e.run is not None and e.run in fires_by_run:
            index = fires_by_run[e.run]
            old = self.all_fires[index]
            if old.outcome is not None:
                return
            status = d.get("http_status")
            new = Fire(
                old.run,
                old.at,
                LaunchOutcome(d["outcome"]),
                status if isinstance(status, int) else None,
                str(d.get("reason", "")),
                d.get("session_url") if isinstance(d.get("session_url"), str) else None,
            )
            self.all_fires[index] = new
            fires = self.attempts[e.run.attempt].fires
            fires[fires.index(old)] = new
        elif e.kind == ev.ATTEMPT_CLEARED and e.attempt in self.attempts:
            basis = ev.ClearingBasis(d["basis"])
            evidence = d[ev.clearing_evidence_field(basis)]
            if isinstance(evidence, str) and evidence.strip() and str(d.get("by", "")).strip():
                self.attempts[e.attempt].cleared = basis
        elif e.kind == ev.REPAIR_AUTHORIZED and e.attempt is not None:
            if e.attempt.number >= 2 and str(d["failure"]).strip() and str(d["by"]).strip():
                self.repairs.add(e.attempt)
        elif e.kind == ev.REFIRE_AUTHORIZED and e.run is not None:
            if str(d["by"]).strip():
                self.refires.add(e.run)
        elif e.kind == ev.HOLD_SET:
            self.hold = d
        elif e.kind == ev.HOLD_CLEARED:
            self.hold = None
        elif e.kind == ev.RATE_LIMIT_WAIT:
            until = _aware(d["not_before"])
            if self.retry_not_before is None or until > self.retry_not_before:
                self.retry_not_before = until
        elif e.kind == ev.USAGE_SNAPSHOT:
            snap = Snapshot(
                _aware(d["taken_at"]),
                _pct(d["session_pct"]),
                _pct(d["weekly_pct"]),
                _number(d["credits_spent"]),
            )
            if self.snapshot is None or snap.taken_at >= self.snapshot.taken_at:
                self.snapshot = snap
        elif e.kind == ev.ESCALATION:
            self.escalations.add(str(d["dedup"]))

    def task_attempts(self, task: TaskId) -> list[AttemptState]:
        return sorted((s for a, s in self.attempts.items() if a.task == task), key=_number_of)

    def trailing_rate_limits(self) -> int:
        """Consecutive 429 results at the end of the factory's fire history."""
        count = 0
        for fire in reversed(self.all_fires):
            if fire.outcome is None:
                continue
            if fire.http_status != 429:
                break
            count += 1
        return count


def _number_of(state: AttemptState) -> int:
    return state.attempt.number


def _aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("expected an ISO time string")
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("ledger times must be timezone-aware")
    return when


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number")
    return float(value)


def _pct(value: object) -> float:
    pct = _number(value)
    if not 0 <= pct <= 100:
        raise ValueError("percentages are 0 to 100")
    return pct


@dataclass(frozen=True)
class Decision:
    task: TaskId
    run: RunId | None
    """The run that would be fired if nothing blocks it."""
    blocks: tuple[Notice, ...]
    alerts: tuple[Notice, ...]

    @property
    def allowed(self) -> bool:
        return not self.blocks and self.run is not None


ESCALATING_BLOCKS = frozenset({"attempt-cap", "task-fire-cap"})
"""Blocks that need Rolando's decision before the task can go on (G-C5, G-C12)."""


def check_dispatch(
    stored: Sequence[StoredEvent],
    task: TaskId,
    digest: ContractDigest,
    now: datetime,
    *,
    refire_of: RunId | None = None,
    automated: bool = False,
    limits: Limits = PILOT_LIMITS,
) -> Decision:
    """May the controller fire for ``task`` now?

    Without ``refire_of`` this asks for the task's next attempt. With it, it asks
    to fire that run's attempt again after a definite not-launched result.
    """
    view = LedgerView.build(stored)
    blocks: list[Notice] = []
    alerts: list[Notice] = []

    if automated:
        blocks.append(
            Notice(
                "automated-repair-deferred",
                "Automated repair stays off until ENG-177 proves a running worker can be stopped.",
            )
        )
    if view.hold is not None:
        blocks.append(Notice("hold", f"Factory is on hold: {view.hold.get('reason', '?')}."))
    if view.retry_not_before is not None and now < view.retry_not_before:
        blocks.append(
            Notice("rate-limited", f"No fire before {view.retry_not_before.isoformat()} (429).")
        )
    _check_snapshot(view, now, limits, blocks, alerts)
    _check_window(view, now, limits, blocks, alerts)

    for state in view.attempts.values():
        if state.unresolved:
            blocks.append(
                Notice(
                    "unresolved-attempt",
                    f"Attempt {state.attempt} is {state.state}; record a clearing record"
                    " (ADR 0002 section 6.1) before anything else starts.",
                )
            )

    attempts = view.task_attempts(task)
    task_fires = sum(len(s.fires) for s in attempts)
    if task_fires >= limits.fires_per_task:
        blocks.append(
            Notice(
                "task-fire-cap", f"{task} has used {task_fires} of {limits.fires_per_task} fires."
            )
        )
    elif task_fires >= limits.fires_per_task_alert_at:
        alerts.append(
            Notice("task-fires-high", f"{task} has used {task_fires} of {limits.fires_per_task}.")
        )

    if refire_of is None:
        run = _next_attempt(view, task, attempts, limits, blocks, alerts)
    else:
        run = _refire(view, task, digest, refire_of, limits, blocks)
    return Decision(task, run, tuple(blocks), tuple(alerts))


def _check_snapshot(
    view: LedgerView, now: datetime, limits: Limits, blocks: list[Notice], alerts: list[Notice]
) -> None:
    snap = view.snapshot
    if snap is None:
        blocks.append(Notice("usage-snapshot-missing", "Record a usage snapshot first."))
        return
    if now - snap.taken_at > limits.snapshot_max_age:
        blocks.append(
            Notice(
                "usage-snapshot-stale", f"Latest usage snapshot is from {snap.taken_at:%Y-%m-%d}."
            )
        )
    if snap.weekly_pct >= limits.usage_stop_pct:
        blocks.append(Notice("usage-high", f"Weekly usage is at {snap.weekly_pct:g}%."))
    elif snap.weekly_pct >= limits.usage_alert_pct:
        alerts.append(Notice("usage-high", f"Weekly usage is at {snap.weekly_pct:g}%."))
    if snap.credits_spent > 0:
        alerts.append(
            Notice(
                "usage-credits-spent", "The snapshot shows usage-credit spend; turn credits off."
            )
        )


def _check_window(
    view: LedgerView, now: datetime, limits: Limits, blocks: list[Notice], alerts: list[Notice]
) -> None:
    since = now - limits.fire_window
    recent = sum(1 for f in view.all_fires if f.at > since)
    if recent >= limits.fires_per_window:
        blocks.append(
            Notice("window-fire-cap", f"{recent} of {limits.fires_per_window} fires in 7 days.")
        )
    elif recent >= limits.fires_per_window_alert_at:
        alerts.append(
            Notice("window-fires-high", f"{recent} of {limits.fires_per_window} fires in 7 days.")
        )


def _next_attempt(
    view: LedgerView,
    task: TaskId,
    attempts: list[AttemptState],
    limits: Limits,
    blocks: list[Notice],
    alerts: list[Notice],
) -> RunId | None:
    used = len(attempts)
    number = (attempts[-1].attempt.number if attempts else 0) + 1
    if used >= limits.attempts_per_task:
        blocks.append(
            Notice("attempt-cap", f"{task} has used {used} of {limits.attempts_per_task} attempts.")
        )
        return None
    if used + 1 >= limits.attempts_alert_at:
        alerts.append(
            Notice(
                "attempts-high",
                f"This is attempt {used + 1} of {limits.attempts_per_task} for {task}.",
            )
        )
    attempt = AttemptId(task, number)
    if number > 1 and attempt not in view.repairs:
        blocks.append(
            Notice(
                "repair-not-authorized",
                f"Attempt {number} needs Rolando's repair authorization naming the failure.",
            )
        )
    return RunId(attempt, 1)


def _refire(
    view: LedgerView,
    task: TaskId,
    digest: ContractDigest,
    prior: RunId,
    limits: Limits,
    blocks: list[Notice],
) -> RunId | None:
    state = view.attempts.get(prior.attempt)
    if prior.attempt.task != task or state is None or state.last_fire is None:
        blocks.append(Notice("refire-not-allowed", f"{prior} is not a recorded fire of {task}."))
        return None
    last = state.last_fire
    if last.run != prior:
        blocks.append(
            Notice("refire-not-allowed", f"{prior} is not {prior.attempt}'s latest fire.")
        )
        return None
    if last.outcome is not LaunchOutcome.NOT_LAUNCHED:
        outcome = "no result yet" if last.outcome is None else last.outcome.value
        blocks.append(
            Notice(
                "refire-not-allowed",
                f"{prior} ended {outcome}; only a definite not-launched can be fired again.",
            )
        )
        return None
    if len(state.fires) >= limits.fires_per_attempt:
        blocks.append(
            Notice(
                "refire-not-allowed",
                f"{prior.attempt} has used {len(state.fires)} of {limits.fires_per_attempt} fires;"
                " going on needs a new attempt.",
            )
        )
        return None
    if state.digest != digest.value:
        blocks.append(Notice("refire-not-allowed", "A re-fire must use the attempt's contract."))
        return None
    if prior not in view.refires:
        blocks.append(
            Notice("refire-not-authorized", f"Re-firing needs Rolando's decision naming {prior}.")
        )
    return RunId(prior.attempt, prior.fire + 1)


def run_alerts(
    stored: Sequence[StoredEvent], now: datetime, limits: Limits = PILOT_LIMITS
) -> list[Notice]:
    """Advisory wall-time alerts for runs that may still be going (G-C9).

    They never stop anything. An overdue run keeps holding the single lane.
    """
    view = LedgerView.build(stored)
    alerts = []
    for state in view.attempts.values():
        last = state.last_fire
        if last is None or not state.unresolved:
            continue
        elapsed = now - last.at
        where = last.session_url or "the routine's run list"
        if elapsed >= limits.run_overdue_after:
            code = "run-overdue"
        elif elapsed >= limits.run_alert_after:
            code = "run-long"
        else:
            continue
        alerts.append(
            Notice(
                code,
                f"{last.run} has been going {_minutes(elapsed)} min ({state.state}). Advisory only:"
                f" check {where} and follow controller/attempts/stop-procedure.md.",
            )
        )
    return alerts


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


NEXT_DECISION = {
    "attempt-cap": (
        "Close the task, or approve a fresh contract with a new budget"
        " (an exception under ADR 0001 section 4)."
    ),
    "task-fire-cap": (
        "Close the task, or approve a fresh contract with a new budget"
        " (an exception under ADR 0001 section 4)."
    ),
    ev.SUBSCRIPTION_EXHAUSTED: (
        "Check the usage page; once the window resets, record a fresh snapshot and run"
        " `factory resume` with a note."
    ),
}


def escalation_data(
    view: LedgerView, task: TaskId | None, dedup: str, reason: str, last_error: str | None = None
) -> dict[str, object]:
    """One escalation: prior attempts, current state, last error and next decision (G-C5)."""
    attempts = view.task_attempts(task) if task is not None else []
    if last_error is None:
        last_error = "none recorded"
        for state in reversed(attempts):
            failed = [f for f in state.fires if f.outcome not in (None, LaunchOutcome.LAUNCHED)]
            if failed:
                f = failed[-1]
                last_error = f"{f.run}: {f.outcome.value} (HTTP {f.http_status}) {f.reason}".strip()
                break
    return {
        "dedup": dedup,
        "reason": reason,
        "prior_attempts": [
            {
                "attempt": str(s.attempt),
                "state": s.state,
                "fires": [
                    {
                        "run": str(f.run),
                        "at": f.at.isoformat(),
                        "outcome": f.outcome.value if f.outcome else None,
                        "http_status": f.http_status,
                    }
                    for f in s.fires
                ],
            }
            for s in attempts
        ],
        "current_state": (
            f"{task}: {attempts[-1].state} after {len(attempts)} attempt(s)"
            if attempts
            else "no attempts on record"
        ),
        "last_error": last_error,
        "next_decision": NEXT_DECISION.get(reason, "Rolando decides how to go on."),
    }
