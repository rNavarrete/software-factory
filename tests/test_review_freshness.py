"""A Linear handoff must describe the current review, including after an outage."""

from dataclasses import replace

from controller.approval import Approvals
from controller.attempts import events as ev
from controller.attempts.policy import LedgerView
from controller.interfaces import AttemptId, ContractDigest
from controller.ledger import SqliteLedgerStore
from controller.recovery import Recovery, State
from controller.report.linear_api import LinearDown
from controller.review.reviewer import ReviewState
from controller.service import queue as q
from tests.test_recovery import SHA_A
from tests.test_report_service import ISSUE, ReviewServiceCase, status
from tests.test_service import KEY, contract_for, yes


class FreshnessTests(ReviewServiceCase):
    def make_store(self):
        store = SqliteLedgerStore(self.root / "ledger.db")
        self.addCleanup(store.close)
        return store

    def queue_ready_during_outage(self):
        self.open_pr()
        self.linear.down = LinearDown("offline")
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.assertTrue(any(k.startswith("ready:") for k in self.view().outbox))

    def waiting(self, head="e" * 40):
        self.reviewer.status = status(ReviewState.WAITING_CI, head=head)

    def test_new_push_explains_that_the_earlier_ready_report_no_longer_applies(self):
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.waiting()
        self.tick(minutes=6)
        self.assertEqual(self.stages()[-1], "Reviewing")
        self.assertIn("not ready", self.bodies()[-1])
        self.assertIn("eeeeeee", self.bodies()[-1])
        before = len(self.bodies())
        self.tick(minutes=6)
        self.assertEqual(len(self.bodies()), before)

    def test_delayed_ready_is_discarded_after_new_push_and_restart(self):
        self.queue_ready_during_outage()
        self.waiting()
        self.restart()
        self.linear.down = None
        self.tick(minutes=6)
        self.assertNotIn("Ready for your review", self.stages())
        self.assertEqual(self.stages()[-1], "Reviewing")
        self.assertEqual(len(self.adapter.requests), 1)
        self.restart()
        before = len(self.bodies())
        self.tick(minutes=6)
        self.assertEqual(len(self.bodies()), before)

    def test_failed_refresh_holds_the_ready_message_until_a_fresh_check(self):
        self.queue_ready_during_outage()
        self.reviewer.status = None  # GitHub is unreadable, not a new passing verdict.
        self.linear.down = None
        self.restart()
        self.tick(minutes=6)
        self.assertNotIn("Ready for your review", self.stages())
        self.assertTrue(any(k.startswith("ready:") for k in self.view().outbox))
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.assertEqual(self.stages().count("Ready for your review"), 1)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_a_later_unreadable_round_cannot_reuse_the_previous_rounds_check(self):
        self.queue_ready_during_outage()
        self.reviewer.status = None
        self.linear.down = None
        self.tick(minutes=6)
        self.assertNotIn("Ready for your review", self.stages())
        self.assertTrue(any(k.startswith("ready:") for k in self.view().outbox))

    def test_a_new_attempt_does_not_leave_the_old_ready_message_blocking_the_outbox(self):
        self.queue_ready_during_outage()
        first = LedgerView.build(self.store.events()).task_attempts(self.task())[0]
        with self.store.writer_lock():
            self.store.append(
                ev.attempt_reserved(
                    AttemptId(self.task(), 2), ContractDigest(first.digest), self.now
                )
            )
        self.linear.down = None
        self.restart()
        self.tick(minutes=6)
        self.assertNotIn("Ready for your review", self.stages())
        self.assertFalse(any(k.startswith("ready:") for k in self.view().outbox))

    def test_a_closed_attempt_cannot_hide_the_notice_that_its_repair_is_paused(self):
        self.queue_ready_during_outage()
        attempt = AttemptId(self.task(), 1)
        desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        desk.record_clearing(
            attempt, ev.ClearingBasis.COMPLETED, "https://claude.ai/code/done", self.now
        )
        recovery = Recovery(self.store, desk, self.github, confirm=yes)
        recovery.close_attempt(attempt, State.FAILED, "tests failed", self.now)
        desk.authorize_repair(contract_for("ENG-186"), 2, "fix the tests", self.now)
        self.gate.hold("paused", "wait before repairing", self.now)
        self.linear.down = None
        self.restart()
        self.tick(minutes=6)
        self.assertNotIn("Ready for your review", self.stages())
        self.assertFalse(any(k.startswith("ready:") for k in self.view().outbox))
        self.assertEqual(self.stages()[-1], "Waiting")
        self.assertEqual(len(self.adapter.requests), 1)

    def test_a_keyless_review_status_is_reported_once_across_restart(self):
        self.open_pr()
        self.reviewer.status = replace(status(ReviewState.NOT_REVIEWABLE), key=None)
        self.tick(minutes=6)
        before = len(self.bodies())
        self.restart()
        self.tick(minutes=6)
        self.assertEqual(len(self.bodies()), before)

    def test_same_head_on_a_changed_base_requires_a_new_ready_report(self):
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        rev = replace(self.reviewer.status.revision, base="d" * 40)
        self.reviewer.status = replace(
            self.reviewer.status, state=ReviewState.WAITING_CI, revision=rev, key=rev.key()
        )
        self.tick(minutes=6)
        self.reviewer.status = replace(self.reviewer.status, state=ReviewState.PASSED)
        self.tick(minutes=6)
        self.assertEqual(self.stages().count("Ready for your review"), 2)
        self.assertEqual(self.stages()[-1], "Ready for your review")
        self.assert_one_comment_per_key()

    def test_a_restored_pass_on_the_same_revision_is_reported_after_invalidation(self):
        self.open_pr()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.waiting(head=SHA_A)
        self.tick(minutes=6)
        self.restart()
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.assertEqual(self.stages().count("Ready for your review"), 2)
        self.assertEqual(self.stages()[-1], "Ready for your review")

    def test_merge_delivers_closeout_without_an_obsolete_ready_request(self):
        self.queue_ready_during_outage()
        self.reviewer.head = SHA_A
        self.linear.down = None
        self.merge()
        self.assertNotIn("Ready for your review", self.stages())
        self.assertEqual(self.stages()[-1], "Merged")

    def test_legacy_queued_readiness_is_replaced_by_a_fresh_bound_report(self):
        self.open_pr()
        attempt = AttemptId(self.task(), 1)
        with self.store.writer_lock():
            self.store.append(
                q.message(
                    f"ready:{attempt}:{SHA_A}",
                    ISSUE,
                    "**Factory: Ready for your review**\n\nLegacy ready message",
                    self.now,
                )
            )
        self.reviewer.status = status(ReviewState.PASSED)
        self.tick(minutes=6)
        self.assertEqual(self.stages().count("Ready for your review"), 1)
        self.assertFalse(any("Legacy ready message" in b for b in self.bodies()))
