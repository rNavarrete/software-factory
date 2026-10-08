"""ENG-146's attempt-gate tests, run on the durable SQLite ledger (ENG-147).

Every test the gate passes on its in-memory store runs again here on disk,
with ``restart`` closing the store and opening the file again the way a new
controller process would. That is what proves the counts, holds and
escalation dedup survive a restart (G-C3, G-C13).
"""

import tempfile
from datetime import timedelta
from pathlib import Path

from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.interfaces import AttemptId, LedgerEvent, LedgerLocked, TaskId
from controller.ledger import InvalidEvent, SqliteLedgerStore
from tests.test_attempts import (
    DIGEST,
    LOST,
    NOW,
    OTHER,
    TASK,
    AttemptGateTests,
    launched,
    not_launched,
)


class SqliteAttemptGateTests(AttemptGateTests):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._dir = Path(tmp.name)
        self._opened = 0
        super().setUp()

    def make_store(self):
        # A separate ledger file per call, as the in-memory version gives.
        self._opened += 1
        return self._open(self._dir / f"ledger-{self._opened}" / "ledger.db")

    def restart(self, store):
        store.close()
        return self._open(store.path)

    def _open(self, path):
        store = SqliteLedgerStore(path)
        self.addCleanup(store.close)
        return store

    def test_reserve_writes_nothing_when_another_process_holds_the_lock(self):
        other = self._open(self.store.path)
        before = len(self.store.events())
        with other.writer_lock():
            with self.assertRaises(LedgerLocked):
                self.gate.reserve(TASK, DIGEST, NOW)
        self.assertEqual(len(self.store.events()), before)

    def test_unresolved_attempts_still_block_after_restart(self):
        self.run_attempt(TASK, LOST)
        self.authorize_repair(TASK, 2)
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("unresolved-attempt")
        self.refused("unresolved-attempt", task=OTHER)

    def test_crash_between_reserve_and_result_blocks_after_restart(self):
        self.gate.reserve(TASK, DIGEST, NOW)
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("unresolved-attempt", task=OTHER)

    def test_hold_survives_restart(self):
        self.gate.hold("upkeep cap", "over 2 hours this week", NOW)
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("hold")

    def test_rate_limit_wait_survives_restart(self):
        self.run_attempt(TASK, not_launched(429, retry_after=600))
        self.store = self.restart(self.store)
        self.gate = AttemptGate(self.store)
        self.refused("rate-limited", task=OTHER, at=NOW + timedelta(minutes=5))

    def test_launched_run_keeps_its_session_and_rejections_have_none(self):
        # AC 3: rejected and lost launches are recorded with no session id.
        self.run_attempt(TASK, not_launched())
        self.run_attempt(TASK, LOST)
        self.clear(TASK, 2, ev.ClearingBasis.UNRESOLVED_ACCEPTED, "never found")
        self.run_attempt(TASK, launched(3))
        self.store = self.restart(self.store)
        results = [e.data for e in self.kinds(ev.FIRE_RESULT, TASK)]
        self.assertEqual(
            [(r["outcome"], r["session_id"]) for r in results],
            [("not-launched", None), ("launch-outcome-unknown", None), ("launched", "cse_3")],
        )

    # The two tests below replace inherited ones. In memory, malformed decision
    # records are stored and the gate ignores them. On disk the ledger refuses
    # them at write time, so they never exist; the lane stays just as blocked.

    def test_decisions_without_a_named_person_grant_nothing(self):
        first = self.run_attempt(TASK, not_launched(400))
        for event in [
            LedgerEvent(ev.REFIRE_AUTHORIZED, NOW, TASK, first.attempt, first, {"by": None}),
            LedgerEvent(ev.REFIRE_AUTHORIZED, NOW, TASK, first.attempt, first, {"by": " "}),
            LedgerEvent(
                ev.REPAIR_AUTHORIZED,
                NOW,
                TASK,
                AttemptId(TASK, 2),
                data={"failure": None, "by": None},
            ),
            LedgerEvent(
                ev.REPAIR_AUTHORIZED, NOW, TASK, AttemptId(TASK, 2), data={"by": "Rolando"}
            ),
            LedgerEvent(ev.REPAIR_AUTHORIZED, NOW, TASK, data={"failure": "x", "by": "R"}),
        ]:
            with self.assertRaises(InvalidEvent):
                self.append(event)
        self.refused("refire-not-authorized", refire_of=first)
        self.refused("repair-not-authorized")
        run = self.run_attempt(OTHER, launched())
        with self.assertRaises(InvalidEvent):
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

    def test_malformed_clearing_record_does_not_clear(self):
        self.run_attempt(TASK, launched())
        for data in [
            {"basis": "completed", "by": "Rolando"},
            {"basis": "routine-deleted", "session_url": "x", "by": "R"},
            {"basis": "write-access-removed", "session_url": "x", "by": "R"},
        ]:
            with self.assertRaises(InvalidEvent, msg=data):
                self.append(
                    LedgerEvent(ev.ATTEMPT_CLEARED, NOW, TASK, AttemptId(TASK, 1), data=data)
                )
        self.assertEqual(self.kinds(ev.ATTEMPT_CLEARED), [])
        self.refused("unresolved-attempt", task=OTHER)


# Imported only to subclass; keep unittest from running the in-memory copy twice.
del AttemptGateTests
