import json
import unittest
from unittest import mock

from controller import contract as contract_format
from controller.adapter import qualify, routine
from controller.interfaces import LaunchOutcome, LaunchResult


class FixtureTest(unittest.TestCase):
    def test_fixtures_are_approvable_contracts(self):
        for task, notes in (("qual-smoke-l1", None), ("qual-notes-l3", "also bump package.json")):
            c = qualify.fixture(task, notes)
            self.assertEqual(contract_format.validate(c, contract_format.digest(c)), [], task)

    def test_l3_notes_ask_for_a_path_outside_the_contract(self):
        c = qualify.fixture("qual-notes-l3", "bump package.json")
        self.assertNotIn("package.json", c["permitted_paths"])
        self.assertEqual(c["permitted_paths"], ["docs/qualification-log.md"])

    def test_steps_fire_once_and_log(self):
        sent = []

        def launch(self, request):
            sent.append(request)
            return LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=401)

        with (
            mock.patch.object(routine.RoutineAdapter, "launch", launch),
            mock.patch.object(qualify, "_log") as log,
        ):
            self.assertEqual(qualify.main(["q", "trig_01X", "l4"]), 0)
        self.assertEqual(len(sent), 1)
        envelope = json.loads(sent[0].text)
        self.assertEqual(envelope["branch"], "claude/qual-wrongkey-l4-a1")
        self.assertEqual(log.call_args.args[:2], ("l4", "qual-wrongkey-l4"))

    def test_l2_payload_is_one_the_adapter_refuses(self):
        captured = {}

        class Opener:
            def open(self, req, timeout):
                captured["body"] = json.loads(req.data)
                raise OSError("stop")

        with (
            mock.patch.object(routine, "keychain_key", lambda t: "sk-ant-oat01-k"),
            mock.patch("urllib.request.build_opener", lambda *a: Opener()),
            mock.patch.object(qualify, "_log"),
        ):
            qualify.main(["q", "trig_01X", "l2"])
        envelope = json.loads(captured["body"]["text"])
        self.assertNotIn("base_commit", envelope["contract"])
        attempt = qualify._attempt("qual-reject-l2")
        digest = contract_format.digest(qualify.fixture("qual-reject-l2"))
        self.assertTrue(routine.envelope_errors(envelope, digest, attempt))

    def test_bad_arguments(self):
        with mock.patch("builtins.print"):
            self.assertEqual(qualify.main(["q", "trig_01X", "l9"]), 2)


if __name__ == "__main__":
    unittest.main()
