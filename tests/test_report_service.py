"""The service posting through LinearReporter onto an in-memory Linear (ENG-178).

Drives ``Service.tick`` exactly as tests/test_service.py does (fixtures, a
scripted runtime adapter, a fake GitHub, a fake clock), with the real
``LinearReporter`` and ``LinearDecisions`` in front of ``FakeLinear``.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from datetime import timedelta
from unittest import mock

from controller.interfaces import AttemptId
from controller.report.linear_api import LinearDown
from controller.report.messages import Stage
from controller.report.replies import LinearDecisions
from controller.report.reporter import LinearReporter, comment_id
from controller.service import queue as q
from controller.service.seams import Option, Question
from tests.linear_world import FACTORY, ROLANDO, FakeLinear, issue_uuid
from tests.test_recovery import SHA_A
from tests.test_service import ServiceCase, move

ISSUE = "issue-ENG-186"
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
        r = self.tick(minutes=1)  # the failed one is backing off; the others go
        self.assertEqual(r.sent, queued - 1)
        r = self.tick(minutes=5)
        self.assertEqual(r.sent, 1)
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
        self.assertEqual(len(self.failed()), queued)  # each was tried
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


if __name__ == "__main__":
    unittest.main()
