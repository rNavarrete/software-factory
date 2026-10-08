"""The ``python3 -m controller`` command (ENG-176), driven through ``cli.main``
with an injected Controller built on fakes. Nothing here touches the network,
the Keychain or ``gh``.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from controller import cli
from controller import contract as contracts
from controller.approval import Approvals
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.dispatch import Dispatcher
from controller.interfaces import AttemptId, TaskId
from controller.recovery import Recovery, State
from tests.test_approval import example
from tests.test_attempts import LOST, MemoryLedger, launched, not_launched
from tests.test_dispatch import BOT, KEY, NOW, START_KEY, TRIG, Confirm, FakeBase, ScriptedAdapter
from tests.test_recovery import FakeGitHub

URL1 = "https://claude.ai/code/cse_1"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.now = NOW
        self.confirm = Confirm()
        self.base = FakeBase()
        self.adapter = ScriptedAdapter([launched(1)])
        self.made = 0
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.contract = example()
        self.task = TaskId(self.contract["task_id"])
        self.a1 = AttemptId(self.task, 1)
        self.path = self.write(self.contract)
        self.controller = self.build()
        self.controller.gate.record_snapshot(
            NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1)
        )

    def write(self, contract, name="contract.json"):
        path = self.dir / name
        path.write_text(json.dumps(contract))
        return path

    def build(self):
        gate = AttemptGate(self.store)
        approvals = Approvals(
            self.store, KEY, confirm=lambda s, c: self.confirm(s, c), os_user="rolando"
        )
        recovery = Recovery(
            self.store,
            approvals,
            FakeGitHub(),
            worker_logins=frozenset({BOT}),
            gate=gate,
            confirm=lambda s, c: True,
        )
        dispatcher = Dispatcher(
            self.store,
            approvals,
            recovery,
            gate,
            self.base,
            routine_id=TRIG,
            adapter=lambda trig, key: self.adapter,
            start_key=lambda trig: START_KEY,
            model_config_version="test",
            now=lambda: self.now,
            sleep=lambda s: None,
        )
        return cli.Controller(self.store, approvals, recovery, gate, dispatcher, lambda: self.now)

    def make(self):
        self.made += 1
        return self.controller

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv), make=self.make)
        return code, out.getvalue()

    def kinds(self, kind):
        return [s.event for s in self.store.events() if s.event.kind == kind]

    # --- dispatch ---

    def test_dispatch_launches_and_exits_0(self):
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 0, out)
        self.assertIn("Started", out)
        self.assertIn(URL1, out)
        self.assertIn("Next:", out)
        self.assertNotIn(START_KEY, out)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_dispatch_again_reports_and_exits_0(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing sent", out)
        self.assertIn("running", out)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_dispatch_declined_exits_1_and_sends_nothing(self):
        self.confirm.answer = False
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1, out)
        self.assertIn("Nothing sent:", out)
        self.assertIn("[not-approved]", out)
        self.assertEqual(self.adapter.requests, [])

    def test_dispatch_refused_lists_each_reason(self):
        self.base.answer = False
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1, out)
        self.assertIn("[base-not-on-main]", out)
        self.assertEqual(self.confirm.calls, [])

    def test_dispatch_not_launched_exits_1(self):
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1, out)
        self.assertIn("Not started (HTTP 429)", out)
        code, out = self.run_cli("status")
        self.assertIn(f"Fire {self.a1}-f1: not-launched", out)
        self.assertNotIn(": launched", out)

    def test_dispatch_unknown_exits_1(self):
        self.adapter = ScriptedAdapter([LOST])
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1, out)
        self.assertIn("Unclear whether", out)
        code, out = self.run_cli("status")
        self.assertIn(f"Fire {self.a1}-f1: launch-outcome-unknown", out)
        self.assertNotIn(": launched", out)

    def test_missing_contract_file_exits_2_without_a_controller(self):
        code, out = self.run_cli("dispatch", str(self.dir / "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("Can't read the contract", out)
        self.assertEqual(self.made, 0)

    def test_unreadable_contract_file_exits_2_without_a_controller(self):
        bad = self.dir / "bad.json"
        bad.write_text("{not json")
        code, out = self.run_cli("approve", str(bad))
        self.assertEqual(code, 2)
        self.assertEqual(self.made, 0)

    # --- status ---

    def test_status_with_nothing_on_record(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("No tasks on record.", out)

    def test_status_lists_attempts(self):
        self.run_cli("dispatch", str(self.path))
        for argv in (("status",), ("status", str(self.task))):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 0, out)
            self.assertIn(f"{self.task}: running", out)
            self.assertIn(f"  {self.a1}: running.", out)
            self.assertIn(URL1, out)

    def test_status_names_each_fire_and_its_answer(self):
        code, out = self.run_cli("status")
        self.assertIn("Usage reading:", out)
        self.assertIn("Fires on record: 0", out)
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Fire {self.a1}-f1: launched {URL1}", out)
        self.assertIn("Fires on record: 1", out)

    # --- latest attempt by default ---

    def test_reconcile_picks_the_latest_attempt(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("reconcile", str(self.task))
        self.assertEqual(code, 0, out)
        self.assertIn(f"  {self.a1}: running.", out)

    def test_reconcile_of_an_unknown_task_exits_2(self):
        code, out = self.run_cli("reconcile", "no-such-task")
        self.assertEqual(code, 2)
        self.assertIn("no attempts on record", out)

    def test_clear_then_close_pick_the_latest_attempt(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("clear", str(self.task), URL1)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.a1} cleared", out)
        (cleared,) = self.kinds(ev.ATTEMPT_CLEARED)
        self.assertEqual(cleared.attempt, self.a1)
        code, out = self.run_cli("close", str(self.task), "failed", "tests failed")
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.a1} closed as failed", out)
        status = self.controller.recovery.attempt_status(self.a1, self.now)
        self.assertEqual(status.state, State.FAILED)

    def test_close_with_an_explicit_attempt(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("close", str(self.task), "canceled", "why", "--attempt", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("Not done", out)

    def test_clear_with_a_wrong_url_is_not_done(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("clear", str(self.task), "https://claude.ai/code/cse_9")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.kinds(ev.ATTEMPT_CLEARED), [])

    # --- other commands ---

    def test_approve_then_dispatch_asks_nothing(self):
        code, out = self.run_cli("approve", str(self.path))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.adapter.requests, [])
        self.confirm.calls.clear()
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.confirm.calls, [])

    def test_revoke_then_dispatch_is_refused(self):
        self.run_cli("approve", str(self.path))
        code, out = self.run_cli("revoke", str(self.path), "not now")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1, out)
        self.assertIn("[approval-revoked]", out)
        self.assertEqual(self.adapter.requests, [])

    def test_hold_and_resume(self):
        code, _ = self.run_cli("hold", "manual", "pause")
        self.assertEqual(code, 0)
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("[hold]", out)
        code, _ = self.run_cli("resume", "back on")
        self.assertEqual(code, 0)
        code, out = self.run_cli("dispatch", str(self.path))
        self.assertEqual(code, 0, out)

    def test_snapshot_is_recorded(self):
        code, _ = self.run_cli("snapshot", "5", "10", "0")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.kinds(ev.USAGE_SNAPSHOT)), 2)

    def test_repair_names_the_next_attempt(self):
        self.run_cli("dispatch", str(self.path))
        code, out = self.run_cli("repair", str(self.path), "tests failed")
        self.assertEqual(code, 0, out)
        self.assertIn("Attempt 2 allowed", out)
        self.assertEqual(self.kinds(ev.REPAIR_AUTHORIZED)[0].attempt, AttemptId(self.task, 2))

    def test_digest_of_the_file_is_what_is_approved(self):
        self.run_cli("approve", str(self.path))
        (decision,) = [s.event for s in self.store.events() if s.event.kind == "human-decision"]
        self.assertEqual(decision.data["digest"], contracts.digest(self.contract).value)


if __name__ == "__main__":
    unittest.main()
