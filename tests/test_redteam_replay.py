"""Re-running the offline bypass cases on real records (ENG-158 live case
``live-offline-cases-on-real-loop``). The real run itself reads GitHub and is
done by hand; these tests hold the carrying-over to account offline."""

import dataclasses
import unittest

from redteam import CASES, Result
from redteam import fixtures as fx
from redteam import replay as rp

OTHER_HEAD = "1" * 40
OTHER_MAIN = "2" * 40
OTHER_RUN = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/999"


def relabelled() -> fx.Scenario:
    """The honest run with every commit and run link changed, as a real PR's would be."""
    return rp._swap(
        fx.honest(),
        [(fx.HEAD, OTHER_HEAD), (fx.MAIN, OTHER_MAIN), (fx.CI_URL, OTHER_RUN)],
    )


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.same = {r.case.id: r for r in rp.replay_all(fx.honest())}
        cls.other = {r.case.id: r for r in rp.replay_all(relabelled())}

    def test_replaying_on_the_made_up_run_gives_the_offline_results(self):
        for r in self.same.values():
            with self.subTest(case=r.case.id):
                self.assertTrue(r.same_as_offline, r.detail)

    def test_replaying_on_relabelled_records_gives_the_offline_results(self):
        self.assertTrue(fx.evaluate(relabelled()).ready)
        for r in self.other.values():
            with self.subTest(case=r.case.id):
                self.assertTrue(r.same_as_offline, r.detail)
                self.assertIsNot(r.result, rp.Replayed.ERROR, r.detail)

    def test_most_record_cases_are_replayed(self):
        replayed = [r for r in self.other.values() if r.result is not rp.Replayed.NOT_REPLAYABLE]
        self.assertGreaterEqual(len(replayed), 60)
        for r in replayed:
            with self.subTest(case=r.case.id):
                self.assertTrue(rp.replayable(r.case))

    def test_every_replayed_change_reaches_the_real_records(self):
        """Carrying a change over never silently drops it: the real scenario
        always differs from the baseline afterwards."""
        real = relabelled()
        for c in CASES:
            if self.other[c.id].result is rp.Replayed.NOT_REPLAYABLE:
                continue
            with self.subTest(case=c.id):
                self.assertNotEqual(rp.carry_over(c.check.make(), real), real)

    def test_carried_changes_use_the_real_names(self):
        real = relabelled()
        stale = next(c for c in CASES if c.id == "stale-ci-result")
        moved = rp.carry_over(stale.check.make(), real)
        self.assertTrue(all(r.commit != OTHER_HEAD for r in moved.results))
        self.assertTrue(all(r.url == OTHER_RUN for r in moved.results))
        body = next(c for c in CASES if c.id == "body-digest-mismatch")
        moved = rp.carry_over(body.check.make(), real)
        self.assertIn("0" * 64, moved.candidate.pr_body)

    def test_text_edits_and_launch_cases_stay_offline_only(self):
        for case_id in (
            "delete-existing-test",
            "fourth-attempt-launch",
            "approval-written-by-worker",
        ):
            with self.subTest(case=case_id):
                self.assertIs(self.other[case_id].result, rp.Replayed.NOT_REPLAYABLE)

    def test_a_baseline_that_is_not_ready_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not ready"):
            rp.replay_all(fx.honest().but(clearances=()))

    def test_a_dropped_change_would_show_as_got_through(self):
        case = next(c for c in CASES if c.id == "stale-ci-result")
        no_op = dataclasses.replace(case, check=_check_returning(fx.honest()))
        r = rp.replay_case(no_op, relabelled(), Result.BLOCKED)
        self.assertIs(r.result, rp.Replayed.GOT_THROUGH)
        self.assertFalse(r.same_as_offline)


class StandInTest(unittest.TestCase):
    def test_stand_ins_cover_only_what_rolando_decides_and_say_so(self):
        bare = fx.honest().but(observations=(), clearances=())
        filled = rp.stand_in_decisions(bare)
        self.assertTrue(fx.evaluate(filled).ready)
        self.assertEqual([o.criterion for o in filled.observations], ["ac3"])
        self.assertEqual([c.flag for c in filled.clearances], [fx.FILE_FLAG])
        for record in (*filled.observations, *filled.clearances):
            self.assertIn(rp.STAND_IN, getattr(record, "seen", "") + getattr(record, "note", ""))

    def test_stand_ins_never_clear_a_gap(self):
        gap = fx.honest().but(links=(fx.link("ac2"),), proofs=(fx.proof("ac2"),))
        self.assertFalse(fx.evaluate(rp.stand_in_decisions(gap)).ready)

    def test_usage(self):
        said = []
        self.assertEqual(rp.main([], said.append), 2)
        self.assertIn("usage", said[0])


def _check_returning(scenario):
    def check():
        raise AssertionError("not called by replay")

    check.make = lambda: scenario
    check.expect = ("stale",)
    return check


if __name__ == "__main__":
    unittest.main()
