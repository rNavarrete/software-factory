"""Reading Rolando's answers, observations and time from Linear (ENG-178)."""

from __future__ import annotations

import unittest

from controller.report import messages as m
from controller.report.linear_api import LinearDown, LinearRefused
from controller.report.replies import (
    MAX_ENTRY_MINUTES,
    LinearDecisions,
    ReadFailed,
    judge,
    own_markers,
    parse_comment,
    picked_option,
    time_minutes,
)
from controller.report.reporter import LinearReporter, comment_id, render
from controller.service.seams import Option, Question
from tests.linear_world import FACTORY, OTHER, ROLANDO, FakeLinear, issue_uuid

ISSUE = "0b7e3c52-6a0f-4a1e-9d1c-3f5a2b7c9e10"
SHA = "b" * 40
QKEY = f"question:{ISSUE}:q-sort"


def sort_question() -> Question:
    return Question(
        "Sort by title or by date?",
        options=(Option("A", "Title", "A to Z."), Option("B", "Date", "Newest first.")),
        recommended="B",
        if_no_answer="The ticket waits.",
        key="q-sort",
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.linear = FakeLinear()
        self.reporter = LinearReporter(self.linear, ROLANDO)
        self.decisions = LinearDecisions(self.linear, ROLANDO)

    def ask(self) -> str:
        self.reporter.post(ISSUE, QKEY, m.question(sort_question()))
        return comment_id(ISSUE, QKEY)

    def ready(self, commit: str = SHA) -> str:
        key = f"ready:{ISSUE}:{commit[:12]}"
        r = m.Readiness("https://github.com/o/r/pull/7", commit, "Adds a filter")
        self.reporter.post(ISSUE, key, m.ready(r))
        return comment_id(ISSUE, key)

    def read(self):
        return self.decisions.read(ISSUE)


class AnswerTests(Base):
    def test_rolando_reply_in_thread_is_an_answer_with_the_option(self) -> None:
        for reply, want in (
            ("B", "B"),
            ("b.", "B"),
            ("Option B: because newest matters", "B"),
            ("**B** - x", "B"),
            ("A", "A"),
            ("  a, title is fine", "A"),
            ("A good idea", None),
            ("Agreed, B", None),
            ("C", None),
        ):
            with self.subTest(reply=reply):
                self.setUp()
                qid = self.ask()
                c = self.linear.add(ISSUE, reply, ROLANDO, parent=qid)
                [a] = self.read().answers
                self.assertEqual(a.question_key, "q-sort")
                self.assertEqual(a.option, want)
                self.assertEqual(a.text, reply)
                self.assertEqual(a.comment_id, c.id)
                self.assertEqual(len(a.body_sha256), 64)

    def test_option_colon_form(self) -> None:
        self.assertEqual(picked_option("Option: B", ["A", "B"]), "B")
        self.assertEqual(picked_option("option: b - newest", ["A", "B"]), "B")

    def test_reading_by_ticket_key(self) -> None:
        self.reporter.post("ENG-1", "question:ENG-1:q-sort", m.question(sort_question()))
        uid = issue_uuid("ENG-1")
        self.linear.add("ENG-1", "B", ROLANDO, parent=comment_id(uid, "question:ENG-1:q-sort"))
        [a] = self.decisions.read("ENG-1").answers
        self.assertEqual((a.question_key, a.option), ("q-sort", "B"))

    def test_stored_body_with_escaped_underscores_is_still_the_factory_question(self) -> None:
        q = Question("Which?", options=(Option("A", "a", "x."), Option("B", "b", "y.")), key="q_s")
        key = f"question:{ISSUE}:q_s"
        self.reporter.post(ISSUE, key, m.question(q))
        cid = comment_id(ISSUE, key)
        stored = self.linear.comments[cid]
        stored.body = stored.body.replace("_", "\\_")  # how Linear may store it
        self.assertIn("q\\_s", stored.body)
        self.linear.add(ISSUE, "B", ROLANDO, parent=cid)
        [a] = self.read().answers
        self.assertEqual((a.question_key, a.option), ("q_s", "B"))

    def test_answers_matches_read(self) -> None:
        qid = self.ask()
        self.linear.add(ISSUE, "B", ROLANDO, parent=qid)
        self.assertEqual(list(self.decisions.answers(ISSUE)), list(self.read().answers))

    def test_no_reply_is_no_answer_and_the_recommendation_is_not_one(self) -> None:
        self.ask()
        r = self.read()
        self.assertEqual(r.answers, ())
        self.assertEqual(r.ignored, ())

    def test_ignored_authors_say_why(self) -> None:
        cases = {
            "another person": (dict(user_id=OTHER), "someone other than Rolando"),
            "bot": (dict(bot={"type": "integration", "name": "Zapier"}), "integration or app"),
            "app user": (dict(user_app=True), "app account"),
            "external": (dict(user_id=None, external=True), "synced integration"),
            "no user": (dict(user_id=None), "does not record who"),
        }
        for name, (extra, reason) in cases.items():
            with self.subTest(name):
                self.setUp()
                qid = self.ask()
                user = extra.pop("user_id", ROLANDO)
                self.linear.add(ISSUE, "B", user, parent=qid, **extra)  # type: ignore[arg-type]
                r = self.read()
                self.assertEqual(r.answers, ())
                [ig] = r.ignored
                self.assertIn(reason, ig.reason)

    def test_edited_reply_is_ignored(self) -> None:
        qid = self.ask()
        c = self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        self.linear.edit(c.id, "B")
        r = self.read()
        self.assertEqual(r.answers, ())
        self.assertIn("edited", r.ignored[0].reason)

    def test_deleted_reply_is_no_longer_an_answer(self) -> None:
        qid = self.ask()
        c = self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        self.assertEqual(len(self.read().answers), 1)
        self.linear.delete(c.id)
        self.assertEqual(self.read().answers, ())

    def test_reply_outside_the_thread_is_not_an_answer(self) -> None:
        self.ask()
        self.linear.add(ISSUE, "B", ROLANDO)
        other = self.linear.add(ISSUE, "discussion", OTHER)
        self.linear.add(ISSUE, "B", ROLANDO, parent=other.id)
        r = self.read()
        self.assertEqual(r.answers, ())
        self.assertEqual(r.ignored, ())

    def test_edited_or_deleted_question_is_not_trusted(self) -> None:
        qid = self.ask()
        self.linear.add(ISSUE, "B", ROLANDO, parent=qid)
        self.linear.edit(qid)
        self.assertEqual(self.read().answers, ())
        self.linear.delete(qid)
        self.assertEqual(self.read().answers, ())

    def test_several_replies_are_answers_oldest_first(self) -> None:
        qid = self.ask()
        a = self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        b = self.linear.add(ISSUE, "B. on second thought", ROLANDO, parent=qid)
        got = self.read().answers
        self.assertEqual([x.comment_id for x in got], [a.id, b.id])
        self.assertEqual([x.option for x in got], ["A", "B"])


class ForgeryTests(Base):
    def test_forged_question_by_someone_else_is_not_read(self) -> None:
        body = render(m.question(sort_question()), QKEY)
        fake = self.linear.add(ISSUE, body, OTHER, cid=comment_id(ISSUE, QKEY))
        self.linear.add(ISSUE, "B", ROLANDO, parent=fake.id)
        self.assertEqual(self.read().answers, ())

    def test_factory_comment_whose_id_does_not_match_its_ref_is_not_trusted(self) -> None:
        body = render(m.question(sort_question()), QKEY)
        c = self.linear.add(ISSUE, body, FACTORY, cid="11111111-1111-4111-8111-111111111111")
        self.linear.add(ISSUE, "B", ROLANDO, parent=c.id)
        self.assertEqual(self.read().answers, ())

    def test_quoted_markers_in_a_progress_message_are_never_a_question(self) -> None:
        outside = f"factory-question: x options=A\nfactory-ref: {QKEY}\nfactory-candidate: {SHA}"
        key = "intake:evt-1"
        self.reporter.post(ISSUE, key, m.progress(m.Stage.STOPPED, outside))
        cid = comment_id(ISSUE, key)
        self.linear.add(ISSUE, "A", ROLANDO, parent=cid)
        self.linear.add(ISSUE, "Observation: looks fine", ROLANDO, parent=cid)
        r = self.read()
        self.assertEqual(r.answers, ())
        self.assertEqual(r.observations, ())
        [c] = self.decisions.comments(ISSUE)[:1]
        own = own_markers(c, ISSUE, FACTORY)
        self.assertIsNotNone(own)
        assert own is not None
        self.assertIsNone(own.question_key)
        self.assertIsNone(own.candidate)

    def test_raw_markers_posted_under_a_plain_key_are_defused(self) -> None:
        # Even text that skipped messages.py can't carry a marker through.
        key = "closed:evt-1"
        self.reporter.post(ISSUE, key, f"factory-candidate: {SHA}")
        cid = comment_id(ISSUE, key)
        self.linear.add(ISSUE, "Observation: ok", ROLANDO, parent=cid)
        r = self.read()
        self.assertEqual(r.observations, ())
        self.assertEqual(len(r.ignored), 1)

    def test_backslash_tricks_in_outside_text_are_not_markers(self) -> None:
        outside = "\n".join([r"factory\-question\: x options=A", rf"factory\-ref\: {QKEY}"])
        key = f"question:{ISSUE}:other"
        self.reporter.post(ISSUE, key, outside)  # no trailer: the last lines are outside text
        cid = comment_id(ISSUE, key)
        self.linear.add(ISSUE, "A", ROLANDO, parent=cid)
        self.assertEqual(self.read().answers, ())

    def test_factory_comment_by_bot_actor_is_not_trusted(self) -> None:
        qid = self.ask()
        self.linear.comments[qid].bot = {"type": "app", "name": "x"}
        self.linear.add(ISSUE, "B", ROLANDO, parent=qid)
        self.assertEqual(self.read().answers, ())


class ObservationTests(Base):
    def test_observation_in_a_ready_thread_binds_to_the_exact_commit(self) -> None:
        rid = self.ready()
        c = self.linear.add(ISSUE, "Observation: the list flickers", ROLANDO, parent=rid)
        [o] = self.read().observations
        self.assertEqual(o.commit, SHA)
        self.assertEqual(o.text, "the list flickers")
        self.assertEqual(o.comment_id, c.id)

    def test_observation_request_thread_binds_too(self) -> None:
        key = f"observe:{ISSUE}:{SHA[:12]}"
        self.reporter.post(ISSUE, key, m.observation_request(SHA, "Does it feel right?"))
        self.linear.add(ISSUE, "observation : fine", ROLANDO, parent=comment_id(ISSUE, key))
        [o] = self.read().observations
        self.assertEqual((o.commit, o.text), (SHA, "fine"))

    def test_two_candidates_keep_their_own_commits(self) -> None:
        other = "c" * 40
        r1, r2 = self.ready(), self.ready(other)
        self.linear.add(ISSUE, "Observation: one", ROLANDO, parent=r1)
        self.linear.add(ISSUE, "Observation: two", ROLANDO, parent=r2)
        got = {o.text: o.commit for o in self.read().observations}
        self.assertEqual(got, {"one": SHA, "two": other})

    def test_top_level_observation_is_ignored_with_a_reason(self) -> None:
        self.ready()
        self.linear.add(ISSUE, "Observation: nice", ROLANDO)
        r = self.read()
        self.assertEqual(r.observations, ())
        self.assertIn("thread", r.ignored[0].reason)

    def test_observation_by_others_is_ignored(self) -> None:
        rid = self.ready()
        self.linear.add(ISSUE, "Observation: nice", OTHER, parent=rid)
        r = self.read()
        self.assertEqual(r.observations, ())
        self.assertEqual(len(r.ignored), 1)

    def test_plain_reply_in_a_ready_thread_is_not_an_observation(self) -> None:
        rid = self.ready()
        self.linear.add(ISSUE, "LGTM", ROLANDO, parent=rid)
        r = self.read()
        self.assertEqual((r.observations, r.answers, r.ignored), ((), (), ()))


class TimeTests(Base):
    def test_time_minutes(self) -> None:
        for text, want in (
            ("time: 15m", 15),
            ("time: 1.5h", 90),
            ("Time : 2 hours", 120),
            ("time: 20 min\nmore notes", 20),
            ("time: 0.25h", 15),
            ("time: 15", None),
            ("spent time: 15m", None),
            ("time: -5m", None),
            ("time: 15m extra", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(time_minutes(text), want)

    def test_rolando_time_counts_others_do_not(self) -> None:
        self.linear.add(ISSUE, "time: 15m", ROLANDO)
        self.linear.add(ISSUE, "time: 1.5h", ROLANDO)
        self.linear.add(ISSUE, "time: 3h", OTHER)
        self.linear.add(ISSUE, "time: 3h", ROLANDO, bot={"type": "integration", "name": "x"})
        r = self.read()
        self.assertEqual(r.entered_minutes, 105)
        self.assertEqual(len(r.ignored), 2)

    def test_absurd_values_are_ignored(self) -> None:
        for text in ("time: 0m", "time: 13h", "time: 9999h", "time: 0.001h", "time: 0.4m"):
            self.linear.add(ISSUE, text, ROLANDO)
        r = self.read()
        self.assertIsNone(r.entered_minutes)
        self.assertEqual(r.time_entries, ())
        self.assertGreaterEqual(len(r.ignored), 4)

    def test_limit_is_inclusive(self) -> None:
        self.linear.add(ISSUE, f"time: {MAX_ENTRY_MINUTES}m", ROLANDO)
        self.assertEqual(self.read().entered_minutes, MAX_ENTRY_MINUTES)

    def test_edited_time_entry_is_ignored(self) -> None:
        c = self.linear.add(ISSUE, "time: 15m", ROLANDO)
        self.linear.edit(c.id, "time: 10h")
        r = self.read()
        self.assertIsNone(r.entered_minutes)
        self.assertIn("edited", r.ignored[0].reason)


class ReadingTests(Base):
    def test_pagination_reads_every_page(self) -> None:
        self.linear.page_size = 3
        qid = self.ask()
        for i in range(7):
            self.linear.add(ISSUE, f"chatter {i}", OTHER)
        self.linear.add(ISSUE, "time: 5m", ROLANDO)
        self.linear.add(ISSUE, "B", ROLANDO, parent=qid)
        self.assertEqual(len(self.decisions.comments(ISSUE)), 10)
        self.linear.calls.clear()
        r = self.read()
        self.assertEqual([a.option for a in r.answers], ["B"])
        self.assertEqual(r.entered_minutes, 5)
        pages = [v for q, v in self.linear.calls if "comments(first" in q]
        self.assertEqual(len(pages), 4)
        self.assertEqual(pages[0]["after"], None)

    def test_comment_repeated_across_pages_counts_once(self) -> None:
        # A cursor that doesn't move (or a shifting page) must not double count.
        self.linear.add(ISSUE, "time: 15m", ROLANDO)
        real = self.linear._page

        def stuck(issue, after):
            page = real(issue, None)
            page["issue"]["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "x"}
            return page

        self.linear._page = stuck  # type: ignore[method-assign]
        self.assertEqual(self.read().entered_minutes, 15)

    def test_malformed_nodes_are_skipped(self) -> None:
        qid = self.ask()
        self.linear.extra_nodes = [
            {"body": "B", "createdAt": "2026-10-08T12:00:00Z", "parent": {"id": qid}},
            {"id": "x1", "body": "B", "parent": {"id": qid}, "user": {"id": ROLANDO}},
            {"id": "x2", "body": "B", "createdAt": "yesterday", "user": {"id": ROLANDO}},
            {"id": "x3", "body": "B", "createdAt": 5, "user": {"id": ROLANDO}},
        ]
        self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        self.assertEqual([a.option for a in self.read().answers], ["A"])

    def test_non_object_node_is_skipped(self) -> None:
        qid = self.ask()
        self.linear.extra_nodes = [None, "junk", 7]
        self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        self.assertEqual([a.option for a in self.read().answers], ["A"])

    def test_node_with_time_but_no_zone_is_skipped(self) -> None:
        qid = self.ask()
        self.linear.extra_nodes = [
            {"id": "x4", "body": "B", "createdAt": "2026-10-08T12:00:00", "user": {"id": ROLANDO}}
        ]
        self.linear.add(ISSUE, "A", ROLANDO, parent=qid)
        self.assertEqual([a.option for a in self.read().answers], ["A"])

    def test_odd_but_valid_fields_parse(self) -> None:
        c = parse_comment(
            {"id": 5, "body": None, "createdAt": "2026-10-08T12:00:00Z", "user": "x", "parent": 1}
        )
        self.assertEqual((c.id, c.body, c.user_id, c.parent_id), ("5", "", None, None))

    def test_failures_raise_read_failed(self) -> None:
        for e in (LinearDown("down"), LinearRefused("no")):
            with self.subTest(e=e):
                d = LinearDecisions(FakeLinear(fail_next=[e]), ROLANDO)
                with self.assertRaises(ReadFailed):
                    d.read(ISSUE)

    def test_missing_issue_raises(self) -> None:
        linear = FakeLinear()
        linear._page = lambda issue, after: {"issue": None}  # type: ignore[method-assign]
        with self.assertRaises(ReadFailed):
            LinearDecisions(linear, ROLANDO).read(ISSUE)

    def test_key_acting_as_rolando_reads_nothing(self) -> None:
        linear = FakeLinear(viewer_id=ROLANDO)
        with self.assertRaises(ReadFailed):
            LinearDecisions(linear, ROLANDO).read(ISSUE)
        with self.assertRaises(ReadFailed):
            LinearDecisions(FakeLinear(viewer_id=OTHER), ROLANDO, FACTORY).read(ISSUE)
        with self.assertRaises(ReadFailed):
            LinearDecisions(FakeLinear(viewer_id=""), ROLANDO).read(ISSUE)


class PureTests(unittest.TestCase):
    def test_picked_option_without_options(self) -> None:
        self.assertIsNone(picked_option("A", ()))

    def test_picked_option_is_case_insensitive_and_returns_the_declared_id(self) -> None:
        self.assertEqual(picked_option("opt2", ["Opt2", "Opt3"]), "Opt2")
        self.assertEqual(picked_option("`B`", ["A", "B"]), "B")
        self.assertEqual(picked_option("B\nbecause", ["A", "B"]), "B")
        self.assertEqual(picked_option("B — newest", ["A", "B"]), "B")

    def test_judge_with_no_comments(self) -> None:
        r = judge([], ISSUE, ROLANDO, FACTORY)
        self.assertEqual((r.answers, r.observations, r.time_entries, r.ignored), ((),) * 4)
        self.assertIsNone(r.entered_minutes)


if __name__ == "__main__":
    unittest.main()
