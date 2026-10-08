"""Attempt caps, fire caps, usage gates and holds (ENG-146, docs/limits.md).

``AttemptGateTests`` runs against a small in-memory LedgerStore. The durable
SQLite store (ENG-147) should subclass it, override ``make_store`` and
``restart``, and pass the same tests, so restart survival is proven on disk.
"""

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller.adapter.fake import FakeRuntimeAdapter, FakeStep, ScriptExhausted
from controller.attempts import PILOT_LIMITS, AttemptGate, DispatchRefused
from controller.attempts import events as ev
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
    StoredEvent,
    TaskId,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
TASK = TaskId("pilot-1")
OTHER = TaskId("pilot-2")
DIGEST = ContractDigest.of(b"contract")
ROOT = Path(__file__).resolve().parent.parent


class MemoryLedger:
    """An append-only LedgerStore kept in memory, for tests only."""

    def __init__(self) -> None:
        self._events: list[StoredEvent] = []
        self._locked = False
        self.locked_elsewhere = False

    def append(self, *events: LedgerEvent) -> list[StoredEvent]:
        if not self._locked:
            raise AssertionError("append without the writer lock")
        start = len(self._events) + 1
        stored = [StoredEvent(start + i, e) for i, e in enumerate(events)]
        self._events.extend(stored)
        return stored

    def events(self, task: TaskId | None = None) -> list[StoredEvent]:
        return [s for s in self._events if task is None or s.event.task == task]

    @contextmanager
    def writer_lock(self):
        if self._locked or self.locked_elsewhere:
            raise LedgerLocked("held")
        self._locked = True
        try:
            yield
        finally:
            self._locked = False


def not_launched(status: int = 400, body: str = "", retry_after: int | None = None):
    return LaunchResult(
        LaunchOutcome.NOT_LAUNCHED,
        http_status=status,
        response_body=body,
        retry_after_seconds=retry_after,
    )


def launched(n: int = 1):
    return LaunchResult(
        LaunchOutcome.LAUNCHED,
        http_status=200,
        session_id=f"cse_{n}",
        session_url=f"https://claude.ai/code/cse_{n}",
    )


LOST = LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, detail="timeout")


class AttemptGateTests(unittest.TestCase):
    def make_store(self) -> LedgerStore:
        return MemoryLedger()

    def restart(self, store: LedgerStore) -> LedgerStore:
        """Reopen the same ledger as a new controller process would."""
        return store

    def setUp(self):
        self.store = self.make_store()
        self.gate = AttemptGate(self.store)
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))

    # --- helpers ---

    def kinds(self, kind, task=None):
        return [s.event for s in self.store.events(task) if s.event.kind == kind]

    def append(self, *events):
        with self.store.writer_lock():
            self.store.append(*events)

    def authorize_repair(self, task, number, at=NOW):
        self.append(ev.repair_authorized(AttemptId(task, number), "CI red", "Rolando", at))

    def clear(self, task, number, basis=ev.ClearingBasis.COMPLETED, evidence="https://x/s"):
        self.append(ev.attempt_cleared(AttemptId(task, number), basis, evidence, "Rolando", NOW))

    def run_attempt(self, task, result, at=NOW, gate=None):
        """Reserve the next attempt (authorizing a repair if needed) and record ``result``."""
        gate = gate or self.gate
        number = len(self.kinds(ev.ATTEMPT_RESERVED, task)) + 1
        if number > 1:
            self.authorize_repair(task, number, at)
        run = gate.reserve(task, DIGEST, at)
        gate.record_launch(run, result, at)
        return run

    def refused(self, *codes, task=TASK, at=NOW, **kw):
        with self.assertRaises(DispatchRefused) as caught:
            self.gate.reserve(task, DIGEST, at, **kw)
        got = {b.code for b in caught.exception.decision.blocks}
        for code in codes:
            self.assertIn(code, got)
        return caught.exception.decision

    # --- G-C1: reserved and counted before launch ---

    def test_attempt_is_recorded_before_the_adapter_is_called(self):
        seen = []

        class Spy(FakeRuntimeAdapter):
            def launch(inner, request):
                seen.append((len(self.kinds(ev.ATTEMPT_RESERVED)), len(self.kinds(ev.FIRE_INTENT))))
                return super().launch(request)

        adapter = Spy([FakeStep.launch()])
        run = self.gate.reserve(TASK, DIGEST, NOW)
        result = adapter.launch(LaunchRequest(run, DIGEST, "payload"))
        self.gate.record_launch(run, result, NOW)
        self.assertEqual(seen, [(1, 1)])
        self.assertEqual(run, RunId(AttemptId(TASK, 1), 1))

    def test_forced_failures_never_launch_beyond_the_cap(self):
        adapter = FakeRuntimeAdapter([FakeStep.rejected(400)] * 3)
        for number in (1, 2, 3):
            if number > 1:
                self.authorize_repair(TASK, number)
            run = self.gate.reserve(TASK, DIGEST, NOW)
            self.gate.record_launch(run, adapter.launch(LaunchRequest(run, DIGEST, "p")), NOW)
        self.authorize_repair(TASK, 4)
        self.refused("attempt-cap")
        self.refused("attempt-cap")
        self.assertEqual(adapter.remaining_steps, 0)
        with self.assertRaises(ScriptExhausted):
            adapter.launch(LaunchRequest(RunId(AttemptId(TASK, 4), 1), DIGEST, "p"))
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 3)

    def test_attempt_two_needs_a_repair_authorization(self):
        self.run_attempt(TASK, not_launched())
        self.refused("repair-not-authorized")
        self.authorize_repair(TASK, 2)
        self.assertEqual(self.gate.reserve(TASK, DIGEST, NOW).attempt.number, 2)

    def test_an_authorization_for_another_attempt_number_does_not_count(self):
        self.run_attempt(TASK, not_launched())
        self.authorize_repair(TASK, 3)
        self.refused("repair-not-authorized")

    # --- G-C10: infrastructure rejections and lost responses stay distinct ---

    def test_rejection_and_lost_response_are_different_outcomes(self):
        self.run_attempt(TASK, not_launched(400))
        self.run_attempt(OTHER, LOST)
        results = {
            s.event.task: s.event.data
            for s in self.store.events()
            if s.event.kind == ev.FIRE_RESULT
        }
        self.assertEqual(results[TASK]["outcome"], "not-launched")
        self.assertEqual(results[TASK]["reason"], "rejected")
        self.assertEqual(results[OTHER]["outcome"], "launch-outcome-unknown")
        self.assertEqual(results[OTHER]["reason"], "no-response")
        for data in results.values():
            self.assertNotEqual(data["outcome"], "launched")

    def test_refire_after_not_launched_keeps_the_attempt_number(self):
        first = self.run_attempt(TASK, not_launched(400))
        self.refused("refire-not-authorized", refire_of=first)
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        second = self.gate.reserve(TASK, DIGEST, NOW, refire_of=first)
        self.assertEqual(second, RunId(AttemptId(TASK, 1), 2))
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 2)

    def test_third_fire_of_an_attempt_is_refused(self):
        first = self.run_attempt(TASK, not_launched(400))
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        second = self.gate.reserve(TASK, DIGEST, NOW, refire_of=first)
        self.gate.record_launch(second, not_launched(400), NOW)
        self.append(ev.refire_authorized(second, "Rolando", NOW))
        self.refused("refire-not-allowed", refire_of=second)

    def test_refire_after_unknown_outcome_is_refused(self):
        first = self.run_attempt(TASK, LOST)
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        self.refused("refire-not-allowed", refire_of=first)

    def test_refire_must_use_the_attempts_contract(self):
        first = self.run_attempt(TASK, not_launched(400))
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        with self.assertRaises(DispatchRefused):
            self.gate.reserve(TASK, ContractDigest.of(b"other"), NOW, refire_of=first)

    # --- G-C4: CI events are not attempts ---

    def test_ci_events_on_one_commit_do_not_count(self):
        run = self.run_attempt(TASK, launched())
        for job in ("lint", "test", "test"):
            self.append(
                LedgerEvent("ci-check", NOW, TASK, run.attempt, run, {"sha": "abc", "job": job})
            )
        self.clear(TASK, 1)
        self.authorize_repair(TASK, 2)
        self.assertEqual(self.gate.reserve(TASK, DIGEST, NOW).attempt.number, 2)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 2)

    # --- G-C5: one escalation at the cap ---

    def test_cap_writes_one_escalation_with_the_full_picture(self):
        for _ in range(3):
            self.run_attempt(TASK, not_launched(403, body="forbidden"))
        self.refused("attempt-cap")
        self.refused("attempt-cap")
        escalations = self.kinds(ev.ESCALATION)
        self.assertEqual(len(escalations), 1)
        data = escalations[0].data
        self.assertEqual(len(data["prior_attempts"]), 3)
        self.assertIn("not-launched", data["current_state"])
        self.assertIn("403", data["last_error"])
        self.assertTrue(data["next_decision"])
        self.assertEqual(len(self.kinds(ev.DISPATCH_REFUSED)), 2)

    def test_task_fire_cap_escalates_once(self):
        for _ in range(3):
            first = self.run_attempt(TASK, not_launched(400))
            self.append(ev.refire_authorized(first, "Rolando", NOW))
            second = self.gate.reserve(TASK, DIGEST, NOW, refire_of=first)
            self.gate.record_launch(second, not_launched(400), NOW)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 6)
        self.refused("task-fire-cap", "attempt-cap")
        self.refused("task-fire-cap", "attempt-cap")
        escalations = self.kinds(ev.ESCALATION)
        self.assertEqual(len(escalations), 1)
        self.assertEqual(set(escalations[0].data["caps"]), {"attempt-cap", "task-fire-cap"})

    # --- G-C3: survives restart ---

    def test_counts_and_escalation_dedup_survive_restart(self):
        self.run_attempt(TASK, not_launched())
        self.run_attempt(TASK, not_launched())
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.run_attempt(TASK, not_launched())
        self.refused("attempt-cap")
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("attempt-cap")
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 3)
        self.assertEqual(len(self.kinds(ev.ESCALATION)), 1)

    def test_weekly_cap_survives_restart(self):
        self.seed_window_fires(12)
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("window-fire-cap", task=TaskId("fresh"))

    # --- G-D4/G-D5: one active attempt; unresolved blocks everything ---

    def test_unknown_outcome_blocks_every_new_launch(self):
        self.run_attempt(TASK, LOST)
        self.authorize_repair(TASK, 2)
        self.refused("unresolved-attempt")
        self.refused("unresolved-attempt", task=OTHER)

    def test_launched_without_a_recorded_end_blocks(self):
        self.run_attempt(TASK, launched())
        self.refused("unresolved-attempt", task=OTHER)

    def test_crash_between_reserve_and_result_blocks(self):
        self.gate.reserve(TASK, DIGEST, NOW)
        self.refused("unresolved-attempt", task=OTHER)

    def test_each_clearing_basis_frees_the_lane(self):
        for i, basis in enumerate(ev.ClearingBasis):
            task = TaskId(f"t{i}")
            self.run_attempt(task, LOST if i % 2 else launched(i))
            self.refused("unresolved-attempt", task=OTHER)
            self.clear(task, 1, basis, "evidence")
        self.assertEqual(self.gate.reserve(OTHER, DIGEST, NOW).attempt.number, 1)

    def test_malformed_clearing_record_does_not_clear(self):
        self.run_attempt(TASK, launched())
        self.append(
            LedgerEvent(
                ev.ATTEMPT_CLEARED,
                NOW,
                TASK,
                AttemptId(TASK, 1),
                data={"basis": "completed", "by": "Rolando"},
            ),
            LedgerEvent(
                ev.ATTEMPT_CLEARED,
                NOW,
                TASK,
                AttemptId(TASK, 1),
                data={"basis": "routine-deleted", "session_url": "x", "by": "R"},
            ),
        )
        self.refused("unresolved-attempt", task=OTHER)

    def test_a_clearing_record_does_not_cover_a_later_refire(self):
        first = self.run_attempt(TASK, not_launched(400))
        self.clear(TASK, 1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, "never found")
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        second = self.gate.reserve(TASK, DIGEST, NOW, refire_of=first)
        self.gate.record_launch(second, LOST, NOW)
        self.refused("unresolved-attempt", task=OTHER)

    def test_decisions_without_a_named_person_grant_nothing(self):
        first = self.run_attempt(TASK, not_launched(400))
        self.append(
            LedgerEvent(ev.REFIRE_AUTHORIZED, NOW, TASK, first.attempt, first, {"by": None}),
            LedgerEvent(
                ev.REPAIR_AUTHORIZED,
                NOW,
                TASK,
                AttemptId(TASK, 2),
                data={"failure": None, "by": None},
            ),
        )
        self.refused("refire-not-authorized", refire_of=first)
        self.refused("repair-not-authorized")
        run = self.run_attempt(OTHER, launched())
        self.append(
            LedgerEvent(
                ev.ATTEMPT_CLEARED,
                NOW,
                OTHER,
                run.attempt,
                data={"basis": "completed", "session_url": "https://x", "by": None},
            )
        )
        self.refused("unresolved-attempt", task=TaskId("third"))

    def test_repair_authorization_does_not_clear_an_unresolved_attempt(self):
        self.run_attempt(TASK, launched())
        self.authorize_repair(TASK, 2)
        self.refused("unresolved-attempt")

    # --- G-C8, G-C13, G-C14: thresholds block new dispatch ---

    def seed_window_fires(self, count, at=NOW - timedelta(days=1)):
        for i in range(count):
            self.run_attempt(TaskId(f"seed-{i}"), not_launched(), at=at)

    def test_weekly_fire_cap_blocks_and_records_an_alert(self):
        self.seed_window_fires(12)
        decision = self.refused("window-fire-cap")
        refusal = self.kinds(ev.DISPATCH_REFUSED)[-1]
        self.assertIn("window-fire-cap", [b["code"] for b in refusal.data["blocks"]])
        self.assertEqual(decision.run.attempt.number, 1)

    def test_weekly_window_rolls(self):
        self.seed_window_fires(12, at=NOW - timedelta(days=7, minutes=1))
        self.gate.reserve(TASK, DIGEST, NOW)

    def test_weekly_alert_before_the_cap(self):
        self.seed_window_fires(9)
        alerts = {a.code for a in self.gate.decide(TASK, DIGEST, NOW).alerts}
        self.assertIn("window-fires-high", alerts)

    def test_usage_snapshot_gates(self):
        store = self.make_store()
        gate = AttemptGate(store)
        self.assertIn(
            "usage-snapshot-missing", {b.code for b in gate.decide(TASK, DIGEST, NOW).blocks}
        )
        gate.record_snapshot(NOW - timedelta(days=8), 0, 10, 0, NOW)
        self.assertIn(
            "usage-snapshot-stale", {b.code for b in gate.decide(TASK, DIGEST, NOW).blocks}
        )
        gate.record_snapshot(NOW, 0, 80, 0, NOW)
        self.assertIn("usage-high", {b.code for b in gate.decide(TASK, DIGEST, NOW).blocks})
        gate.record_snapshot(NOW, 0, 60, 5, NOW)
        decision = gate.decide(TASK, DIGEST, NOW)
        self.assertTrue(decision.allowed)
        self.assertLessEqual(
            {"usage-high", "usage-credits-spent"}, {a.code for a in decision.alerts}
        )

    def test_hold_blocks_until_resume_with_a_note(self):
        self.gate.hold("upkeep cap reached", "2h this week", NOW)
        self.refused("hold")
        with self.assertRaises(ValueError):
            self.gate.resume("  ", NOW)
        self.gate.resume("new week", NOW)
        self.gate.reserve(TASK, DIGEST, NOW)

    def test_hold_cleared_without_a_note_or_reason_does_not_clear(self):
        self.gate.hold("manual", "n", NOW)
        self.append(
            LedgerEvent(ev.HOLD_CLEARED, NOW, data={}),
            LedgerEvent(ev.HOLD_CLEARED, NOW, data={"reason": "manual", "note": " "}),
        )
        self.refused("hold")

    def test_exhaustion_does_not_lift_a_manual_hold(self):
        self.gate.hold("upkeep cap", "2h used", NOW)
        self.gate.resume("check", NOW)
        self.run_attempt(TASK, not_launched(400, body="usage limit reached"))
        self.gate.hold("upkeep cap", "2h used", NOW)
        with self.assertRaises(ValueError):
            self.gate.resume("reset", NOW)
        self.gate.resume("reset", NOW, reason=ev.SUBSCRIPTION_EXHAUSTED)
        self.refused("hold")
        self.gate.resume("new week", NOW)
        self.gate.decide(TASK, DIGEST, NOW)

    def test_future_dated_snapshot_counts_for_nothing(self):
        store = self.make_store()
        gate = AttemptGate(store)
        with store.writer_lock():
            store.append(
                LedgerEvent(
                    ev.USAGE_SNAPSHOT,
                    NOW,
                    data={
                        "taken_at": (NOW + timedelta(days=365)).isoformat(),
                        "session_pct": 0,
                        "weekly_pct": 0,
                        "credits_spent": 0,
                    },
                )
            )
        blocks = {b.code for b in gate.decide(TASK, DIGEST, NOW + timedelta(days=30)).blocks}
        self.assertIn("usage-snapshot-missing", blocks)

    def test_snapshot_rejects_bad_numbers(self):
        for args in ((0, 101, 0), (0, 10, float("nan")), (0, 10, -1)):
            with self.assertRaises(ValueError):
                self.gate.record_snapshot(NOW, *args, NOW)

    def test_absurd_retry_after_is_still_recorded(self):
        self.run_attempt(TASK, not_launched(429, retry_after=10**12))
        self.assertEqual(len(self.kinds(ev.FIRE_RESULT)), 1)
        self.refused("rate-limited", task=OTHER, at=NOW + timedelta(days=300))

    def test_automated_repair_is_refused(self):
        self.refused("automated-repair-deferred", automated=True)

    # --- docs/limits.md section 5: rate limits and exhaustion ---

    def test_429_waits_for_retry_after_factory_wide(self):
        self.run_attempt(TASK, not_launched(429, retry_after=600))
        self.refused("rate-limited", task=OTHER, at=NOW + timedelta(seconds=599))
        self.gate.reserve(OTHER, DIGEST, NOW + timedelta(seconds=600))

    def test_429_without_retry_after_backs_off_doubling_to_two_hours(self):
        waits = []
        for i in range(5):
            at = NOW + timedelta(hours=3 * i)
            self.run_attempt(TaskId(f"r{i}"), not_launched(429), at=at)
            until = datetime.fromisoformat(self.kinds(ev.RATE_LIMIT_WAIT)[-1].data["not_before"])
            waits.append(until - at)
        self.assertEqual(waits, [timedelta(minutes=m) for m in (15, 30, 60, 120, 120)])

    def test_exhaustion_text_sets_one_hold_and_one_escalation(self):
        self.run_attempt(TASK, not_launched(400, body="Weekly limit reached"))
        self.refused("hold", task=OTHER)
        self.gate.resume("window reset", NOW)
        self.assertEqual(len(self.kinds(ev.HOLD_SET)), 1)
        self.assertEqual(len(self.kinds(ev.ESCALATION)), 1)

    def test_exhaustion_on_a_5xx_stays_unknown(self):
        result = LaunchResult(
            LaunchOutcome.OUTCOME_UNKNOWN, http_status=503, response_body="usage limit"
        )
        self.run_attempt(TASK, result)
        self.gate.resume("reset", NOW)
        self.refused("unresolved-attempt", task=OTHER)
        self.assertEqual(self.kinds(ev.FIRE_RESULT)[-1].data["outcome"], "launch-outcome-unknown")

    def test_repeat_exhaustion_while_held_adds_nothing(self):
        first = self.run_attempt(TASK, not_launched(400, body="usage limit"))
        self.assertEqual(len(self.kinds(ev.HOLD_SET)), 1)
        self.gate.resume("check", NOW)
        self.append(ev.refire_authorized(first, "Rolando", NOW))
        second = self.gate.reserve(TASK, DIGEST, NOW, refire_of=first)
        self.gate.hold(ev.SUBSCRIPTION_EXHAUSTED, "by hand", NOW)
        self.gate.record_launch(second, not_launched(400, body="usage limit"), NOW)
        self.assertEqual(len(self.kinds(ev.HOLD_SET)), 2)
        self.assertEqual(len(self.kinds(ev.ESCALATION)), 1)

    # --- G-C9: run time alerts are advisory ---

    def test_run_time_alerts_are_advisory(self):
        self.run_attempt(TASK, launched())
        self.assertEqual(self.gate.run_alerts(NOW + timedelta(minutes=44)), [])
        self.assertEqual(
            [a.code for a in self.gate.run_alerts(NOW + timedelta(minutes=45))], ["run-long"]
        )
        overdue = self.gate.run_alerts(NOW + timedelta(minutes=90))
        self.assertEqual([a.code for a in overdue], ["run-overdue"])
        self.assertIn("Advisory", overdue[0].detail)
        self.assertIn("https://claude.ai/code/cse_1", overdue[0].detail)
        self.clear(TASK, 1)
        self.assertEqual(self.gate.run_alerts(NOW + timedelta(minutes=90)), [])

    # --- recording rules ---

    def test_reserve_writes_nothing_when_another_process_holds_the_lock(self):
        if not isinstance(self.store, MemoryLedger):
            self.skipTest("lock simulation is specific to the memory ledger")
        before = len(self.store.events())
        self.store.locked_elsewhere = True
        with self.assertRaises(LedgerLocked):
            self.gate.reserve(TASK, DIGEST, NOW)
        self.assertEqual(len(self.store.events()), before)

    def test_a_result_is_recorded_once_per_reserved_run(self):
        run = self.run_attempt(TASK, launched())
        with self.assertRaises(ValueError):
            self.gate.record_launch(run, launched(), NOW)
        with self.assertRaises(ValueError):
            self.gate.record_launch(RunId(AttemptId(OTHER, 1), 1), launched(), NOW)

    def test_pilot_limits_match_the_signed_numbers(self):
        limits = PILOT_LIMITS
        self.assertEqual(
            (
                limits.attempts_per_task,
                limits.fires_per_attempt,
                limits.fires_per_task,
                limits.fires_per_window,
                limits.usage_stop_pct,
            ),
            (3, 2, 6, 12, 75),
        )
        self.assertEqual(limits.run_alert_after, timedelta(minutes=45))


class StopProcedureTest(unittest.TestCase):
    def test_procedure_separates_the_three_kinds_of_stop(self):
        text = (ROOT / "controller" / "attempts" / "stop-procedure.md").read_text()
        for heading in (
            "## 1. Stop future launches",
            "## 2. Stop monitoring",
            "## 3. Confirm the running worker has stopped",
        ):
            self.assertIn(heading, text)
        self.assertIn("advisory", text.lower())


if __name__ == "__main__":
    unittest.main()
