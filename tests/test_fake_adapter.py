import unittest

from controller.adapter.fake import FakeBehavior, FakeRuntimeAdapter, FakeStep, ScriptExhausted
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchRequest,
    RunId,
    RuntimeAdapter,
    TaskId,
)

DIGEST = ContractDigest.of(b"contract")


def request(fire: int = 1) -> LaunchRequest:
    return LaunchRequest(RunId(AttemptId(TaskId("t"), 1), fire), DIGEST, "payload")


class FakeRuntimeAdapterTest(unittest.TestCase):
    def test_is_a_runtime_adapter(self):
        self.assertIsInstance(FakeRuntimeAdapter([]), RuntimeAdapter)

    def test_launch_success(self):
        fake = FakeRuntimeAdapter([FakeStep.launch()])
        result = fake.launch(request())
        self.assertIs(result.outcome, LaunchOutcome.LAUNCHED)
        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.session_id.startswith("cse_"))
        self.assertEqual(result.session_url, f"https://claude.ai/code/{result.session_id}")
        self.assertEqual([s.session_id for s in fake.sessions_created], [result.session_id])

    def test_rate_rejection_starts_nothing(self):
        fake = FakeRuntimeAdapter([FakeStep.rate_limited(120)])
        result = fake.launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertEqual(result.http_status, 429)
        self.assertEqual(result.retry_after_seconds, 120)
        self.assertEqual(fake.sessions_created, [])

    def test_other_rejection(self):
        fake = FakeRuntimeAdapter([FakeStep.rejected(401)])
        result = fake.launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertEqual(result.http_status, 401)
        self.assertEqual(fake.sessions_created, [])

    def test_bad_scripts_fail_when_built(self):
        with self.assertRaises(ValueError):
            FakeStep.rejected(500)
        with self.assertRaises(ValueError):
            FakeStep.lost_response(status=429)
        with self.assertRaises(ValueError):
            FakeStep(FakeBehavior.REJECTED, 400, session_created=True)
        with self.assertRaises(ValueError):
            FakeStep(FakeBehavior.LAUNCH, 200)

    def test_rate_rejection_without_retry_after(self):
        fake = FakeRuntimeAdapter([FakeStep.rate_limited(None, body="usage limit")])
        result = fake.launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertIsNone(result.retry_after_seconds)
        self.assertEqual(result.response_body, "usage limit")

    def test_server_error_and_200_without_session_are_unknown(self):
        fake = FakeRuntimeAdapter(
            [
                FakeStep.lost_response(status=503, body="unavailable"),
                FakeStep.lost_response(status=200, session_created=False),
            ]
        )
        first = fake.launch(request(1))
        self.assertIs(first.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertEqual((first.http_status, first.response_body), (503, "unavailable"))
        self.assertEqual(len(fake.sessions_created), 1)
        second = fake.launch(request(2))
        self.assertIs(second.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(second.http_status, 200)
        self.assertEqual(len(fake.sessions_created), 1)

    def test_lost_response_hides_a_started_session(self):
        fake = FakeRuntimeAdapter([FakeStep.lost_response()])
        result = fake.launch(request())
        self.assertIs(result.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertIsNone(result.http_status)
        self.assertIsNone(result.session_id)
        self.assertEqual(len(fake.sessions_created), 1)

    def test_lost_response_without_session(self):
        fake = FakeRuntimeAdapter([FakeStep.lost_response(session_created=False)])
        self.assertIs(fake.launch(request()).outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(fake.sessions_created, [])

    def test_follows_script_in_order_and_records_requests(self):
        fake = FakeRuntimeAdapter([FakeStep.rate_limited(), FakeStep.launch()])
        first, second = request(1), request(2)
        self.assertIs(fake.launch(first).outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertIs(fake.launch(second).outcome, LaunchOutcome.LAUNCHED)
        self.assertEqual(fake.requests, [first, second])
        self.assertEqual(fake.sessions_created[0].run, second.run)
        self.assertEqual(fake.remaining_steps, 0)

    def test_unscripted_launch_fails_the_test(self):
        fake = FakeRuntimeAdapter([])
        with self.assertRaises(ScriptExhausted):
            fake.launch(request())


if __name__ == "__main__":
    unittest.main()
