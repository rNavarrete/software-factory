"""Fixes for what the first live delivery runs found (ENG-163).

Each class is one finding from planning/eng-145/live-results.md: a clearing
note about something else cleared a flag, an off-topic note counted as an
observation, a missing observation was worded as unfixable, the "look at it"
hint always said to start the app, a review comment stopped counting in
silence when main moved, a merge before the loop's ready verdict went
unmentioned, and a sample went stale after an earlier merge.

Everything runs on the fakes test_loop uses. Nothing touches the network.
"""

import unittest

from controller.approval import ApprovalRefused
from controller.ledger import kinds
from controller.loop.check import assess
from controller.loop.decisions import (
    CLEARED,
    ReviewDecisions,
    flag_label,
    flag_names,
    names_flag,
)
from controller.loop.loop import EXIT_READY, EXIT_STOPPED, EXIT_WAITING, _look_hint
from redteam import fixtures as fx
from tests.github_world import ATTEMPT, NUMBER, REPO, World
from tests.test_dispatch import KEY, NOW
from tests.test_loop import LoopCase, MemoryLedger, collected
from verify.criteria import Verdict

GOOD_NOTE = "Read books.test.ts: only the import changed."


class FlagNamesTests(unittest.TestCase):
    def test_names_for_each_kind_of_subject(self):
        self.assertEqual(
            flag_names("changed-test:tests/books.test.ts"),
            ("books.test.ts", "tests/books.test.ts"),
        )
        self.assertEqual(
            set(flag_names("may-not-run:tests/books.test.ts > filterByStatus > keeps order")),
            {
                "tests/books.test.ts > filterByStatus > keeps order",
                "tests/books.test.ts",
                "books.test.ts",
                "keeps order",
            },
        )
        self.assertEqual(flag_names("passes-without-change:ac4"), ("ac4",))
        self.assertIn("ci", flag_names("control-change:ci-report"))

    def test_a_note_must_name_the_subject_as_a_whole_word(self):
        flag = "changed-test:tests/books.test.ts"
        for note in ("books.test.ts is fine", "Looked at tests/books.test.ts.", "BOOKS.TEST.TS ok"):
            with self.subTest(note=note):
                self.assertTrue(names_flag(flag, note))
        for note in (
            "The Clear finished button works.",  # the first live run's mistake
            "checked the books tests",
            "mybooks.test.ts is fine",
            "books.test.tsx is fine",
            "",
        ):
            with self.subTest(note=note):
                self.assertFalse(names_flag(flag, note))

    def test_the_label_shown_is_one_of_the_names(self):
        for flag in (
            "changed-test:tests/books.test.ts",
            "may-not-run:tests/books.test.ts > filterByStatus > keeps order",
            "passes-without-change:ac4",
            "control-change:ci-report",
            "control-change:.github/workflows/ci.yml",
        ):
            with self.subTest(flag=flag):
                self.assertTrue(names_flag(flag, f"Looked at {flag_label(flag)}: fine."))

    def test_criterion_flag_needs_the_criterion_id(self):
        self.assertTrue(names_flag("passes-without-change:ac4", "ac4: accepted, weak test"))
        self.assertFalse(names_flag("passes-without-change:ac4", "ac44 looked fine"))
        self.assertFalse(names_flag("passes-without-change:ac4", "the rename tests are fine"))


class ClearingNoteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.decisions = ReviewDecisions(self.store, KEY, os_user="rolando")
        self.cand = fx.candidate()

    def test_note_about_something_else_is_refused_and_not_written(self):
        with self.assertRaises(ApprovalRefused):
            self.decisions.clear(
                ATTEMPT, fx.DIGEST, self.cand, fx.FILE_FLAG, "The button works fine.", NOW
            )
        self.assertEqual(self.store.events(), [])

    def test_signed_record_whose_note_names_nothing_is_never_read_back(self):
        # As if written before this rule existed, signed with the real key.
        self.decisions._write(
            ATTEMPT,
            fx.DIGEST,
            self.cand,
            CLEARED,
            NOW,
            flag=fx.FILE_FLAG,
            note="The Clear finished button works.",
        )
        self.assertEqual(self.decisions.clearances(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_note_naming_another_flag_does_not_clear_this_one(self):
        other = "changed-setup:tests/setup.ts"
        with self.assertRaises(ApprovalRefused):
            self.decisions.clear(ATTEMPT, fx.DIGEST, self.cand, other, GOOD_NOTE, NOW)


class LoopFindingCase(LoopCase):
    def ready_world(self):
        self.open_pr()

    def text(self):
        return "\n".join(self.said)

    def clearances(self):
        return ReviewDecisions(self.store, KEY).clearances(
            ATTEMPT, fx.DIGEST, collected().candidate
        )

    def observations(self):
        return ReviewDecisions(self.store, KEY).observations(
            ATTEMPT, fx.DIGEST, collected().candidate
        )


class LoopClearingTests(LoopFindingCase):
    def test_note_about_the_button_leaves_the_test_file_flag_open(self):
        self.ready_world()
        code = self.run_loop("y\n\nThe Clear finished button removes done books.\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertIn("a note clears only the thing it names", self.text())
        self.assertIn("books.test.ts", self.text())
        self.assertEqual(self.clearances(), ())

    def test_then_a_note_naming_the_file_clears_it(self):
        self.ready_world()
        self.assertEqual(self.run_loop("y\n\nThe button works.\n"), EXIT_STOPPED)
        self.assertEqual(self.run_loop(f"{GOOD_NOTE}\n"), EXIT_READY, self.text())
        (c,) = self.clearances()
        self.assertEqual(c.note, GOOD_NOTE)


class LoopObservationTests(LoopFindingCase):
    def test_off_topic_note_is_asked_again_and_enter_records_the_expected(self):
        self.ready_world()
        code = self.run_loop(f"y\nthe new code changes\n\n{GOOD_NOTE}\n")
        self.assertEqual(code, EXIT_READY, self.text())
        (o,) = self.observations()
        self.assertEqual(o.verdict, Verdict.PASS)
        self.assertIn("Saw what was expected", o.seen)
        self.assertNotIn("new code changes", o.seen)

    def test_off_topic_twice_records_nothing(self):
        self.ready_world()
        code = self.run_loop(f"y\nthe new code changes\nlooks good to me\n{GOOD_NOTE}\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertIn("Skipped ac3", self.text())
        self.assertEqual(self.observations(), ())

    def test_note_about_the_check_is_kept_as_typed(self):
        self.ready_world()
        seen = "Picked reading in the filter and only that book was listed."
        code = self.run_loop(f"y\n{seen}\n{GOOD_NOTE}\n")
        self.assertEqual(code, EXIT_READY, self.text())
        (o,) = self.observations()
        self.assertEqual(o.seen, seen)

    def test_a_no_is_never_second_guessed(self):
        self.ready_world()
        code = self.run_loop(f"n\nnothing happened at all\n{GOOD_NOTE}\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        (o,) = self.observations()
        self.assertEqual(o.verdict, Verdict.FAIL)


class MissingObservationWordingTests(unittest.TestCase):
    def test_owed_observation_says_what_is_missing_not_unfixable(self):
        a = assess(fx.CONTRACT, fx.DIGEST, collected())
        text = "\n".join(a.blockers)
        self.assertIn("ac3 needs your own look", text)
        self.assertNotIn("cannot be cleared", text)
        self.assertNotIn("ac3 is unknown", text)
        self.assertEqual(sum("ac3" in b for b in a.blockers), 1)

    def test_a_real_gap_keeps_its_wording(self):
        w = World()
        w.comments = []  # no mapping: ac1 and ac2 are real gaps
        a = assess(fx.CONTRACT, fx.DIGEST, collected(w), observations=(fx.observation(),))
        text = "\n".join(a.blockers)
        self.assertIn("ac1 is uncovered", text)
        self.assertIn("cannot be cleared", text)
        self.assertNotIn("needs your own look", text)


class LookHintTests(unittest.TestCase):
    def test_app_change_says_how_to_run_the_app(self):
        hint = _look_hint(REPO, fx.candidate())
        self.assertIn(f"https://github.com/{REPO}/tree/{fx.HEAD}", hint)
        self.assertIn("npm run dev", hint)

    def test_readme_only_change_needs_no_dev_server(self):
        from dataclasses import replace

        cand = replace(fx.candidate(), changed_paths=("README.md",))
        hint = _look_hint(REPO, cand)
        self.assertIn(f"https://github.com/{REPO}/tree/{fx.HEAD}", hint)
        self.assertNotIn("npm", hint)


class StaleReviewTests(LoopFindingCase):
    def test_main_moving_under_the_review_is_said_not_silent(self):
        self.ready_world()
        self.world.pr["base"]["sha"] = "e" * 40  # main moved after the review was posted
        answers = f"y\n\n{GOOD_NOTE}\n"
        code = self.run_loop(answers, wait=0)
        self.assertEqual(code, EXIT_STOPPED, self.text())
        text = self.text()
        self.assertIn("Review comment not used:", text)
        self.assertIn("main moved after the review was posted", text)
        self.assertIn("Update branch", text)
        self.assertNotIn("python3 -m controller repair", text)  # not the worker's fault
        self.assertNotIn("Ready for your review", text)
        self.assertEqual(self.stdin.read(), answers)  # nothing asked of him

    def test_review_missing_its_new_comment_says_why_while_waiting(self):
        # Everything else re-collected for the new main; only the review is old.
        from verify.review import review_block

        self.ready_world()
        old = fx.candidate()
        moved = type(old)(**{**old.__dict__, "base_commit": "e" * 40})
        self.world.comments[0]["body"] = review_block(str(fx.DIGEST), moved)
        code = self.run_loop("", wait=0)
        self.assertEqual(code, EXIT_WAITING, self.text())
        self.assertIn("waiting for the independent review comment", self.text())
        self.assertIn("Not used:", self.text())


class MergedBeforeReadyTests(LoopFindingCase):
    def failures(self):
        return [
            e for e in self.events(kinds.FAILURE) if e.data.get("stage") == "merged-before-ready"
        ]

    def test_merge_with_no_ready_verdict_is_said_and_recorded_once(self):
        self.open_pr(merged=True)
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertIn(f"PR #{NUMBER} was merged at commit {fx.HEAD[:12]} before", self.text())
        self.assertIn("never checked that commit", self.text())
        (f,) = self.failures()
        self.assertIn(fx.HEAD, f.data["detail"])
        self.assertEqual(f.attempt, ATTEMPT)
        # A rerun says it again but records nothing new.
        self.said.clear()
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertIn("before this loop said it was ready", self.text())
        self.assertEqual(len(self.failures()), 1)

    def test_merge_while_answers_were_still_missing_names_the_last_verdict(self):
        self.open_pr()
        self.assertEqual(self.run_loop("\n\n"), EXIT_STOPPED)  # not ready: answers missing
        self.open_pr(merged=True)
        self.said.clear()
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertIn("its last check said action_required", self.text())
        self.assertEqual(len(self.failures()), 1)

    def test_merge_after_ready_says_nothing_and_records_nothing(self):
        self.open_pr()
        self.assertEqual(self.run_loop(f"y\n\n{GOOD_NOTE}\n"), EXIT_READY, self.text())
        self.open_pr(merged=True)
        self.said.clear()
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertNotIn("before this loop said it was ready", self.text())
        self.assertEqual(self.failures(), [])

    def test_merge_of_a_later_push_than_the_ready_one_is_recorded(self):
        self.open_pr()
        self.assertEqual(self.run_loop(f"y\n\n{GOOD_NOTE}\n"), EXIT_READY, self.text())
        self.world.pr["head"]["sha"] = fx.NEW_HEAD
        self.open_pr(merged=True)
        self.said.clear()
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertIn(f"merged at commit {fx.NEW_HEAD[:12]} before", self.text())
        self.assertEqual(len(self.failures()), 1)

    def test_unreadable_pr_warns_and_still_closes_out(self):
        self.open_pr(merged=True)
        self.world.fail.add(f"repos/{REPO}/pulls/")
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertIn("couldn't read PR", self.text())
        self.assertEqual(self.failures(), [])


class StaleBaseTests(LoopFindingCase):
    def test_main_changed_a_permitted_file_asks_and_enter_starts_nothing(self):
        self.world.main_changes = [{"filename": "src/books.ts"}, {"filename": "docs/x.md"}]
        before = len(self.store.events())
        code = self.run_loop("\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertIn("Main has changed since this contract's base", self.text())
        self.assertIn("src/books.ts", self.text())
        self.assertNotIn("docs/x.md", self.text())
        self.assertIn("Not started", self.text())
        self.assertEqual(self.adapter.requests, [])
        # Only his timed minutes were recorded; no dispatch record of any kind.
        kinds_written = {s.event.kind for s in self.store.events()[before:]}
        self.assertLessEqual(kinds_written, {kinds.HUMAN_TIME})

    def test_y_starts_it_anyway(self):
        self.world.main_changes = [{"filename": "src/books.ts"}]
        self.assertEqual(self.run_loop("y\n", wait=0), EXIT_WAITING, self.text())
        self.assertEqual(len(self.adapter.requests), 1)

    def test_no_terminal_starts_nothing(self):
        self.world.main_changes = [{"filename": "src/books.ts"}]
        self.loop = self.build("")
        import io

        from controller.loop.loop import Asker

        self.loop.asker = Asker(io.StringIO("y\n"), io.StringIO())
        self.assertEqual(self.loop.run(self.contract, wait_minutes=0), EXIT_STOPPED)
        self.assertEqual(self.adapter.requests, [])

    def test_changes_outside_the_task_ask_nothing(self):
        self.world.main_changes = [{"filename": "README.md"}]
        self.assertEqual(self.run_loop("", wait=0), EXIT_WAITING, self.text())
        self.assertNotIn("Main has changed", self.text())
        self.assertEqual(len(self.adapter.requests), 1)

    def test_unreadable_compare_warns_and_carries_on(self):
        self.world.fail.add(f"repos/{REPO}/compare/")
        self.assertEqual(self.run_loop("", wait=0), EXIT_WAITING, self.text())
        self.assertIn("couldn't check whether main moved", self.text())
        self.assertEqual(len(self.adapter.requests), 1)

    def test_a_rerun_of_a_started_task_does_not_check_again(self):
        self.assertEqual(self.run_loop("", wait=0), EXIT_WAITING)
        self.world.main_changes = [{"filename": "src/books.ts"}]
        self.world.calls.clear()
        self.said.clear()
        self.assertEqual(self.run_loop("", wait=0), EXIT_WAITING, self.text())
        self.assertFalse(any("/compare/" in c for c in self.world.calls))
        self.assertNotIn("Main has changed", self.text())


if __name__ == "__main__":
    unittest.main()
