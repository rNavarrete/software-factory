"""Re-running the offline bypass cases on real records (ENG-158 live case
``live-offline-cases-on-real-loop``). The real run itself reads GitHub and is
done by hand; these tests hold the carrying-over to account offline."""

import dataclasses
import json
import unittest
from dataclasses import replace

from controller.adapter import routine
from controller.contract import digest, freeze
from controller.interfaces import AttemptId, TaskId
from redteam import CASES, Result
from redteam import fixtures as fx
from redteam import replay as rp
from tests.github_world import NUMBER, PR_URL, World

OTHER_HEAD = "3f2a9c41d0b7e6f5a8c9d0e1f2a3b4c5d6e7f809"
OTHER_MAIN = "7b1e0c2d3e4f5061728394a5b6c7d8e9f0a1b2c3"
OTHER_RUN = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/18234567"
OTHER_PROOF = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/18234999"
OTHER_FILE = "tests/status.test.ts"
SECOND_FILE = "tests/filter.test.ts"
AC1_IT = "returns only the books with that status, in their original order"
OTHER_IT = "keeps just the books in that status, in the same order"


def other_contract(edit=None):
    """Another task: its own id, digest, budget and test file."""
    c = json.loads(fx.EXAMPLE.read_text())
    c["task_id"] = "status-filter"
    c["attempt_budget"] = 2
    paths = [p for p in c["permitted_paths"] if p != fx.TEST_FILE]
    c["permitted_paths"] = [*paths, OTHER_FILE, SECOND_FILE]
    if edit:
        edit(c)
    return freeze(c)


def relabelled(contract=None) -> fx.Scenario:
    """The honest run with every name a real PR would change changed: commits,
    run links, digest, task, test file and a test name."""
    contract = contract or other_contract()
    approved = digest(contract)
    names = [
        (fx.HEAD, OTHER_HEAD),
        (fx.MAIN, OTHER_MAIN),
        (fx.CI_URL, OTHER_RUN),
        (fx.CC_URL, OTHER_RUN),
        (fx.PROOF_URL, OTHER_PROOF),
        (str(fx.DIGEST), str(approved)),
        (fx.DIGEST.short, approved.short),
        (str(fx.CONTRACT["task_id"]), contract["task_id"]),
        (fx.TEST_FILE, OTHER_FILE),
        (AC1_IT, OTHER_IT),
    ]
    return rp._swap(fx.honest().but(contract={}), names).but(contract=contract, approved=approved)


def two_test_files() -> fx.Scenario:
    """ac2's test lives in a second new file."""
    s = relabelled()
    text = (
        "import { describe, expect, it } from 'vitest';\n"
        "import { filterByStatus, type Book } from '../src/books';\n" + fx.FILTER_TESTS
    ).replace(AC1_IT, OTHER_IT)
    return s.but(
        candidate=replace(s.candidate, changed_paths=(*s.candidate.changed_paths, SECOND_FILE)),
        sources=(
            *s.sources,
            fx.source(text, OTHER_HEAD, SECOND_FILE),
            fx.source(None, fx.BASE, SECOND_FILE),
        ),
        links=(s.links[0], replace(s.links[1], path=SECOND_FILE)),
        proofs=(s.proofs[0], replace(s.proofs[1], path=SECOND_FILE)),
    )


def with_limit() -> fx.Scenario:
    """ac1 has a recorded reason instead of a failure proof."""
    s = relabelled()
    limit = fx.proof_limit(
        "ac1", contract_digest=str(s.approved), commit=OTHER_HEAD, base_commit=OTHER_MAIN
    )
    return s.but(proofs=s.proofs[1:], limits=(limit,))


def observable_named(cid: str) -> fx.Scenario:
    def edit(c):
        c["acceptance_criteria"][2]["id"] = cid

    s = relabelled(other_contract(edit))
    return s.but(observations=tuple(replace(o, criterion=cid) for o in s.observations))


def nothing_by_hand() -> fx.Scenario:
    def edit(c):
        del c["acceptance_criteria"][2]

    return relabelled(other_contract(edit)).but(observations=())


def by_id(real: fx.Scenario) -> dict[str, rp.ReplayResult]:
    return {r.case.id: r for r in rp.replay_all(real)}


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
        self.assertGreaterEqual(len(replayed), 67)
        self.assertLessEqual(len(self.other) - len(replayed), 57)
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


class RealShapesTest(unittest.TestCase):
    """Real PRs differ from the made-up one in shape, not only in names."""

    def assert_same_as_offline(self, results, *, at_least):
        for r in results.values():
            with self.subTest(case=r.case.id):
                self.assertTrue(r.same_as_offline, f"{r.result.value}: {r.detail}")
        blocked = [r for r in results.values() if r.result is rp.Replayed.BLOCKED]
        self.assertGreaterEqual(len(blocked), at_least)

    def test_tests_in_two_files(self):
        self.assert_same_as_offline(by_id(two_test_files()), at_least=67)

    def test_by_hand_criterion_with_another_id(self):
        results = by_id(observable_named("ac4"))
        self.assert_same_as_offline(results, at_least=67)
        self.assertIn("ac4", results["stale-observation-after-push"].detail)

    def test_a_task_with_nothing_checked_by_hand_says_so(self):
        results = by_id(nothing_by_hand())
        self.assert_same_as_offline(results, at_least=50)
        why = {r.detail for r in results.values() if r.result is rp.Replayed.NOT_REPLAYABLE}
        self.assertIn("this task has no criterion checked by hand", why)
        self.assertIs(results["stale-observation-after-push"].result, rp.Replayed.NOT_REPLAYABLE)

    def test_a_limit_instead_of_a_proof_never_hides_a_case(self):
        """Cases about ac1's proof can't reach a run that has none; they show
        as differences rather than quietly dropping out or getting through."""
        results = by_id(with_limit())
        for r in results.values():
            with self.subTest(case=r.case.id):
                self.assertIsNot(r.result, rp.Replayed.GOT_THROUGH, r.detail)
                self.assertIsNot(r.result, rp.Replayed.WRONG_REASON, r.detail)
        missing = [r for r in results.values() if r.result is rp.Replayed.NO_COUNTERPART]
        self.assertTrue(missing)
        self.assertTrue(all(not r.same_as_offline for r in missing))
        self.assertTrue(all("proof" in r.detail for r in missing))

    def test_a_reason_the_honest_run_already_gives_is_unclear(self):
        s = relabelled()
        s = s.but(results=(*s.results, replace(s.results[0], url="")))
        results = by_id(s)
        self.assertIs(results["ci-without-link"].result, rp.Replayed.UNCLEAR)
        self.assertFalse(results["ci-without-link"].same_as_offline)

    def test_the_summary_names_what_was_left_out_and_every_stand_in(self):
        real = rp.stand_in_decisions(nothing_by_hand().but(clearances=()))
        text = rp.summary(rp.replay_all(real), "the PR", real)
        self.assertIn("x this task has no criterion checked by hand", text)
        self.assertIn(f"look at flag changed-test:{OTHER_FILE}", text)


class CollectRealTest(unittest.TestCase):
    def merged_second_attempt(self) -> World:
        w = World()
        w.runs[0]["pull_requests"] = []  # what GitHub shows once the PR is merged
        w.pr["state"] = "closed"
        a2 = AttemptId(TaskId("filter-by-status"), 2)
        w.pr["head"]["ref"] = a2.branch
        w.pr["title"] = routine.pr_title(a2, fx.DIGEST)
        return w

    def test_a_merged_second_attempt_replays(self):
        real, url = rp.collect_real(str(fx.EXAMPLE), NUMBER, api=self.merged_second_attempt())
        self.assertEqual(url, PR_URL)
        self.assertTrue(fx.evaluate(real).ready)
        said = []
        self.assertEqual(rp.main([str(fx.EXAMPLE), str(NUMBER)], said.append, api=World()), 0)
        self.assertIn("differ from the offline run: 0", said[0])

    def test_a_merged_pr_still_needs_the_run_report_for_its_base(self):
        w = self.merged_second_attempt()
        w.pr["base"]["sha"] = "f" * 40
        said = []
        self.assertEqual(rp.main([str(fx.EXAMPLE), str(NUMBER)], said.append, api=w), 1)
        self.assertIn("older main", said[0])


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

    def test_stand_ins_refuse_a_real_problem(self):
        for flagged in (
            fx.honest().but(control_change=fx.control_change(flagged=True, reasons=("ci.yml",))),
            fx.honest().but(
                proofs=(fx.proof("ac1", outcome=fx.ProofOutcome.PASSED), fx.proof("ac2"))
            ),
        ):
            with self.subTest(flags=fx.evaluate(flagged).assertions.open_flags):
                with self.assertRaisesRegex(ValueError, "stand-in may not clear"):
                    rp.stand_in_decisions(flagged.but(clearances=()))

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
