import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from controller import contract as contract_format
from controller.adapter import qualify, routine
from controller.approval import Approvals, StaticKey
from controller.attempts import PILOT_LIMITS, AttemptGate
from controller.attempts import events as ev
from controller.interfaces import LaunchOutcome, LaunchResult, TaskId
from controller.ledger import SqliteLedgerStore
from controller.recovery import Recovery
from tests.test_approval import yes
from tests.test_attempts import MemoryLedger
from tests.test_recovery import FakeGitHub

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"k" * 32)
URL = "https://claude.ai/code/session_01Q"


def launched():
    return LaunchResult(
        LaunchOutcome.LAUNCHED, http_status=200, session_id="session_01Q", session_url=URL
    )


class FakeAdapter:
    """Records what would have been sent, and answers with ``answer``."""

    def __init__(self, test, answer):
        self.test = test
        self.answer = answer

    def _send(self, how, text):
        # The intent must already be on the ledger when the request goes out.
        self.test.intents_at_send.append(self.test.count(ev.FIRE_INTENT))
        self.test.sent.append((how, text))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def launch(self, request):
        return self._send("launch", request.text)

    def _post(self, text):
        return self._send("post", text)


class QualifierTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.now = NOW
        self.sent = []
        self.intents_at_send = []
        self.answer = launched()
        self.gate = AttemptGate(self.store)
        self.approvals = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        self.recovery = Recovery(
            self.store,
            self.approvals,
            FakeGitHub(),
            worker_logins=frozenset({"rnavarrete-factory-bot"}),
            gate=self.gate,
            confirm=yes,
        )
        self.q = qualify.Qualifier(
            self.store,
            self.approvals,
            self.recovery,
            self.gate,
            adapter=lambda trig_id, key: FakeAdapter(self, self.answer),
            key=lambda step, trig_id: "sk-ant-oat01-test",
            now=lambda: self.now,
        )

    def count(self, kind, task=None):
        return sum(
            1
            for s in self.store.events()
            if s.event.kind == kind and (task is None or s.event.task == TaskId(task))
        )

    def ready(self, *steps):
        self.q.snapshot(10, 20, 0)
        for step in steps:
            self.q.approve(step)

    def test_fixtures_are_approvable_contracts(self):
        for step in qualify.STEPS:
            c = qualify.step_contract(step)
            self.assertEqual(contract_format.approval_errors(c), [], step)

    def test_l3_notes_ask_for_a_path_outside_the_contract(self):
        c = qualify.step_contract("l3")
        self.assertIn("package.json", c["notes"])
        self.assertEqual(c["permitted_paths"], ["docs/qualification-log.md"])

    def test_unapproved_step_sends_nothing_and_records_no_fire(self):
        self.q.snapshot(10, 20, 0)
        with self.assertRaises(qualify.QualifyRefused):
            self.q.fire("trig_01X", "l1")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.count(ev.FIRE_INTENT), 0)

    def test_fire_needs_a_usage_snapshot_like_any_dispatch(self):
        self.q.approve("l1")
        with self.assertRaises(qualify.QualifyRefused) as cm:
            self.q.fire("trig_01X", "l1")
        self.assertIn("usage-snapshot-missing", str(cm.exception))
        self.assertEqual(self.sent, [])

    def test_fire_records_intent_before_sending_and_result_after(self):
        self.ready("l1")
        result = self.q.fire("trig_01X", "l1")
        self.assertIs(result.outcome, LaunchOutcome.LAUNCHED)
        self.assertEqual(self.intents_at_send, [1])
        self.assertEqual(self.count(ev.FIRE_RESULT, "qual-smoke-l1"), 1)
        envelope = json.loads(self.sent[0][1])
        self.assertEqual(envelope["branch"], "claude/qual-smoke-l1-a1")

    def test_fires_count_against_the_weekly_cap(self):
        self.ready("l4", "l5")
        self.answer = LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=401)
        self.q.fire("trig_01X", "l4")
        self.now += timedelta(minutes=1)
        self.q.fire("trig_01X", "l5")
        # A real dispatch, on the same ledger, sees both fires in its 7-day window.
        two_a_week = AttemptGate(self.store, replace(PILOT_LIMITS, fires_per_window=2))
        other = qualify.fixture("other-task")
        decision = two_a_week.decide(TaskId("other-task"), contract_format.digest(other), self.now)
        self.assertIn("window-fire-cap", [b.code for b in decision.blocks])

    def test_launched_step_holds_the_single_lane_until_cleared(self):
        self.ready("l1", "l3")
        self.q.fire("trig_01X", "l1")
        with self.assertRaises(qualify.QualifyRefused) as cm:
            self.q.fire("trig_01X", "l3")
        self.assertIn("unresolved-attempt", str(cm.exception))
        self.assertEqual(len(self.sent), 1)
        self.q.clear("l1", URL)
        self.now += timedelta(minutes=1)
        self.q.fire("trig_01X", "l3")
        self.assertEqual(len(self.sent), 2)

    def test_a_step_fires_once(self):
        self.ready("l4")
        self.answer = LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=401)
        self.q.fire("trig_01X", "l4")
        with self.assertRaises(qualify.QualifyRefused):
            self.q.fire("trig_01X", "l4")
        self.assertEqual(len(self.sent), 1)

    def test_refire_after_a_rejected_key_needs_a_signed_decision(self):
        self.ready("l1")
        self.answer = LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=401)
        self.q.fire("trig_01X", "l1")
        with self.assertRaises(qualify.QualifyRefused):
            self.q.fire("trig_01X", "l1")
        self.answer = launched()
        self.now += timedelta(minutes=1)
        result = self.q.refire("trig_01X", "l1")
        self.assertIs(result.outcome, LaunchOutcome.LAUNCHED)
        fires = [s.event.run.fire for s in self.store.events() if s.event.kind == ev.FIRE_INTENT]
        self.assertEqual(fires, [1, 2])
        # Same branch: it is the same attempt, fired again.
        self.assertEqual(json.loads(self.sent[1][1])["branch"], "claude/qual-smoke-l1-a1")

    def test_refire_is_refused_once_a_worker_may_have_started(self):
        self.ready("l1")
        self.q.fire("trig_01X", "l1")
        with self.assertRaises(qualify.QualifyRefused):
            self.q.refire("trig_01X", "l1")
        self.assertEqual(len(self.sent), 1)

    def test_refire_declined_at_the_terminal_sends_nothing(self):
        self.ready("l1")
        self.answer = LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=401)
        self.q.fire("trig_01X", "l1")
        self.approvals._confirm = lambda summary, code: False
        with self.assertRaises(qualify.QualifyRefused):
            self.q.refire("trig_01X", "l1")
        self.assertEqual(len(self.sent), 1)

    def test_refire_of_an_unfired_step_is_refused(self):
        self.ready("l1")
        with self.assertRaises(qualify.QualifyRefused):
            self.q.refire("trig_01X", "l1")
        self.assertEqual(self.sent, [])

    def test_ctrl_c_during_the_send_is_recorded_unknown(self):
        self.ready("l1")
        self.answer = routine.LaunchInterrupted(
            LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, detail="KeyboardInterrupt")
        )
        with self.assertRaises(KeyboardInterrupt):
            self.q.fire("trig_01X", "l1")
        results = [s.event for s in self.store.events() if s.event.kind == ev.FIRE_RESULT]
        self.assertEqual([r.data["outcome"] for r in results], ["launch-outcome-unknown"])

    def test_any_other_stop_during_the_send_is_recorded_unknown(self):
        self.ready("l2")
        self.answer = SystemExit(1)
        with self.assertRaises(SystemExit):
            self.q.fire("trig_01X", "l2")
        results = [s.event for s in self.store.events() if s.event.kind == ev.FIRE_RESULT]
        self.assertEqual([r.data["outcome"] for r in results], ["launch-outcome-unknown"])

    def test_a_process_killed_mid_send_is_marked_unknown_at_next_start(self):
        self.ready("l1", "l3")
        run = self.gate.reserve(
            TaskId("qual-smoke-l1"),
            contract_format.digest(qualify.step_contract("l1")),
            self.now,
        )
        self.now += timedelta(minutes=11)
        with self.assertRaises(qualify.QualifyRefused):
            self.q.fire("trig_01X", "l3")
        status = self.recovery.attempt_status(run.attempt, self.now)
        self.assertEqual(status.latest_run, run)
        self.assertEqual(self.count(ev.FIRE_RESULT), 1)
        self.assertEqual(self.sent, [])

    def test_l2_goes_through_the_gate_then_skips_the_adapter_checks(self):
        self.ready("l2")
        self.q.fire("trig_01X", "l2")
        self.assertEqual(self.intents_at_send, [1])
        how, text = self.sent[0]
        self.assertEqual(how, "post")
        envelope = json.loads(text)
        self.assertNotIn("base_commit", envelope["contract"])
        attempt = qualify._attempt("qual-reject-l2")
        digest = contract_format.digest(qualify.step_contract("l2"))
        self.assertTrue(routine.envelope_errors(envelope, digest, attempt))

    def test_missing_key_uses_no_fire(self):
        self.ready("l5")

        def no_key(step, trig_id):
            raise LookupError("no Keychain item")

        self.q._key = no_key
        with self.assertRaises(LookupError):
            self.q.fire("trig_01X", "l5")
        self.assertEqual(self.count(ev.FIRE_INTENT), 0)


class RealAdapterTest(unittest.TestCase):
    def test_l2_reaches_the_wire_without_base_commit(self):
        captured = {}

        class Opener:
            def open(self, req, timeout):
                captured["body"] = json.loads(req.data)
                raise OSError("stop")

        adapter = routine.RoutineAdapter(
            "trig_01X", start_key=lambda t: "sk-ant-oat01-k", opener=Opener()
        )
        c = qualify.step_contract("l2")
        adapter._post(qualify.fire_text("l2", c, contract_format.digest(c)))
        self.assertNotIn("base_commit", json.loads(captured["body"]["text"])["contract"])


class CliTest(unittest.TestCase):
    def test_bad_arguments(self):
        with mock.patch("builtins.print"):
            self.assertEqual(qualify.main(["q", "fire", "trig_01X", "l9"]), 2)
            self.assertEqual(qualify.main(["q", "l1"]), 2)
            self.assertEqual(qualify.main(["q"]), 2)

    def test_uses_the_private_ledger_folder_and_no_separate_log(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            store = None

            def make():
                nonlocal store
                q = qualify._real()
                store = q._store
                return q

            with mock.patch("builtins.print"):
                self.assertEqual(qualify.main(["q", "snapshot", "10", "20", "0"], make), 0)
            folder = Path(home) / ".software-factory"
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            self.assertEqual(sorted(p.name for p in folder.iterdir() if "ledger" not in p.name), [])
            kinds = [s.event.kind for s in store.events()]
            self.assertEqual(kinds, [ev.USAGE_SNAPSHOT])
            store.close()
            # The controller opens the same ledger afterwards.
            SqliteLedgerStore().close()


if __name__ == "__main__":
    unittest.main()
