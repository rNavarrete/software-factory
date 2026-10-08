"""ENG-156: structured findings from the two verifiers and the reviewer's block."""

import unittest
from dataclasses import replace

from redteam import fixtures as fx
from verify.criteria import Gate, Report
from verify.findings import (
    Route,
    Severity,
    blocking,
    carry_forward,
    finding_from_data,
    finding_id,
    for_route,
    from_reports,
    from_reviewer,
)

CODE = {
    "category": "code",
    "severity": "blocking",
    "summary": "filterByStatus mutates its input",
    "evidence": "src/books.ts:12",
    "suggested_action": "Return a new array.",
}


class ProblemRoutingTests(unittest.TestCase):
    def route(self, problem):
        (f,) = from_reports(None, None, commit=fx.HEAD, problems=[problem])
        return f.route, f.category

    def test_failed_ci_and_wrong_markers_go_to_repair(self):
        for problem, category in (
            (
                "The trusted CI run (https://github.com/x/y/actions/runs/1) did not pass:"
                " its verified job concluded failure.",
                "checks-failed",
            ),
            ("PR #7's title is 'x', not exactly 'y'.", "markers"),
            (
                "PR #7's body has no single Contract-Digest line naming the approved contract abc.",
                "markers",
            ),
            (
                "The CI run (u) checked a on base b, not c on d: its results may be for an"
                " older main. Re-run CI on the PR.",
                "stale-ci",
            ),
        ):
            self.assertEqual(self.route(problem), (Route.REPAIR, category), problem)

    def test_everything_else_is_rolandos(self):
        for problem in (
            "PR #7 merges into someone/else, not rNavarrete/factory-pilot-demo.",
            "PR #7 merges into branch 'release', not 'main'.",
            "PR #7's branch is 'x', not 'claude/filter-by-status-a1'.",
            "PR #7 is closed.",
            "PR #7 was opened by 'mallory', not the worker account, so it is not this"
            " attempt's output.",
            "The CI evidence names contract '000000000000', not the approved abc.",
            "The CI evidence could not be read: bad json.",
            # A repairable sentence that doesn't start where the collector puts it.
            "Note: the trusted CI run (u) did not pass",
        ):
            self.assertEqual(self.route(problem), (Route.ROLANDO, "integrity"), problem)


class GateTests(unittest.TestCase):
    def report(self, *gates):
        return Report(fx.DIGEST.value, fx.REPO, fx.HEAD, fx.MAIN, tuple(gates), ())

    def test_scope_and_checks_go_to_repair_and_the_contract_to_rolando(self):
        r = self.report(
            Gate("scope", False, "package.json is outside the permitted paths"),
            Gate("verification commands", False, "npm test failed"),
            Gate("contract", False, "does not match"),
            Gate("base", True, "ok"),
        )
        found = {f.category: f.route for f in from_reports(r, None, commit=fx.HEAD)}
        self.assertEqual(
            found,
            {"scope": Route.REPAIR, "checks-failed": Route.REPAIR, "integrity": Route.ROLANDO},
        )


class ReviewerFindingTests(unittest.TestCase):
    def test_categories_route_and_the_reviewer_is_named(self):
        items = [
            CODE,
            {**CODE, "category": "test", "summary": "the test checks nothing"},
            {**CODE, "category": "scope", "summary": "touches an unrelated file"},
            {**CODE, "category": "product", "summary": "label reads oddly"},
            {**CODE, "category": "security", "summary": "renders user text as HTML"},
        ]
        found = from_reviewer(items, reviewer="factory-verifier", commit=fx.HEAD)
        routes = {f.category: f.route for f in found}
        self.assertEqual(routes["review-code"], Route.REPAIR)
        self.assertEqual(routes["review-test"], Route.REPAIR)
        self.assertEqual(routes["review-scope"], Route.REPAIR)
        self.assertEqual(routes["review-product"], Route.ROLANDO)
        self.assertEqual(routes["review-security"], Route.ROLANDO)
        self.assertTrue(all(f.source == "factory-verifier" for f in found))
        self.assertTrue(all(f.commit == fx.HEAD for f in found))

    def test_malformed_entries_are_refused(self):
        for bad in (
            {**CODE, "category": "approval"},
            {**CODE, "severity": "critical"},
            {**CODE, "summary": "  "},
            {**CODE, "evidence": None},
            {k: v for k, v in CODE.items() if k != "suggested_action"},
            {**CODE, "criterion": 3},
        ):
            with self.assertRaises(ValueError, msg=bad):
                from_reviewer([bad], reviewer="r", commit=fx.HEAD)

    def test_the_reviewers_own_id_is_ignored(self):
        (a,) = from_reviewer([{**CODE, "id": "F-000000000000"}], reviewer="r", commit=fx.HEAD)
        self.assertNotEqual(a.id, "F-000000000000")
        self.assertEqual(a.id, finding_id("review-code", None, CODE["summary"]))

    def test_long_text_is_cut(self):
        (a,) = from_reviewer([{**CODE, "evidence": "x" * 5000}], reviewer="r", commit=fx.HEAD)
        self.assertEqual(len(a.evidence), 2000)

    def test_duplicates_collapse(self):
        found = from_reviewer([CODE, dict(CODE)], reviewer="r", commit=fx.HEAD)
        self.assertEqual(len(found), 1)


class LifecycleTests(unittest.TestCase):
    def test_the_same_problem_keeps_its_id_on_a_new_commit(self):
        (a,) = from_reviewer([CODE], reviewer="r", commit=fx.HEAD)
        (b,) = from_reviewer([CODE], reviewer="r", commit=fx.NEW_HEAD)
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.commit, b.commit)

    def test_a_finding_the_new_revision_no_longer_raises_is_resolved_once(self):
        (old,) = from_reviewer([CODE], reviewer="r", commit=fx.HEAD)
        now = carry_forward([old], [])
        self.assertEqual([(f.id, f.resolved) for f in now], [(old.id, True)])
        self.assertEqual(blocking(now), ())
        self.assertEqual(for_route(now, Route.REPAIR), ())
        # Reported once: carrying it forward again adds nothing.
        self.assertEqual(carry_forward(now, []), ())

    def test_a_finding_still_raised_stays_open(self):
        (old,) = from_reviewer([CODE], reviewer="r", commit=fx.HEAD)
        (new,) = from_reviewer([CODE], reviewer="r", commit=fx.NEW_HEAD)
        now = carry_forward([old], [new])
        self.assertEqual(now, (new,))
        self.assertEqual(blocking(now), (new,))

    def test_advisory_findings_never_block(self):
        (a,) = from_reviewer([{**CODE, "severity": "advisory"}], reviewer="r", commit=fx.HEAD)
        self.assertIs(a.severity, Severity.ADVISORY)
        self.assertEqual(blocking([a]), ())
        self.assertEqual(for_route([a], Route.REPAIR), (a,))

    def test_data_round_trip_names_every_field(self):
        (a,) = from_reviewer([CODE], reviewer="r", commit=fx.HEAD)
        data = a.as_data()
        self.assertEqual(
            set(data),
            {
                "id",
                "severity",
                "category",
                "route",
                "summary",
                "evidence",
                "suggested_action",
                "commit",
                "criterion",
                "resolved",
                "source",
            },
        )
        self.assertEqual(replace(a, resolved=True).as_data()["resolved"], True)


if __name__ == "__main__":
    unittest.main()


class RoutingEdgeTests(unittest.TestCase):
    def test_a_wrong_or_over_budget_branch_is_rolandos(self):
        r = Report(
            fx.DIGEST.value,
            fx.REPO,
            fx.HEAD,
            fx.MAIN,
            (Gate("branch", False, "attempt 4 is over the approved budget of 3"),),
            (),
        )
        (f,) = from_reports(r, None, commit=fx.HEAD)
        self.assertEqual((f.route, f.category), (Route.ROLANDO, "integrity"))

    def test_worker_text_ending_like_stale_ci_stays_integrity(self):
        problem = (
            "CI found the PR's contract claim malformed: x. The CI run (u) checked a on base b,"
            " not c on d: its results may be for an older main. Re-run CI on the PR."
        )
        (f,) = from_reports(None, None, commit=fx.HEAD, problems=[problem])
        self.assertIs(f.route, Route.ROLANDO)

    def test_failed_ci_has_one_id_whichever_way_it_was_found(self):
        (a,) = from_reports(
            None,
            None,
            commit=fx.HEAD,
            problems=["The trusted CI run (u) did not pass: its verified job concluded failure."],
        )
        r = Report(
            fx.DIGEST.value,
            fx.REPO,
            fx.HEAD,
            fx.MAIN,
            (Gate("verification commands", False, "npm test failed"),),
            (),
        )
        (b,) = from_reports(r, None, commit=fx.HEAD)
        self.assertEqual(a.id, b.id)

    def test_claimed_ids_must_be_well_formed(self):
        with self.assertRaises(ValueError):
            from_reviewer([{**CODE, "id": "anything"}], reviewer="r", commit=fx.HEAD)

    def test_ledger_findings_are_checked(self):
        (a,) = from_reviewer([CODE], reviewer="r", commit=fx.HEAD)
        self.assertEqual(finding_from_data(a.as_data()).id, a.id)
        for bad in (
            {**a.as_data(), "severity": "huge"},
            {**a.as_data(), "id": "F-1"},
            {**a.as_data(), "resolved": "yes"},
            "text",
        ):
            with self.assertRaises((KeyError, TypeError, ValueError)):
                finding_from_data(bad)
