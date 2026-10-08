"""ENG-158: real Codex verdict -> service merge record -> Linear comment.

External GitHub/Linear answers are synthetic. The reviewer, SQLite ledger,
service close-out and reporter are real. Removing the close-out's fresh check
must turn retargeted/changed/unreadable merges into false successes here.
"""

from types import SimpleNamespace

from controller.interfaces import LedgerEvent
from controller.report.reporter import LinearReporter
from controller.review.reviewer import ReviewState
from controller.service import queue as q
from controller.service.seams import Integrations
from controller.service.service import Service, TickReport
from redteam import fixtures as fx
from tests.github_world import ATTEMPT
from tests.linear_world import ROLANDO, FakeLinear
from tests.test_review_workflow import Case


class MergeQualificationTests(Case):
    def setUp(self):
        super().setUp()
        self.review = self.started()
        self.finish()
        self.assertIs(self.review.check(ATTEMPT).state, ReviewState.PASSED)
        self.linear = FakeLinear()
        self.item = q.Item(
            "evt-merge",
            ATTEMPT.task,
            "issue-ENG-158",
            "ENG-158",
            "pilot",
            "Rolando",
            self.now.isoformat(),
            "0" * 64,
            1,
        )
        with self.store.writer_lock():
            self.store.append(LedgerEvent(q.REVIEW_STARTED, self.now, ATTEMPT.task, ATTEMPT))
        self.world.pr.update(state="closed", merged=True)

    def close(self):
        # A new reviewer/process reads its saved passing verdict after the merge.
        reviewer = self.reviewer()
        service = Service(
            self.store,
            None,
            None,
            None,
            None,
            lambda: None,
            Integrations(None, None, LinearReporter(self.linear, ROLANDO), reviewer, None),
            now=lambda: self.now,
        )
        service._close_merged(
            self.item, SimpleNamespace(attempt=ATTEMPT, pull_requests=(7,)), self.now
        )
        service._flush(TickReport())
        self.assertEqual(len(self.runtime.texts), 1, "Closing must not launch another review")
        [comment] = self.linear.on(self.item.issue_id)
        return comment.body

    def assert_exception(self):
        self.assertTrue(self.close().startswith("**Factory: Merged as an exception**"))

    def test_same_reviewed_revision_is_verified_after_restart(self):
        body = self.close()
        self.assertTrue(body.startswith("**Factory: Merged**"), body)
        self.assertIn("passed on that exact commit", body)

    def test_retargeted_merge_with_the_same_head_is_an_exception(self):
        self.world.pr["base"]["ref"] = "release"
        self.assert_exception()

    def test_changed_contract_marker_with_the_same_head_is_an_exception(self):
        self.world.pr["body"] = "The approved contract marker was removed."
        self.assert_exception()

    def test_changed_base_with_the_same_head_is_an_exception(self):
        self.world.pr["base"]["sha"] = fx.NEW_MAIN
        self.assert_exception()

    def test_unreadable_check_never_falls_back_to_the_saved_pass(self):
        # The PR/head can still be read; the fresh evidence collection cannot.
        self.world.fail.add(f"repos/{fx.REPO}/pulls/7/files")
        self.assert_exception()

    def test_new_head_merged_without_review_is_an_exception(self):
        self.world.pr["head"]["sha"] = "e" * 40
        self.assert_exception()
