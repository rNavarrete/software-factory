"""The attempt gate: reserve before every fire, record every result.

Dispatch (ENG-176) calls ``reserve`` before it calls the runtime adapter and
``record_launch`` right after. Each call takes the ledger's writer lock, reads
the whole ledger, decides, and appends in one transaction, so two callers can
never both pass the same check.

The gate holds no state between calls. A fresh gate on the same store after a
restart sees exactly what the old one saw (G-C3).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from controller.attempts import events as ev
from controller.attempts.limits import PILOT_LIMITS, Limits
from controller.attempts.policy import (
    ESCALATING_BLOCKS,
    Decision,
    LedgerView,
    Notice,
    check_dispatch,
    escalation_data,
    run_alerts,
)
from controller.interfaces import (
    ContractDigest,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    LedgerStore,
    RunId,
    TaskId,
)

# docs/limits.md section 5: a non-200 body that mentions one of these limits.
_EXHAUSTED_RE = re.compile(r"\b(usage|session|weekly|spend(ing)?)[\s_-]+limit", re.IGNORECASE)


class DispatchRefused(Exception):
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__("; ".join(f"{b.code}: {b.detail}" for b in decision.blocks))


class AttemptGate:
    def __init__(self, store: LedgerStore, limits: Limits = PILOT_LIMITS) -> None:
        self._store = store
        self._limits = limits

    def decide(
        self,
        task: TaskId,
        digest: ContractDigest,
        now: datetime,
        *,
        refire_of: RunId | None = None,
        automated: bool = False,
    ) -> Decision:
        """Read-only preview of what ``reserve`` would do. Writes nothing."""
        return check_dispatch(
            self._store.events(),
            task,
            digest,
            now,
            refire_of=refire_of,
            automated=automated,
            limits=self._limits,
        )

    def reserve(
        self,
        task: TaskId,
        digest: ContractDigest,
        now: datetime,
        *,
        refire_of: RunId | None = None,
        automated: bool = False,
    ) -> RunId:
        """Count the attempt and the fire before launching (G-C1).

        Returns the run to fire. If anything blocks it, records the refusal (and,
        at a cap, one escalation) and raises DispatchRefused.
        """
        with self._store.writer_lock():
            stored = self._store.events()
            decision = check_dispatch(
                stored,
                task,
                digest,
                now,
                refire_of=refire_of,
                automated=automated,
                limits=self._limits,
            )
            if not decision.allowed:
                self._store.append(*self._refusal(stored, decision, now))
                raise DispatchRefused(decision)
            run = decision.run
            assert run is not None
            new = []
            if refire_of is None:
                new.append(ev.attempt_reserved(run.attempt, digest, now))
            new.append(
                LedgerEvent(
                    ev.FIRE_INTENT,
                    now,
                    task,
                    run.attempt,
                    run,
                    {
                        "refire_of": str(refire_of) if refire_of else None,
                        "alerts": [a.as_data() for a in decision.alerts],
                    },
                )
            )
            self._store.append(*new)
            return run

    def _refusal(self, stored, decision: Decision, now: datetime) -> list[LedgerEvent]:
        out = [
            LedgerEvent(
                ev.DISPATCH_REFUSED,
                now,
                decision.task,
                data={
                    "blocks": [b.as_data() for b in decision.blocks],
                    "alerts": [a.as_data() for a in decision.alerts],
                },
            )
        ]
        view = LedgerView.build(stored)
        for block in decision.blocks:
            if block.code not in ESCALATING_BLOCKS:
                continue
            dedup = f"{decision.task}:{block.code}"
            if dedup in view.escalations:
                continue
            view.escalations.add(dedup)
            out.append(
                LedgerEvent(
                    ev.ESCALATION,
                    now,
                    decision.task,
                    data=escalation_data(view, decision.task, dedup, block.code),
                )
            )
        return out

    def record_launch(self, run: RunId, result: LaunchResult, now: datetime) -> None:
        """Record what the adapter returned for ``run``.

        The launch state comes from the HTTP status alone (ADR 0002 section 6).
        A 429 sets a factory-wide earliest next fire; a non-200 body naming a
        usage limit also sets the subscription-exhausted hold with one escalation.
        """
        with self._store.writer_lock():
            stored = self._store.events()
            view = LedgerView.build(stored)
            fire = next((f for f in view.all_fires if f.run == run), None)
            if fire is None:
                raise ValueError(f"{run} was never reserved")
            if fire.outcome is not None:
                raise ValueError(f"{run} already has a result")
            new = [
                LedgerEvent(
                    ev.FIRE_RESULT,
                    now,
                    run.attempt.task,
                    run.attempt,
                    run,
                    {
                        "outcome": result.outcome.value,
                        "reason": _reason(result),
                        "http_status": result.http_status,
                        "session_id": result.session_id,
                        "session_url": result.session_url,
                        "retry_after_seconds": result.retry_after_seconds,
                        "response_body": result.response_body,
                        "detail": result.detail,
                    },
                )
            ]
            if result.http_status == 429:
                if result.retry_after_seconds is not None:
                    wait = timedelta(seconds=max(result.retry_after_seconds, 0))
                else:
                    streak = view.trailing_rate_limits() + 1
                    wait = min(
                        self._limits.rate_limit_default_wait * 2 ** (streak - 1),
                        self._limits.rate_limit_max_wait,
                    )
                new.append(
                    LedgerEvent(
                        ev.RATE_LIMIT_WAIT,
                        now,
                        data={"not_before": (now + wait).isoformat(), "run": str(run)},
                    )
                )
            if (
                result.http_status != 200
                and result.response_body
                and _EXHAUSTED_RE.search(result.response_body)
            ):
                new += self._exhausted(view, run, result, now)
            self._store.append(*new)

    def _exhausted(
        self, view: LedgerView, run: RunId, result: LaunchResult, now: datetime
    ) -> list[LedgerEvent]:
        if view.hold is not None and view.hold.get("reason") == ev.SUBSCRIPTION_EXHAUSTED:
            return []
        view.hold = {"reason": ev.SUBSCRIPTION_EXHAUSTED}
        dedup = f"{ev.SUBSCRIPTION_EXHAUSTED}:{now.isoformat()}"
        error = f"{run}: HTTP {result.http_status}: {result.response_body}"
        return [
            LedgerEvent(
                ev.HOLD_SET,
                now,
                data={
                    "reason": ev.SUBSCRIPTION_EXHAUSTED,
                    "note": f"Set automatically from the response to {run}.",
                    "automatic": True,
                },
            ),
            LedgerEvent(
                ev.ESCALATION,
                now,
                run.attempt.task,
                data=escalation_data(
                    view, run.attempt.task, dedup, ev.SUBSCRIPTION_EXHAUSTED, error
                ),
            ),
        ]

    def hold(self, reason: str, note: str, now: datetime) -> None:
        """``factory hold``: every dispatch refuses until ``resume``."""
        if not reason.strip():
            raise ValueError("a hold needs a reason")
        with self._store.writer_lock():
            self._store.append(
                LedgerEvent(
                    ev.HOLD_SET, now, data={"reason": reason, "note": note, "automatic": False}
                )
            )

    def resume(self, note: str, now: datetime) -> None:
        """``factory resume``: clears the hold. Needs Rolando's note."""
        if not note.strip():
            raise ValueError("resuming needs a note saying why")
        with self._store.writer_lock():
            if LedgerView.build(self._store.events()).hold is None:
                raise ValueError("the factory is not on hold")
            self._store.append(LedgerEvent(ev.HOLD_CLEARED, now, data={"note": note}))

    def record_snapshot(
        self,
        taken_at: datetime,
        session_pct: float,
        weekly_pct: float,
        credits_spent: float,
        now: datetime,
    ) -> None:
        """``factory snapshot``: Rolando's reading of claude.ai/settings/usage."""
        if taken_at.tzinfo is None:
            raise ValueError("snapshot time must be timezone-aware")
        if taken_at > now:
            raise ValueError("a snapshot cannot be from the future")
        for pct in (session_pct, weekly_pct):
            if not 0 <= pct <= 100:
                raise ValueError("percentages are 0 to 100")
        if credits_spent < 0:
            raise ValueError("credit spend cannot be negative")
        with self._store.writer_lock():
            self._store.append(
                LedgerEvent(
                    ev.USAGE_SNAPSHOT,
                    now,
                    data={
                        "taken_at": taken_at.isoformat(),
                        "session_pct": session_pct,
                        "weekly_pct": weekly_pct,
                        "credits_spent": credits_spent,
                        "source": "manual claude.ai/settings/usage",
                        "scope": "account-wide",
                        "attribution": "approximate",
                    },
                )
            )

    def run_alerts(self, now: datetime) -> list[Notice]:
        return run_alerts(self._store.events(), now, self._limits)


def _reason(result: LaunchResult) -> str:
    """Keeps infrastructure rejections and lost responses apart (G-C10)."""
    if result.outcome is LaunchOutcome.LAUNCHED:
        return "launched"
    if result.outcome is LaunchOutcome.NOT_LAUNCHED:
        return "rate-limited" if result.http_status == 429 else "rejected"
    return "no-response" if result.http_status is None else "unclear-response"
