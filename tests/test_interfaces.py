import hashlib
import unittest
from datetime import UTC, datetime

from controller.interfaces import (
    MAX_FIRE_TEXT_CHARS,
    NOT_LAUNCHED_STATUSES,
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchRequest,
    LaunchResult,
    LedgerEvent,
    RunId,
    TaskId,
    classify,
)

DIGEST = ContractDigest.of(b"example contract")


class TaskIdTest(unittest.TestCase):
    def test_accepts_lowercase_hyphenated_ids(self):
        for value in ["probe-1", "eng-186", "a", "x" * 64]:
            self.assertEqual(str(TaskId(value)), value)

    def test_rejects_other_ids(self):
        for value in ["", "ENG-186", "-a", "a-", "a--b", "a_b", "a b", "a/b", "x" * 65]:
            with self.assertRaises(ValueError, msg=value):
                TaskId(value)


class AttemptIdTest(unittest.TestCase):
    def test_markers(self):
        attempt = AttemptId(TaskId("probe-1"), 2)
        self.assertEqual(str(attempt), "probe-1-a2")
        self.assertEqual(attempt.branch, "claude/probe-1-a2")
        self.assertEqual(attempt.pr_title_marker(DIGEST), f"[probe-1 a2 {DIGEST.value[:12]}]")

    def test_rejects_non_positive_or_non_int(self):
        for number in [0, -1, True, 1.0, "1"]:
            with self.assertRaises(ValueError, msg=repr(number)):
                AttemptId(TaskId("t"), number)

    def test_branch_round_trip(self):
        attempt = AttemptId(TaskId("eng-186-x"), 13)
        self.assertEqual(AttemptId.from_branch(attempt.branch), attempt)

    def test_from_branch_ignores_non_marker_branches(self):
        for branch in [
            "main",
            "claude/zealous-bohr-123",
            "claude/probe-1-a0",
            "claude/probe-1-a01",
            "claude/probe-1",
            "claude/Probe-1-a1",
            "feature/probe-1-a1",
            "claude/probe-1-a1/x",
            "claude/" + "x" * 65 + "-a1",
        ]:
            self.assertIsNone(AttemptId.from_branch(branch), branch)


class PrMarkerParsingTest(unittest.TestCase):
    def test_title_round_trip(self):
        attempt = AttemptId(TaskId("eng-186"), 3)
        title = attempt.pr_title_marker(DIGEST) + " Add a filter"
        self.assertEqual(AttemptId.from_pr_title(title), (attempt, DIGEST.short))

    def test_title_marker_must_lead_and_be_exact(self):
        good = AttemptId(TaskId("t"), 1).pr_title_marker(DIGEST)
        for title in [
            "x " + good,
            good.replace("[t ", "[T "),
            good.replace(" a1 ", " a0 "),
            good[:-2] + "]",
            "[t a1]",
            "",
        ]:
            self.assertIsNone(AttemptId.from_pr_title(title), title)

    def test_body_line(self):
        body = f"Summary\n\n{DIGEST.pr_body_line}\nMore"
        self.assertEqual(ContractDigest.from_pr_body(body), DIGEST)

    def test_body_line_rejects_missing_duplicate_or_malformed(self):
        other = ContractDigest.of(b"other")
        for body in [
            "no digest here",
            f"{DIGEST.pr_body_line}\n{other.pr_body_line}",
            f"{DIGEST.pr_body_line}\n{DIGEST.pr_body_line}",
            "Contract-Digest: abc",
            f"Contract-Digest: {DIGEST.value}x",
            f"{DIGEST.pr_body_line}\nContract-Digest: junk",
            f"  {DIGEST.pr_body_line}",
        ]:
            self.assertIsNone(ContractDigest.from_pr_body(body), body)


class RunIdTest(unittest.TestCase):
    def test_str(self):
        self.assertEqual(str(RunId(AttemptId(TaskId("t"), 1), 2)), "t-a1-f2")

    def test_rejects_bad_fire_numbers(self):
        for fire in [0, -1, False]:
            with self.assertRaises(ValueError):
                RunId(AttemptId(TaskId("t"), 1), fire)


class ContractDigestTest(unittest.TestCase):
    def test_of_is_sha256(self):
        self.assertEqual(DIGEST.value, hashlib.sha256(b"example contract").hexdigest())
        self.assertEqual(DIGEST.short, DIGEST.value[:12])

    def test_pr_body_line(self):
        self.assertEqual(DIGEST.pr_body_line, "Contract-Digest: " + DIGEST.value)

    def test_rejects_malformed(self):
        for value in ["", "abc", DIGEST.value.upper(), DIGEST.value + "0", "g" * 64]:
            with self.assertRaises(ValueError):
                ContractDigest(value)


class LaunchResultTest(unittest.TestCase):
    def test_not_launched_statuses_match_adr(self):
        self.assertEqual(NOT_LAUNCHED_STATUSES, {400, 401, 403, 404, 429})

    def test_classify(self):
        self.assertIs(classify(200, "cse_1"), LaunchOutcome.LAUNCHED)
        for status in NOT_LAUNCHED_STATUSES:
            self.assertIs(classify(status, None), LaunchOutcome.NOT_LAUNCHED)
            self.assertIs(classify(status, "cse_1"), LaunchOutcome.NOT_LAUNCHED)
        for status, session in [(200, None), (200, ""), (500, None), (503, "cse_1"), (None, None)]:
            self.assertIs(classify(status, session), LaunchOutcome.OUTCOME_UNKNOWN)

    def test_launched_needs_200_session_id_and_url(self):
        url = "https://claude.ai/code/cse_1"
        LaunchResult(LaunchOutcome.LAUNCHED, 200, "cse_1", url)
        for status, session, session_url in [
            (200, None, url),
            (404, "cse_1", url),
            (None, "cse_1", url),
            (200, "cse_1", None),
        ]:
            with self.assertRaises(ValueError):
                LaunchResult(LaunchOutcome.LAUNCHED, status, session, session_url)

    def test_not_launched_needs_documented_status(self):
        for status in [None, 200, 500, 503]:
            with self.assertRaises(ValueError):
                LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=status)
        LaunchResult(LaunchOutcome.NOT_LAUNCHED, http_status=429, retry_after_seconds=5)

    def test_unknown_excludes_no_session_statuses(self):
        LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN)
        LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, http_status=503)
        with self.assertRaises(ValueError):
            LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, http_status=429)


class LaunchRequestTest(unittest.TestCase):
    def test_text_limit(self):
        run = RunId(AttemptId(TaskId("t"), 1), 1)
        LaunchRequest(run, DIGEST, "x" * MAX_FIRE_TEXT_CHARS)
        with self.assertRaises(ValueError):
            LaunchRequest(run, DIGEST, "x" * (MAX_FIRE_TEXT_CHARS + 1))


AT = datetime(2026, 10, 8, tzinfo=UTC)


class LedgerEventTest(unittest.TestCase):
    def test_requires_aware_time(self):
        with self.assertRaises(ValueError):
            LedgerEvent("x", datetime(2026, 10, 8), TaskId("t"))
        LedgerEvent("x", AT, TaskId("t"))

    def test_factory_wide_event_has_no_task(self):
        self.assertIsNone(LedgerEvent("hold", AT).task)

    def test_ids_must_agree(self):
        task = TaskId("t")
        attempt = AttemptId(task, 1)
        run = RunId(attempt, 1)
        LedgerEvent("x", AT, task, attempt, run)
        for kwargs in [
            {"task": TaskId("other"), "attempt": attempt},
            {"attempt": attempt},
            {"task": task, "run": run},
            {"task": task, "attempt": AttemptId(task, 2), "run": run},
        ]:
            with self.assertRaises(ValueError, msg=kwargs):
                LedgerEvent("x", AT, **kwargs)

    def test_data_is_copied_and_frozen(self):
        data = {"a": 1}
        event = LedgerEvent("x", AT, data=data)
        data["a"] = 2
        self.assertEqual(event.data["a"], 1)
        with self.assertRaises(TypeError):
            event.data["a"] = 3


if __name__ == "__main__":
    unittest.main()
