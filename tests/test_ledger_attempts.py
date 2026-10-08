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
from controller.interfaces import LedgerLocked
from controller.ledger import SqliteLedgerStore
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


# Imported only to subclass; keep unittest from running the in-memory copy twice.
del AttemptGateTests
