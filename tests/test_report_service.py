"""The service posting through LinearReporter onto an in-memory Linear (ENG-178).

Drives ``Service.tick`` exactly as tests/test_service.py does (fixtures, a
scripted runtime adapter, a fake GitHub, a fake clock), with the real
``LinearReporter`` and ``LinearDecisions`` in front of ``FakeLinear``.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass, replace
from datetime import timedelta
from unittest import mock

from controller.interfaces import AttemptId, TaskId
from controller.report.linear_api import LinearDown
from controller.report.messages import Stage
from controller.report.replies import LinearDecisions
from controller.report.reporter import LinearReporter, comment_id
from controller.review.report import review_messages
from controller.review.reviewer import ReviewState, ReviewStatus, Revision
from controller.service import queue as q
from controller.service.seams import Option, Question
from tests.linear_world import FACTORY, ROLANDO, FakeLinear, issue_uuid
from tests.test_recovery import SHA_A
from tests.test_service import REPO, ServiceCase, move
from verify.findings import Finding, Route, Severity, finding_id

ISSUE = "issue-ENG-186"
OTHER_ISSUE = "issue-ENG-187"
HEADER_RE = re.compile(r"^\*\*Factory: (?P<stage>[^*]+)\*\*$")
STAGES = {s.value for s in Stage}
LIN_KEY = "lin_api_" + "Zq9" * 12


@dataclass(frozen=True)
class KindQuestion(Question):
    kind: str = "product"


def sort_question(**kw: object) -> Question:
    base: dict[str, object] = dict(
        text="Sort by title or by date?",
        options=(Option("A", "Title", "A to Z."), Option("B", "Date", "Newest first.")),
        recommended="B",
        if_no_answer="The ticket waits.",
        key="q-sort",
    )
    base.update(kw)
    cls = KindQuestion if "kind" in base else Question
    return cls(**base)  # type: ignore[arg-type]


class QuestionPreparer:
    def __init__(self, question: Question) -> None:
        self.question = question
        self.calls = 0

    def prepare(self, authorization, project):
        self.calls += 1
        return self.question


class LinearServiceCase(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.linear = FakeLinear()
        self.reporter = LinearReporter(self.linear, ROLANDO)  # type: ignore[assignment]
        self.build()

    def restart(self) -> None:
        """A new process: a new reporter instance on the same Linear."""
        self.reporter = LinearReporter(self.linear, ROLANDO)  # type: ignore[assignment]
        self.build()

    def bodies(self, issue: str = ISSUE) -> list[str]:
        return [c.body for c in sorted(self.linear.on(issue), key=lambda c: c.created_at)]

    def failed(self) -> list[object]:
        return [s.event for s in self.store.events() if s.event.kind == q.OUTBOX_FAILED]

    def assert_one_comment_per_key(self) -> None:
        refs = [b.rsplit("factory-ref: ", 1)[1] for b in self.bodies()]
        self.assertEqual(len(refs), len(set(refs)), refs)


class OutboxTests(LinearServiceCase):
    def test_hold_all_failure_stops_the_round_and_later_rounds_send(self) -> None:
        self.rolando_approves()
        self.linear.down = LinearDown("Linear answered HTTP 429", "RATELIMITED")
        r = self.tick()
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(r.sent, 0)
        self.assertGreaterEqual(len(self.view().outbox), 2)
        # One message tried, one failure recorded, the rest not tried at all.
        self.assertEqual(len(self.linear.calls), 1)
        self.assertEqual(len(self.failed()), 1)
        queued = len(self.view().outbox)

        self.linear.down = None
        # The failed one is backing off, and the ticket's newer messages wait
        # behind it so its comments stay in the order they were written.
        r = self.tick(minutes=1)
        self.assertEqual(r.sent, 0)
        r = self.tick(minutes=5)
        self.assertEqual(r.sent, queued)
        self.assertEqual(self.view().outbox, {})
        self.assertEqual(len(self.bodies()), queued)
        self.assert_one_comment_per_key()
        for _ in range(3):
            self.tick(minutes=10)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.fires()), 1)

    def test_plain_refusal_does_not_stop_the_round(self) -> None:
        from controller.report.linear_api import LinearRefused

        self.rolando_approves()
        self.linear.refuse_create[issue_uuid(ISSUE)] = LinearRefused("bad", "INVALID")
        self.tick()
        queued = len(self.view().outbox)
        self.assertGreaterEqual(queued, 2)
        # Only the oldest was tried: the ticket's later messages wait behind it.
        self.assertEqual(len(self.failed()), 1)
        del self.linear.refuse_create[issue_uuid(ISSUE)]
        self.tick(minutes=5)
        self.assertEqual(self.view().outbox, {})
        self.assertEqual(len(self.adapter.requests), 1)

    def test_lost_answer_is_not_posted_twice(self) -> None:
        self.rolando_approves()
        self.linear.lose_next_create = 1
        self.tick()
        self.assertEqual(len(self.bodies()), 1)  # made, but the answer was lost
        self.assertEqual(len(self.failed()), 1)
        for _ in range(3):
            self.tick(minutes=5)
        self.assertEqual(self.view().outbox, {})
        self.assert_one_comment_per_key()
        self.assertEqual(len(self.adapter.requests), 1)

    def test_crash_between_post_and_record_then_restart_shows_one_comment(self) -> None:
        self.rolando_approves()
        real_append = self.service._append

        def die_on_sent(*events):
            if any(e.kind == q.OUTBOX_SENT for e in events):
                raise SystemExit("killed after posting")
            real_append(*events)

        with mock.patch.object(self.service, "_append", side_effect=die_on_sent):
            with self.assertRaises(SystemExit):
                self.tick()
        self.assertEqual(len(self.bodies()), 1)
        self.restart()
        for _ in range(2):
            self.tick(minutes=6)
        self.assertEqual(self.view().outbox, {})
        self.assert_one_comment_per_key()
        self.assertEqual(len(self.adapter.requests), 1)

    def test_key_acting_as_rolando_posts_nothing_and_launches_once(self) -> None:
        self.linear.viewer_id = ROLANDO
        self.rolando_approves()
        for i in range(4):
            self.tick(minutes=10 if i else 0)
        self.assertEqual(self.linear.comments, {})
        self.assertEqual(self.linear.creates(), 0)
        self.assertGreater(len(self.view().outbox), 0)
        self.assertEqual(len(self.adapter.requests), 1)
        errors = [str(e.data.get("error")) for e in self.failed()]  # type: ignore[attr-defined]
        self.assertTrue(all("acts as Rolando" in e for e in errors), errors)

    def test_failure_text_in_the_ledger_has_no_key(self) -> None:
        self.rolando_approves()
        self.linear.down = LinearDown(f"cannot reach with {LIN_KEY}")
        self.tick()
        self.assertTrue(self.failed())
        for s in self.store.events():
            self.assertNotIn(LIN_KEY, repr(s.event.data))


class QuestionServiceTests(LinearServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.preparer = QuestionPreparer(sort_question())  # type: ignore[assignment]
        self.build()

    def question_key(self) -> str:
        return f"question:{ISSUE}:q-sort"

    def test_question_is_posted_once_under_its_key_and_reads_back(self) -> None:
        self.tick()
        self.assertEqual(self.view().items["evt-1"].closed, "question")
        cid = comment_id(issue_uuid(ISSUE), self.question_key())
        self.assertIn(cid, self.linear.comments)
        body = self.linear.comments[cid].body
        self.assertTrue(body.startswith("**Factory: Needs your decision**"))
        lines = [x for x in body.split("\n") if x.strip()]
        self.assertEqual(lines[-2], "factory-question: q-sort options=A,B")
        self.assertEqual(lines[-1], f"factory-ref: {self.question_key()}")
        self.assertEqual(self.adapter.requests, [])

        decisions = LinearDecisions(self.linear, ROLANDO)
        self.assertEqual(decisions.answers(ISSUE), ())  # the recommendation is not an answer
        self.linear.add(ISSUE, "B", ROLANDO, parent=cid)
        [a] = decisions.answers(ISSUE)
        self.assertEqual((a.question_key, a.option), ("q-sort", "B"))

    def test_same_question_again_posts_one_repeat_note_per_extra_move(self) -> None:
        self.tick()
        self.source.events.append(move(2))
        self.tick(minutes=6)
        self.source.events.append(move(3))
        self.tick(minutes=6)
        self.assertEqual(self.preparer.calls, 3)  # type: ignore[attr-defined]
        for evt in ("evt-1", "evt-2", "evt-3"):
            self.assertEqual(self.view().items[evt].closed, "question")
        bodies = self.bodies()
        questions = [b for b in bodies if "factory-question:" in b]
        repeats = [b for b in bodies if "hasn't changed" in b]
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(repeats), 2)
        refs = {b.rsplit("factory-ref: ", 1)[1] for b in repeats}
        self.assertEqual(refs, {"evt-2:question-repeat", "evt-3:question-repeat"})
        self.assert_one_comment_per_key()
        for _ in range(2):
            self.tick(minutes=6)
        self.assertEqual(len([b for b in self.bodies() if "hasn't changed" in b]), 2)
        self.assertEqual(self.adapter.requests, [])

    def test_restart_does_not_ask_again(self) -> None:
        self.tick()
        self.restart()
        self.tick(minutes=6)
        self.assertEqual(len([b for b in self.bodies() if "factory-question:" in b]), 1)

    def test_notice_kind_closes_with_its_kind_and_is_stopped(self) -> None:
        self.preparer.question = sort_question(kind="changed")  # type: ignore[attr-defined]
        self.tick()
        self.assertEqual(self.view().items["evt-1"].closed, "question-changed")
        [body] = [b for b in self.bodies() if "factory-question:" in b]
        self.assertTrue(body.startswith("**Factory: Stopped**"))
        self.assertNotIn("Options:", body)
        self.assertNotIn("recommends", body)

    def test_question_without_key_uses_the_event_id(self) -> None:
        self.preparer.question = sort_question(key="")  # type: ignore[attr-defined]
        self.tick()
        cid = comment_id(issue_uuid(ISSUE), f"question:{ISSUE}:evt-1")
        self.assertIn(cid, self.linear.comments)


class MergeServiceTests(LinearServiceCase):
    def merge(self) -> None:
        self.rolando_approves()
        self.tick()
        a1 = AttemptId(self.task(), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr()]
        self.tick(minutes=6)
        self.tick(minutes=6)
        self.github.pulls = [self.pr(state="closed", merged=True, merge_commit="c" * 40)]
        self.tick(minutes=6)
        self.tick(minutes=6)

    def test_merge_is_recorded_as_an_exception(self) -> None:
        self.merge()
        self.assertEqual(self.view().items["evt-1"].closed, "merged")
        [body] = [b for b in self.bodies() if "merged at" in b]
        self.assertTrue(body.startswith("**Factory: Merged as an exception**"))
        self.assertIn("PR #7", body)
        self.assertIn("not as verified work", body)
        self.assertIn("Nothing has been released", body)
        self.assertFalse(any(b.startswith("**Factory: Merged**") for b in self.bodies()))
        self.assertEqual(len(self.adapter.requests), 1)

    def test_every_message_opens_with_a_stage(self) -> None:
        self.merge()
        self.now += timedelta(hours=3)
        self.restart()
        self.tick()
        all_bodies = [c.body for c in self.linear.comments.values()]
        self.assertTrue(any("was not running" in b for b in all_bodies))
        self.assertGreaterEqual(len(all_bodies), 5)
        for b in all_bodies:
            with self.subTest(body=b[:60]):
                match = HEADER_RE.match(b.split("\n", 1)[0])
                self.assertIsNotNone(match, b)
                assert match is not None
                self.assertIn(match["stage"], STAGES)
                self.assertRegex(b.rsplit("\n", 1)[-1], r"^factory-ref: \S+$")
        for c in self.linear.comments.values():
            self.assertEqual(c.user_id, FACTORY)


class OrderTests(LinearServiceCase):
    def test_comments_keep_their_order_after_an_outage(self) -> None:
        """Rolando's report: Working showed before Queued after an outage."""
        self.rolando_approves()
        self.linear.down = LinearDown("Linear answered HTTP 503")
        self.tick()  # queued + working are both written; nothing posts
        self.linear.down = None
        for _ in range(8):
            self.tick(minutes=1)  # the service's own pace: inside the retry wait
        self.assertEqual(self.view().outbox, {})
        expected = [
            s.event.data["key"]
            for s in self.store.events()
            if s.event.kind == q.OUTBOX_QUEUED and s.event.data["issue_id"] == ISSUE
        ]
        refs = [b.rsplit("factory-ref: ", 1)[1] for b in self.bodies()]
        self.assertEqual(refs, expected)
        stages = [b.split("**", 2)[1] for b in self.bodies()]
        self.assertEqual(stages[0], "Factory: Queued")

    def test_a_held_ticket_does_not_hold_another(self) -> None:
        from controller.report.linear_api import LinearRefused

        self.rolando_approves()
        self.service._append(
            q.message("other:1", OTHER_ISSUE, "**Factory: Notice**\n\nx", self.now)
        )
        self.linear.refuse_create[issue_uuid(ISSUE)] = LinearRefused("bad", "INVALID")
        self.tick()
        self.assertEqual(len(self.bodies(OTHER_ISSUE)), 1)


class FactoryFailureRepeatTests(LinearServiceCase):
    """Rolando's report: a repeated factory failure became a fake question."""

    def setUp(self) -> None:
        super().setUp()
        failure = Question("The factory couldn't read the repository.", kind="factory", key="f-1")
        self.preparer = QuestionPreparer(failure)  # type: ignore[assignment]
        self.build()

    def test_repeat_stays_a_stop_notice(self) -> None:
        self.tick()
        self.source.events.append(move(2))
        self.tick(minutes=6)
        self.assertEqual(self.view().items["evt-2"].closed, "question-factory")
        bodies = [b for b in self.bodies() if "read the repository" in b]
        self.assertEqual(len(bodies), 2)  # the notice and its repeat
        for b in bodies:
            self.assertTrue(b.startswith("**Factory: Stopped**"), b)
            self.assertNotIn("Needs your decision", b)
            self.assertIn("read the repository", b)


class PullRequestLinkTests(LinearServiceCase):
    """Rolando's report: review messages named PR #7 but gave no link."""

    def test_review_and_merge_messages_link_the_pr(self) -> None:
        self.rolando_approves()
        self.tick()
        a1 = AttemptId(self.task(), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr()]
        self.tick(minutes=6)
        self.tick(minutes=6)
        link = f"https://github.com/{REPO}/pull/7"
        review = [b for b in self.bodies() if b.startswith("**Factory: Reviewing**")]
        self.assertEqual(len(review), 1)
        self.assertIn(f"Pull request: {link}", review[0])
        self.github.pulls = [self.pr(state="closed", merged=True, merge_commit="c" * 40)]
        for _ in range(2):
            self.tick(minutes=6)
        merged = [b for b in self.bodies() if b.startswith("**Factory: Merged")]
        self.assertEqual(len(merged), 1)
        self.assertIn(f"Pull request: {link}", merged[0])


class ScriptedReviewer:
    """A reviewer with the full ENG-156 surface, answering from a script."""

    def __init__(self) -> None:
        self.started: list = []
        self.status = None
        self.head = ""
        self.checks = 0

    def start(self, pr, key):
        self.started.append(pr)
        return "asked"

    def check(self, attempt):
        self.checks += 1
        if self.status is None:
            raise RuntimeError("GitHub unreadable")
        return replace(self.status, attempt=attempt)

    def evidence(self, attempt):
        return None if self.status is None else replace(self.status, attempt=attempt)

    def merged_head(self, attempt):
        return self.head


def status(state, head=SHA_A, findings=()):
    rv = Revision(REPO, 7, "0" * 64, head, "b" * 40, "c" * 40)
    return ReviewStatus(state, AttemptId(TaskId("x"), 1), 7, "note", rv.key(), rv, findings)


def finding(category, route, summary, criterion=None):
    return Finding(
        id=finding_id(category, criterion, summary),
        severity=Severity.BLOCKING,
        category=category,
        route=route,
        summary=summary,
        evidence="e",
        suggested_action="a",
        commit=SHA_A,
        criterion=criterion,
    )


class ReviewServiceTests(LinearServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.reviewer = ScriptedReviewer()  # type: ignore[assignment]
        self.build()

    def open_pr(self) -> None:
        self.rolando_approves()
        self.tick()
        a1 = AttemptId(self.task(), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr()]
        self.tick(minutes=6)
        self.tick(minutes=6)

    def merge(self) -> None:
        self.github.pulls = [self.pr(state="closed", merged=True, merge_commit="c" * 40)]
        self.tick(minutes=6)
        self.tick(minutes=6)

    def stages(self):
        return [HEADER_RE.match(b.split("\n", 1)[0])["stage"] for b in self.bodies()]

    def test_a_pass_on_the_merged_commit_is_verified_work(self) -> None:
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.tick(minutes=6)
        self.assertEqual(self.stages().count("Ready for your review"), 1)
        self.reviewer.head = SHA_A
        self.merge()
        [body] = [b for b in self.bodies() if "merged at" in b]
        self.assertTrue(body.startswith("**Factory: Merged**"), body)
        self.assertIn("passed on that exact commit", body)

    def test_a_pass_on_an_earlier_commit_is_an_exception(self) -> None:
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED, head="e" * 40)
        self.tick(minutes=6)
        self.reviewer.head = SHA_A
        self.merge()
        [body] = [b for b in self.bodies() if "merged at" in b]
        self.assertTrue(body.startswith("**Factory: Merged as an exception**"), body)
        self.assertIn("the review covered `eeeeeee", body)

    def test_an_unreadable_merged_head_is_an_exception(self) -> None:
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.merge()
        [body] = [b for b in self.bodies() if "merged at" in b]
        self.assertTrue(body.startswith("**Factory: Merged as an exception**"), body)

    def test_failed_review_reports_once_for_the_repair_step(self) -> None:
        self.open_pr()
        self.reviewer.status = status(
            ReviewState.FAILED, findings=(finding("checks-failed", Route.REPAIR, "npm test fails"),)
        )
        for _ in range(3):
            self.tick(minutes=6)
        failed = [b for b in self.bodies() if b.startswith("**Factory: Failed**")]
        self.assertEqual(len(failed), 1)
        self.assertIn("npm test fails", failed[0])
        self.assertIn("Nothing is needed from you", failed[0])

    def test_needs_rolando_asks_for_an_observation_bound_to_the_commit(self) -> None:
        self.open_pr()
        self.reviewer.status = status(
            ReviewState.NEEDS_ROLANDO,
            findings=(
                finding("needs-observation", Route.ROLANDO, "Check the filter control", "ac3"),
                finding("flag-changed-test", Route.ROLANDO, "A protected control changed"),
            ),
        )
        for _ in range(3):
            self.tick(minutes=6)
        asks = [b for b in self.bodies() if "Please look at the change" in b]
        self.assertEqual(len(asks), 1)
        self.assertIn("aaaaaaa", asks[0])
        others = [b for b in self.bodies() if "only you can settle" in b]
        self.assertEqual(len(others), 1)
        self.assertNotIn("Check the filter control", others[0])
        self.assert_one_comment_per_key()

    def test_a_check_that_fails_posts_nothing_and_keeps_going(self) -> None:
        self.open_pr()
        before = len(self.bodies())
        self.tick(minutes=6)
        self.assertGreater(self.reviewer.checks, 0)
        self.assertEqual(len(self.bodies()), before)


class ReviewMessageTests(unittest.TestCase):
    def test_waiting_and_running_say_nothing(self) -> None:
        for state in (ReviewState.WAITING_CI, ReviewState.RUNNING):
            self.assertEqual(review_messages(status(state)), [])

    def test_a_new_revision_gets_new_keys(self) -> None:
        a = review_messages(status(ReviewState.PASSED))
        b = review_messages(status(ReviewState.PASSED, head="e" * 40))
        self.assertNotEqual(a[0][0], b[0][0])
        self.assertTrue(a[0][0].startswith("ready:"))

    def test_blocked_keys_change_with_the_reason(self) -> None:
        s = replace(status(ReviewState.BLOCKED), blocks=("on hold",))
        t = replace(status(ReviewState.BLOCKED), blocks=("12 of 12 fires",))
        self.assertNotEqual(review_messages(s)[0][0], review_messages(t)[0][0])
        self.assertIn("Waiting", review_messages(s)[0][1])


if __name__ == "__main__":
    unittest.main()
