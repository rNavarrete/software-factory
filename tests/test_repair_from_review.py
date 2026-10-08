"""The repair step reading the real automatic review (ENG-156 → ENG-160).

``ReviewFailures`` over the actual ``AutoReviewer``, not a test double: a
failed review becomes a repair plan, and verdicts that aren't a final failure
never do."""

from __future__ import annotations

import unittest

from controller.repair import policy
from controller.repair.review import ReviewFailures
from controller.review.reviewer import ReviewState
from redteam import fixtures as fx
from tests.github_world import ATTEMPT, NUMBER, World
from tests.test_review_auto import CODE_FINDING, PR, Base, his_answers, review_for


def plan(findings):
    return policy.plan(
        findings,
        attempts_used=1,
        contract_budget=3,
        project_max=3,
        allowance=1,
        repairs_used=0,
    )


class RepairFromRealReviewTests(Base):
    def reviewed(self, **block):
        w = World()
        w.comments = [review_for(fx.HEAD, **block)]
        r = self.reviewer(world=w, decisions=his_answers)
        r.start(PR, "req-1")
        return r, r.check(ATTEMPT)

    def test_a_failed_review_becomes_a_repair_plan(self):
        r, status = self.reviewed(findings=[CODE_FINDING])
        self.assertIs(status.state, ReviewState.FAILED, status.note)
        report = ReviewFailures(r).failure(ATTEMPT)
        self.assertIsNotNone(report)
        self.assertEqual((report.attempt, report.pr, report.head), (ATTEMPT, NUMBER, fx.HEAD))
        ids = {f.id for f in status.for_repair()}
        self.assertTrue(ids <= {f.id for f in report.findings})
        self.assertTrue(all(f.route == "repair" for f in report.findings))
        out = plan(report.findings)
        self.assertIsInstance(out, policy.Plan, out)
        self.assertEqual(out.attempt, 2)

    def test_a_passed_review_is_nothing_to_repair(self):
        r, status = self.reviewed()
        self.assertIsNot(status.state, ReviewState.FAILED, status.note)
        self.assertIsNone(ReviewFailures(r).failure(ATTEMPT))

    def test_the_service_reads_the_real_review_when_there_is_one(self):
        from controller.service.fixtures import NoRepair
        from controller.service.main import _repair

        r, _ = self.reviewed(findings=[CODE_FINDING])
        wired = _repair(r)
        self.assertIsInstance(wired, ReviewFailures)
        self.assertIsNotNone(wired.failure(ATTEMPT))
        self.assertIsInstance(_repair(None), NoRepair)

    def test_no_review_yet_is_nothing_to_repair(self):
        r = self.reviewer(world=World(), decisions=his_answers)
        self.assertIsNone(ReviewFailures(r).failure(ATTEMPT))


if __name__ == "__main__":
    unittest.main()
