"""Assertion map (ENG-157). Each test class names the acceptance criterion it covers.

AC1: report criterion -> assertion/evidence -> exact tested commit, or uncovered/unknown.
AC2: for fixes, show the relevant check detects the original failure; document limits.
AC3: deleted/weakened assertions, fixture-only success and tests mirroring the
     implementation are flagged for human review.
AC4: uncovered or unknown criteria block readiness unless the contract is revised and
     approved again; they cannot be silently waived.
AC5: the check runs independently of the writer; no custom reviewer infrastructure.
"""

import ast
import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from controller.contract import digest, freeze
from verify import Candidate, CheckResult, Observation, Source, Verdict, verify
from verify.assertions import (
    DEFAULT_ASSERTION_POLICY,
    AssertionLink,
    AssertionPolicy,
    Clearance,
    ControlChangeReport,
    Coverage,
    FailureProof,
    FailureProofLimit,
    FlagKind,
    ProofOutcome,
    TestSource,
    map_assertions,
    render,
)
from verify.criteria import TrustPolicy

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "schema" / "examples" / "filter-by-status.json"

HEAD = "a" * 40
NEW_HEAD = "b" * 40
MAIN = "c" * 40  # tip of main when the PR was checked
NEW_MAIN = "d" * 40
REPO = "rNavarrete/factory-pilot-demo"
CI_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/1"
CC_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/2"
PROOF_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/3"
REVIEWER = "rNavarrete"
WORKER = "rnavarrete-factory-bot"
MAPPER = "factory-verifier"  # an independent read-only verifier session
TEST_FILE = "tests/books.test.ts"

CONTRACT = freeze(json.loads(EXAMPLE.read_text()))
DIGEST = digest(CONTRACT)
BASE = CONTRACT["base_commit"]  # the candidate's merge base

# --- Pilot-style test file at the merge base and at the candidate ----------------

BASE_TEST = """\
import { describe, expect, it } from 'vitest';
import { addBook, type Book } from '../src/books';

const NOW = 1_767_225_600_000;

describe('addBook', () => {
  it('adds the book to the end without changing the input', () => {
    const books: Book[] = [];
    const next = addBook(books, { title: 'Dune', author: 'Frank Herbert' }, NOW);
    expect(next).toHaveLength(1);
    expect(next[0].title).toBe('Dune');
    expect(books).toEqual([]);
  });
});
"""

FILTER_TESTS = """
describe('filterByStatus', () => {
  const books: Book[] = [
    { id: '1', title: 'Dune', author: 'Frank Herbert', status: 'to-read', addedAt: 1 },
    { id: '2', title: 'Emma', author: 'Jane Austen', status: 'reading', addedAt: 2 },
    { id: '3', title: 'Ulysses', author: 'James Joyce', status: 'done', addedAt: 3 },
    { id: '4', title: 'Beloved', author: 'Toni Morrison', status: 'reading', addedAt: 4 },
  ];

  it('returns only the books with that status, in their original order', () => {
    const before = structuredClone(books);
    const result = filterByStatus(books, 'reading');
    expect(result.map((b) => b.id)).toEqual(['2', '4']);
    expect(books).toEqual(before);
  });

  it("returns every book for 'all'", () => {
    expect(filterByStatus(books, 'all')).toEqual(books);
  });
});
"""

HEAD_TEST = (
    BASE_TEST.replace(
        "import { addBook, type Book }", "import { addBook, filterByStatus, type Book }"
    )
    + FILTER_TESTS
)

ADD_TEST = "addBook > adds the book to the end without changing the input"
AC1_TEST = "filterByStatus > returns only the books with that status, in their original order"
AC1_ASSERT = "expect(result.map((b) => b.id)).toEqual(['2', '4'])"
AC2_TEST = "filterByStatus > returns every book for 'all'"
AC2_ASSERT = "expect(filterByStatus(books, 'all')).toEqual(books)"
FILE_FLAG = f"changed-test:{TEST_FILE}"

PROBE = "probe > checks it"
SHELF = """
describe('probe', () => {
  const shelf: Book[] = [
    { id: '1', title: 'Dune', author: 'Frank Herbert', status: 'to-read', addedAt: 1 },
    { id: '2', title: 'Emma', author: 'Jane Austen', status: 'reading', addedAt: 2 },
  ];
"""


def probe_file(tests, *, describe="describe", base=HEAD_TEST):
    """HEAD_TEST plus a 'probe' suite holding ``tests``."""
    return base + SHELF.replace("describe(", f"{describe}(", 1) + tests + "});\n"


def probe_test(body, *, call="it", name="checks it"):
    return f"  {call}('{name}', () => {{\n{body}\n  }});\n"


# --- Evidence builders ------------------------------------------------------------


def candidate(approved=None, **changes):
    d = approved or DIGEST
    c = Candidate(
        repository=REPO,
        head_commit=HEAD,
        base_commit=MAIN,
        merge_base=BASE,
        branch="claude/filter-by-status-a1",
        pr_title=f"[filter-by-status a1 {d.short}] Filter books by status",
        pr_body=f"Adds a status filter.\n\n{d.pr_body_line}\n",
        changed_paths=("src/books.ts", "src/main.ts", TEST_FILE),
    )
    return replace(c, **changes)


def ci(command, exit_code=0, **changes):
    r = CheckResult(
        command=command,
        commit=HEAD,
        base_commit=MAIN,
        exit_code=exit_code,
        source=Source.CI,
        url=CI_URL,
        output_excerpt=f"{command}: ok",
        repository=REPO,
        workflow_path=".github/workflows/ci.yml",
        app="github-actions",
    )
    return replace(r, **changes)


def all_green(contract=CONTRACT, **changes):
    return [ci(c, **changes) for c in contract["verification_commands"]]


def seen(**changes):
    o = Observation(
        criterion="ac3",
        commit=HEAD,
        base_commit=MAIN,
        observer=REVIEWER,
        verdict=Verdict.PASS,
        seen="Added three books, marked one reading and one done, picked 'reading': "
        "only that book listed; 'all' listed three.",
        limitations="Checked in Chrome only; did not try an empty list.",
    )
    return replace(o, **changes)


def eng156(cand=None, results=None, contract=CONTRACT, approved=DIGEST):
    """ENG-156's report, made the way the ENG-156 tests make it."""
    cand = cand or candidate()
    return verify(
        contract,
        approved,
        cand,
        all_green(contract) if results is None else results,
        [seen(commit=cand.head_commit, base_commit=cand.base_commit)],
    )


def src(text, commit=HEAD, path=TEST_FILE):
    return TestSource(path=path, commit=commit, text=text)


def link(criterion="ac1", test=None, assertion=None, **changes):
    test = test or (AC1_TEST if criterion == "ac1" else AC2_TEST)
    assertion = assertion or (AC1_ASSERT if criterion == "ac1" else AC2_ASSERT)
    lk = AssertionLink(
        criterion=criterion,
        path=TEST_FILE,
        test=test,
        assertion=assertion,
        contract_digest=str(DIGEST),
        commit=HEAD,
        base_commit=MAIN,
        mapper=MAPPER,
        why=f"The assertion checks {criterion}'s statement directly on filterByStatus.",
    )
    return replace(lk, **changes)


def proof(criterion="ac1", test=None, outcome=ProofOutcome.FAILED_ASSERTION, **changes):
    test = test or (AC1_TEST if criterion == "ac1" else AC2_TEST)
    p = FailureProof(
        criterion=criterion,
        path=TEST_FILE,
        test=test,
        contract_digest=str(DIGEST),
        tests_commit=HEAD,
        code_commit=BASE,
        outcome=outcome,
        by=MAPPER,
        url=PROOF_URL,
        output_excerpt="AssertionError: expected [ '1', '2', '3', '4' ] to deeply equal",
    )
    return replace(p, **changes)


def limit(criterion="ac1", **changes):
    lim = FailureProofLimit(
        criterion=criterion,
        contract_digest=str(DIGEST),
        commit=HEAD,
        base_commit=MAIN,
        by=MAPPER,
        reason="filterByStatus is new: the test cannot load against the base's code.",
    )
    return replace(lim, **changes)


def control(**changes):
    r = ControlChangeReport(
        head_commit=HEAD,
        base_commit=MAIN,
        flagged=False,
        reasons=(),
        url=CC_URL,
        repository=REPO,
        workflow_path=".github/workflows/ci.yml",
        app="github-actions",
    )
    return replace(r, **changes)


def clearance(flag=FILE_FLAG, **changes):
    c = Clearance(
        flag=flag,
        contract_digest=str(DIGEST),
        commit=HEAD,
        base_commit=MAIN,
        by=REVIEWER,
        note="Read the diff: it adds the filterByStatus import and tests; addBook's "
        "test is unchanged.",
    )
    return replace(c, **changes)


_DEFAULT = object()


def run(
    head_text=HEAD_TEST,
    *,
    base_text=BASE_TEST,
    links=None,
    sources=None,
    proofs=None,
    limits=(),
    control_change=_DEFAULT,
    clearances=None,
    cand=None,
    verification=None,
    contract=CONTRACT,
    approved=DIGEST,
    policy=DEFAULT_ASSERTION_POLICY,
):
    cand = cand or candidate()
    return map_assertions(
        contract,
        approved,
        cand,
        eng156(cand) if verification is None else verification,
        links=[link("ac1"), link("ac2")] if links is None else links,
        sources=[src(head_text), src(base_text, BASE)] if sources is None else sources,
        proofs=[proof("ac1"), proof("ac2")] if proofs is None else proofs,
        limits=limits,
        control_change=control() if control_change is _DEFAULT else control_change,
        clearances=[clearance()] if clearances is None else clearances,
        policy=policy,
    )


def probe_run(tests, assertion, *, name=PROBE, describe="describe", **kw):
    """Link ac1 to a test in the 'probe' suite instead of the real one."""
    kw.setdefault("links", [link("ac1", test=name, assertion=assertion), link("ac2")])
    kw.setdefault("proofs", [proof("ac1", test=name), proof("ac2")])
    return run(probe_file(tests, describe=describe), **kw)


def cov(report, cid="ac1"):
    return next(c for c in report.criteria if c.criterion == cid)


def statuses(report):
    return {c.criterion: c.status for c in report.criteria}


def open_keys(report):
    return {f.key for f in report.open_flags}


def open_kinds(report):
    return {f.kind for f in report.open_flags}


def ignored_with(report, prefix):
    return [i for i in report.ignored if i.startswith(prefix)]


# --- Happy path -------------------------------------------------------------------


class HappyPathTest(unittest.TestCase):
    def test_everything_shown_is_ready(self):
        report = run()
        self.assertEqual(report.blockers, ())
        self.assertTrue(report.ready)
        self.assertEqual(
            statuses(report),
            {"ac1": Coverage.COVERED, "ac2": Coverage.COVERED, "ac3": Coverage.COVERED},
        )
        self.assertEqual(report.open_flags, ())
        self.assertEqual(report.ignored, ())

    def test_eng156_report_used_as_input_is_itself_ready(self):
        self.assertTrue(eng156().ready, eng156().blockers)

    def test_adding_tests_to_an_existing_file_is_one_file_level_flag(self):
        # The import line changed and tests were appended; nothing old changed.
        report = run(clearances=[])
        self.assertEqual(open_keys(report), {FILE_FLAG})
        self.assertFalse(report.ready)


# --- AC1 --------------------------------------------------------------------------


class MapsCriterionToAssertionTest(unittest.TestCase):
    """AC1: criterion -> assertion/evidence -> exact tested commit, or uncovered/unknown."""

    def test_report_names_digest_commits_and_every_criterion(self):
        report = run()
        self.assertEqual(report.contract_digest, DIGEST.value)
        self.assertEqual(report.candidate_commit, HEAD)
        self.assertEqual(report.base_commit, MAIN)
        self.assertEqual(report.repository, REPO)
        self.assertEqual([c.criterion for c in report.criteria], ["ac1", "ac2", "ac3"])
        for c in report.criteria:
            self.assertEqual(c.commit, HEAD)

    def test_covered_criterion_names_path_test_line_assertion_and_commit(self):
        (a,) = cov(run()).assertions
        self.assertEqual(a.path, TEST_FILE)
        self.assertEqual(a.test, AC1_TEST)
        self.assertEqual(a.commit, HEAD)
        self.assertEqual(a.mapper, MAPPER)
        self.assertEqual(a.assertion, AC1_ASSERT)
        line = HEAD_TEST.splitlines().index(f"    {AC1_ASSERT};") + 1
        self.assertEqual(a.line, line)
        self.assertIn("filterByStatus", a.why)

    def test_observed_criterion_cites_the_observation_and_its_limits(self):
        ac3 = cov(run(), "ac3")
        self.assertEqual(ac3.status, Coverage.COVERED)
        self.assertEqual(ac3.evidence_type, "observable-behavior")
        self.assertTrue(ac3.observations)
        self.assertIn("only that book listed", ac3.observations[0].detail)
        self.assertTrue(any("Chrome only" in x for x in ac3.limits))

    def test_observed_criterion_without_observation_is_unknown(self):
        cand = candidate()
        v = verify(CONTRACT, DIGEST, cand, all_green(), [])
        report = run(verification=v)
        self.assertEqual(cov(report, "ac3").status, Coverage.UNKNOWN)
        self.assertFalse(report.ready)

    def test_assertion_limit_says_no_per_test_result(self):
        ac1 = cov(run())
        self.assertTrue(any("no per-test result" in x for x in ac1.limits))

    def test_no_link_is_uncovered(self):
        report = run(links=[link("ac2")])
        self.assertEqual(cov(report).status, Coverage.UNCOVERED)
        self.assertEqual(cov(report).assertions, ())
        self.assertIn("a passing suite does not show", cov(report).reasons[0])
        self.assertFalse(report.ready)

    def test_link_to_a_missing_test_is_unknown(self):
        report = run(links=[link("ac1", test="filterByStatus > does not exist"), link("ac2")])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("no test with that full name", cov(report).reasons[0])

    def test_test_name_must_include_its_describe(self):
        report = run(links=[link("ac1", test=AC1_TEST.split(" > ")[1]), link("ac2")])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_assertion_from_a_different_test_is_unknown(self):
        report = run(links=[link("ac1", assertion=AC2_ASSERT), link("ac2")])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("not in that test's own body", cov(report).reasons[0])

    def test_assertion_only_in_a_comment_is_unknown(self):
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    // expect(result).toHaveLength(1);\n"
            "    /* expect(result).toHaveLength(1); */\n"
            "    expect(result).toBeDefined();"
        )
        report = probe_run(probe_test(body), "expect(result).toHaveLength(1)")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_assertion_only_in_a_string_is_unknown(self):
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    const note = 'expect(result).toHaveLength(1)';\n"
            "    const tpl = `expect(result).toHaveLength(1)`;\n"
            "    expect(note + tpl).toContain('expect');"
        )
        report = probe_run(probe_test(body), "expect(result).toHaveLength(1)")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_expect_with_no_matcher_is_unknown(self):
        body = "    const result = filterByStatus(shelf, 'reading');\n    expect(result);"
        report = probe_run(probe_test(body), "expect(result)")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_expect_with_a_property_but_no_call_is_unknown(self):
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n    expect(result).toBeTruthy;"
        )
        report = probe_run(probe_test(body), "expect(result).toBeTruthy")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_plain_probe_is_covered(self):
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    expect(result).toHaveLength(1);"
        )
        report = probe_run(probe_test(body), "expect(result).toHaveLength(1)")
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertTrue(report.ready, report.blockers)

    def test_not_and_resolves_matchers_count(self):
        for assertion in (
            "expect(result).not.toContain(shelf[0])",
            "await expect(Promise.resolve(result)).resolves.toHaveLength(1)",
        ):
            with self.subTest(assertion=assertion):
                body = f"    const result = filterByStatus(shelf, 'reading');\n    {assertion};"
                report = probe_run(probe_test(body).replace("() =>", "async () =>"), assertion)
                self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_assertion_in_a_nested_test_is_unknown(self):
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    it('inner', () => {\n"
            "      expect(result).toHaveLength(1);\n"
            "    });"
        )
        report = probe_run(probe_test(body), "expect(result).toHaveLength(1)")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_quote_that_starts_before_a_nested_test_does_not_reach_into_it(self):
        # Review found: the nested-test check looks at where the quote starts, not where the
        # assertion it finds starts, so a quote with a prefix reaches into a nested test.
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    it('inner', () => {\n"
            "      expect(result).toHaveLength(1);\n"
            "    });"
        )
        quote = (
            "filterByStatus(shelf, 'reading'); it('inner', () => { expect(result).toHaveLength(1)"
        )
        report = probe_run(probe_test(body), quote)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_multiline_assertion_matches_when_only_whitespace_differs(self):
        body = (
            "    expect(\n"
            "      filterByStatus(shelf, 'reading'),\n"
            "    )\n"
            "      .toEqual([shelf[1]]);"
        )
        quote = "expect( filterByStatus(shelf, 'reading'), ) .toEqual([shelf[1]])"
        report = probe_run(probe_test(body), quote)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertEqual(cov(report).assertions[0].assertion, quote)

    def test_regex_and_template_literals_do_not_break_parsing(self):
        body = r"""    const pattern = /[{}'"`(]+/g;
    const label = `{${shelf.length}} '"(`;
    const half = shelf.length / 2;
    const cleaned = 'a{b}c'.replace(/\}/g, '').replace(pattern, '');
    const fake = 'it("x", () => {';
    // it('fake', () => { expect(true).toBe(true) })
    const result = filterByStatus(shelf, 'reading');
    expect(result.map((b) => `${b.title} {${b.id}}`)).toEqual(['Emma {2}']);"""
        quote = "expect(result.map((b) => `${b.title} {${b.id}}`)).toEqual(['Emma {2}'])"
        report = probe_run(probe_test(body), quote)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        # The other tests in the file still parse and still map.
        self.assertEqual(cov(report, "ac2").status, Coverage.COVERED)
        self.assertNotIn(FlagKind.FIXTURE_ONLY, open_kinds(report))

    def test_test_titles_in_template_literals_and_with_escapes(self):
        tests = (
            "  it(`checks it`, () => {\n"
            "    expect(filterByStatus(shelf, 'done')).toEqual([]);\n"
            "  });\n"
            "  it('it\\'s escaped', () => {\n"
            "    expect(filterByStatus(shelf, 'reading')).toHaveLength(1);\n"
            "  });\n"
        )
        report = probe_run(tests, "expect(filterByStatus(shelf, 'done')).toEqual([])")
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        q = "expect(filterByStatus(shelf, 'reading')).toHaveLength(1)"
        name = "probe > it's escaped"
        report = probe_run(tests, q, name=name)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_unparseable_file_makes_the_link_unknown_never_covered(self):
        for broken in ("\nconst s = 'unterminated;\n", "\nconst r = /never closed\n", "\n/* x"):
            with self.subTest(broken=broken):
                report = run(HEAD_TEST + broken)
                self.assertEqual(cov(report).status, Coverage.UNKNOWN)
                self.assertEqual(cov(report, "ac2").status, Coverage.UNKNOWN)
                self.assertIn("could not be read reliably", cov(report).reasons[0])
                self.assertFalse(report.ready)

    def test_unbalanced_brackets_make_the_link_unknown(self):
        report = run(HEAD_TEST + "\ndescribe('x', () => {\n")
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_duplicate_test_names_are_unknown(self):
        report = run(HEAD_TEST + FILTER_TESTS)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("more than one test", cov(report).reasons[0])

    def test_head_source_not_supplied_is_unknown(self):
        report = run(sources=[src(BASE_TEST, BASE)])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("not supplied", cov(report).reasons[0])

    def test_file_missing_at_candidate_is_unknown(self):
        report = run(sources=[src(None), src(BASE_TEST, BASE)])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("does not exist", cov(report).reasons[0])

    def test_conflicting_duplicate_sources_are_not_used(self):
        report = run(
            sources=[src(HEAD_TEST), src(HEAD_TEST + "\n// x\n"), src(BASE_TEST, BASE)],
            clearances=[],
        )
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertTrue(ignored_with(report, "conflicting sources"))
        flag = next(f for f in report.open_flags if f.key == FILE_FLAG)
        self.assertIn("was not supplied", flag.detail)

    def test_identical_duplicate_sources_are_fine(self):
        report = run(sources=[src(HEAD_TEST), src(HEAD_TEST), src(BASE_TEST, BASE)])
        self.assertTrue(report.ready, report.blockers)

    def test_source_for_another_commit_is_ignored_as_stale(self):
        report = run(sources=[src(HEAD_TEST, NEW_HEAD), src(BASE_TEST, BASE)])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertTrue(ignored_with(report, "stale: source"))
        # Base source at main (not the merge base) is stale too.
        report = run(sources=[src(HEAD_TEST), src(BASE_TEST, MAIN)], clearances=[])
        self.assertTrue(ignored_with(report, "stale: source"))
        flag = next(f for f in report.open_flags if f.key == FILE_FLAG)
        self.assertIn("its merge base", flag.detail)

    def test_stale_or_incomplete_links_are_ignored(self):
        for changes, prefix in (
            ({"commit": NEW_HEAD}, "stale:"),
            ({"base_commit": NEW_MAIN}, "stale:"),
            ({"why": "  "}, "incomplete:"),
            ({"mapper": ""}, "untrusted:"),
        ):
            with self.subTest(changes=changes):
                report = run(links=[link("ac1", **changes), link("ac2")])
                self.assertEqual(cov(report).status, Coverage.UNCOVERED)
                self.assertTrue(ignored_with(report, prefix))
                self.assertFalse(report.ready)

    def test_link_for_unknown_criterion_is_ignored(self):
        report = run(links=[link("ac1"), link("ac2"), link("ac9", test=AC1_TEST)])
        self.assertTrue(report.ready, report.blockers)
        self.assertTrue(any("unknown criterion 'ac9'" in i for i in report.ignored))

    def test_link_to_a_path_npm_test_does_not_run_is_unknown(self):
        for path in ("tests/books.spec.ts", "src/books.test.ts", "tests/books.test.js"):
            with self.subTest(path=path):
                report = run(
                    links=[link("ac1", path=path), link("ac2")],
                    sources=[src(HEAD_TEST), src(BASE_TEST, BASE), src(HEAD_TEST, path=path)],
                )
                self.assertEqual(cov(report).status, Coverage.UNKNOWN)
                self.assertIn("does not run", cov(report).reasons[0])

    def test_nested_test_directory_is_run_by_npm_test(self):
        path = "tests/unit/filter.test.ts"
        report = run(
            links=[link("ac1", path=path), link("ac2")],
            sources=[src(HEAD_TEST), src(BASE_TEST, BASE), src(HEAD_TEST, path=path)],
            proofs=[proof("ac1", path=path), proof("ac2")],
        )
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_command_with_no_known_suite_is_unknown(self):
        report = run(policy=AssertionPolicy(suites=()))
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("is not known", cov(report).reasons[0])

    def test_criterion_command_failed_is_failed_even_with_a_good_link(self):
        cand = candidate()
        results = [ci("npm run typecheck"), ci("npm test", exit_code=1), ci("npm run build")]
        report = run(verification=eng156(cand, results))
        self.assertEqual(cov(report).status, Coverage.FAILED)
        self.assertEqual(cov(report, "ac2").status, Coverage.FAILED)
        self.assertTrue(cov(report).assertions)  # still says which assertion
        self.assertFalse(report.ready)

    def test_criterion_command_without_trusted_result_is_unknown_even_with_a_good_link(self):
        cand = candidate()
        results = [ci("npm run typecheck"), ci("npm run build")]
        report = run(verification=eng156(cand, results))
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertFalse(report.ready)

    def test_criterion_absent_from_the_eng156_report_is_unknown(self):
        v = eng156()
        v = replace(v, criteria=tuple(c for c in v.criteria if c.criterion != "ac1"))
        report = run(verification=v)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)

    def test_eng156_report_for_another_commit_fails_the_gate(self):
        v = eng156(candidate(head_commit=NEW_HEAD))
        report = run(verification=v)
        gate = next(g for g in report.gates if g.name == "criterion check")
        self.assertFalse(gate.ok)
        self.assertIn("candidate moved", gate.detail)
        self.assertEqual(report.criteria, ())
        self.assertFalse(report.ready)

    def test_eng156_report_for_another_base_or_repo_fails_the_gate(self):
        for cand in (candidate(base_commit=NEW_MAIN), candidate(repository="someone/fork")):
            with self.subTest(cand=cand):
                report = run(verification=eng156(cand))
                self.assertFalse(next(g for g in report.gates if g.name == "criterion check").ok)
                self.assertFalse(report.ready)

    def test_eng156_report_for_another_digest_fails_the_gate(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["goal"] += " Quickly."
        other = freeze(raw)
        v = eng156(candidate(digest(other)), contract=other, approved=digest(other))
        report = run(verification=v)
        gate = next(g for g in report.gates if g.name == "criterion check")
        self.assertFalse(gate.ok)
        self.assertIn("is for contract", gate.detail)
        self.assertFalse(report.ready)

    def test_invalid_contract_fails_the_contract_gate(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["permitted_paths"].append("package.json")
        report = run(contract=freeze(raw))  # still the old approved digest
        self.assertFalse(report.gates[0].ok)
        self.assertEqual(report.criteria, ())
        self.assertFalse(report.ready)
        self.assertIn("no acceptance criteria were mapped", report.blockers)
        report = run(approved="not-a-digest")
        self.assertFalse(report.gates[0].ok)
        self.assertFalse(report.ready)

    def test_eng156_blockers_hold_readiness(self):
        cand = candidate(pr_body="no digest line")
        report = run(cand=cand)
        self.assertEqual(set(statuses(report).values()), {Coverage.COVERED})
        self.assertFalse(report.ready)
        self.assertTrue(any(b.startswith("criterion check: pr body") for b in report.blockers))


class TestThatMayNotRunTest(unittest.TestCase):
    """AC1/AC3: a test that is skipped or may not reach its assertion is not coverage."""

    BODY = (
        "    const result = filterByStatus(shelf, 'reading');\n    expect(result).toHaveLength(1);"
    )
    QUOTE = "expect(result).toHaveLength(1)"

    def assert_unknown(self, tests, *, name=PROBE, describe="describe", why=None):
        report = probe_run(tests, self.QUOTE, name=name, describe=describe)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN, report.criteria)
        self.assertFalse(report.ready)
        if why:
            self.assertIn(why, " ".join(cov(report).reasons))
        return report

    def test_skip_todo_fails_and_xit(self):
        for call in ("it.skip", "test.skip", "it.todo", "it.fails", "xit", "it.concurrent.skip"):
            with self.subTest(call=call):
                self.assert_unknown(probe_test(self.BODY, call=call), why="skipped")

    def test_todo_without_body(self):
        self.assert_unknown("  it.todo('checks it');\n")

    def test_describe_skip_around_the_test(self):
        for describe in ("describe.skip", "xdescribe", "describe.todo"):
            with self.subTest(describe=describe):
                self.assert_unknown(probe_test(self.BODY), describe=describe, why="skipped")

    def test_skip_if_and_run_if(self):
        for call in ("it.skipIf(process.env.CI)", "it.runIf(Math.random() > 0.5)"):
            with self.subTest(call=call):
                self.assert_unknown(probe_test(self.BODY, call=call), why="condition")
        self.assert_unknown(
            probe_test(self.BODY), describe="describe.runIf(false)", why="condition"
        )

    def test_each_is_not_supported(self):
        tests = (
            "  it.each([['reading'], ['done']])('checks %s', (status) => {\n"
            "    const result = filterByStatus(shelf, status);\n"
            "    expect(result).toHaveLength(1);\n"
            "  });\n"
        )
        self.assert_unknown(tests, name="probe > checks %s", why="parameterized")
        tagged = (
            "  it.each`\n    status\n    ${'reading'}\n  `('checks it', ({ status }) => {\n"
            "    const result = filterByStatus(shelf, status);\n"
            "    expect(result).toHaveLength(1);\n"
            "  });\n"
        )
        self.assert_unknown(tagged, why="parameterized")

    def test_only_elsewhere_in_the_file(self):
        other = probe_test("    expect(filterByStatus(shelf, 'all')).toHaveLength(2);", name="x")
        for call in ("it.only", "test.only"):
            with self.subTest(call=call):
                tests = probe_test(self.BODY) + other.replace("it(", f"{call}(", 1)
                self.assert_unknown(tests, why=".only")

    def test_only_on_the_linked_test_leaves_it_covered_but_others_unknown(self):
        report = probe_run(probe_test(self.BODY, call="it.only"), self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertEqual(cov(report, "ac2").status, Coverage.UNKNOWN)
        self.assertIn(f"weakened-test:{TEST_FILE} > {ADD_TEST}", open_keys(report))

    def test_only_in_a_comment_or_string_does_not_count(self):
        tests = probe_test(
            self.BODY + "\n    // it.only('x', () => {});\n    const s = 'it.only(';"
        )
        report = probe_run(tests, self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_early_return_before_the_assertion_is_unknown(self):
        for early in ("    return;", "    if (shelf.length > 0) return;"):
            with self.subTest(early=early):
                body = early + "\n" + self.BODY
                self.assert_unknown(probe_test(body), why="returns or throws before the assertion")

    def test_return_after_the_assertion_is_fine(self):
        report = probe_run(probe_test(self.BODY + "\n    return;"), self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_return_inside_a_nested_callback_is_fine(self):
        body = (
            "    const ids = shelf.map((b) => {\n      return b.id;\n    });\n"
            "    const names = shelf.map(function (b) { return b.title; });\n" + self.BODY
        )
        report = probe_run(probe_test(body), self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertNotIn(FlagKind.MAY_NOT_RUN, open_kinds(report))
        self.assertTrue(report.ready, report.blockers)

    def test_return_inside_a_typed_helper_function_is_fine(self):
        # Review found: _opener_kind returns "block" for `function f(): T {` (both branches of its
        # last line say "block"), so a return inside a typed helper reads as an early return.
        body = (
            "    function ids(list: Book[]): string[] {\n      return list.map((b) => b.id);\n"
            "    }\n" + self.BODY
        )
        report = probe_run(probe_test(body), self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertNotIn(FlagKind.MAY_NOT_RUN, open_kinds(report))

    def test_return_inside_an_if_block_flags_may_not_run(self):
        body = "    if (shelf.length === 0) {\n      return;\n    }\n" + self.BODY
        report = probe_run(probe_test(body), self.QUOTE)
        self.assertEqual(cov(report).status, Coverage.COVERED)
        self.assertIn(f"may-not-run:{TEST_FILE} > {PROBE}", open_keys(report))
        self.assertFalse(report.ready)

    def test_return_expect_is_not_an_early_return(self):
        # Review found: `return expect(...)...` counts its own `return` as an early return, so the
        # common promise-returning idiom is reported unknown instead of covered.
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    return expect(Promise.resolve(result)).resolves.toHaveLength(1);"
        )
        quote = "expect(Promise.resolve(result)).resolves.toHaveLength(1)"
        report = probe_run(probe_test(body), quote)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)

    def test_assertion_in_a_foreach_callback_flags_may_not_run(self):
        for body, quote in (
            (
                "    filterByStatus(shelf, 'reading').forEach((b) => {\n"
                "      expect(b.status).toBe('reading');\n    });",
                "expect(b.status).toBe('reading')",
            ),
            (
                "    filterByStatus(shelf, 'reading')"
                ".forEach((b) => expect(b.status).toBe('reading'));",
                "expect(b.status).toBe('reading')",
            ),
        ):
            with self.subTest(body=body):
                report = probe_run(probe_test(body), quote)
                self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
                self.assertIn(f"may-not-run:{TEST_FILE} > {PROBE}", open_keys(report))
                self.assertFalse(report.ready)

    def test_assertion_in_an_if_block_or_loop_block_flags_may_not_run(self):
        for wrap in ("if (shelf.length > 1) {", "for (const b of shelf) {", "try {"):
            with self.subTest(wrap=wrap):
                body = (
                    "    const result = filterByStatus(shelf, 'reading');\n"
                    f"    {wrap}\n      expect(result).toHaveLength(1);\n    }}"
                    + (" catch (e) {}" if wrap == "try {" else "")
                )
                report = probe_run(probe_test(body), self.QUOTE)
                self.assertEqual(cov(report).status, Coverage.COVERED)
                self.assertIn(f"may-not-run:{TEST_FILE} > {PROBE}", open_keys(report))
                self.assertFalse(report.ready)

    def test_assertion_in_a_braceless_loop_flags_may_not_run(self):
        # Review found: _context only sees `{` blocks, so `for (...) expect(...)` (vacuous when the
        # list is empty) and `if (...) expect(...)` are treated as always running.
        body = (
            "    for (const b of filterByStatus(shelf, 'reading')) "
            "expect(b.status).toBe('reading');"
        )
        report = probe_run(probe_test(body), "expect(b.status).toBe('reading')")
        self.assertFalse(report.ready)
        self.assertIn(f"may-not-run:{TEST_FILE} > {PROBE}", open_keys(report))

    def test_assertion_in_a_braceless_if_flags_may_not_run(self):
        # Review found: same as above for a brace-less `if`.
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            "    if (result.length > 5) expect(result).toHaveLength(1);"
        )
        report = probe_run(probe_test(body), self.QUOTE)
        self.assertFalse(report.ready)
        self.assertIn(f"may-not-run:{TEST_FILE} > {PROBE}", open_keys(report))


# --- AC2 --------------------------------------------------------------------------


class FailureProofTest(unittest.TestCase):
    """AC2: the check is shown to detect the original failure, or its limit is documented."""

    def assert_flag(self, report, kind, cid="ac1"):
        self.assertIn(f"{kind.value}:{cid}", open_keys(report))
        self.assertFalse(report.ready)

    def test_failed_assertion_is_proof_with_no_flag_or_limit(self):
        report = run()
        self.assertTrue(report.ready, report.blockers)
        self.assertFalse([x for x in cov(report).limits if "without the change" in x])

    def test_failed_error_is_a_noted_limit_not_a_flag(self):
        report = run(proofs=[proof("ac1", outcome=ProofOutcome.FAILED_ERROR), proof("ac2")])
        self.assertTrue(report.ready, report.blockers)
        self.assertTrue(any("before reaching its assertion" in x for x in cov(report).limits))
        self.assertTrue(any(PROOF_URL in x for x in cov(report).limits))

    def test_passed_without_the_change_is_flagged(self):
        report = run(proofs=[proof("ac1", outcome=ProofOutcome.PASSED), proof("ac2")])
        self.assert_flag(report, FlagKind.PASSES_WITHOUT_CHANGE)
        flag = next(f for f in report.flags if f.kind is FlagKind.PASSES_WITHOUT_CHANGE)
        self.assertEqual(flag.criterion, "ac1")
        self.assertIn(PROOF_URL, flag.detail)

    def test_no_proof_is_flagged(self):
        report = run(proofs=[proof("ac2")])
        self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)
        self.assertEqual(cov(report).status, Coverage.COVERED)

    def test_conflicting_proofs_for_the_same_test_are_flagged(self):
        # Review found: a PASSED proof is dropped whenever any FAILED_ASSERTION proof exists, even
        # for the same test on the same commits, so a flaky or contested proof clears itself.
        report = run(proofs=[proof("ac1"), proof("ac1", outcome=ProofOutcome.PASSED), proof("ac2")])
        self.assert_flag(report, FlagKind.PASSES_WITHOUT_CHANGE)

    def test_independent_limit_is_noted_not_flagged(self):
        report = run(proofs=[proof("ac2")], limits=[limit("ac1")])
        self.assertTrue(report.ready, report.blockers)
        self.assertTrue(any("cannot load against the base" in x for x in cov(report).limits))
        self.assertTrue(any(MAPPER in x for x in cov(report).limits))

    def test_worker_limit_is_ignored_and_still_flagged(self):
        for by in (WORKER, WORKER.upper(), "RNavarrete-Factory-Bot"):
            with self.subTest(by=by):
                report = run(proofs=[proof("ac2")], limits=[limit("ac1", by=by)])
                self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)
                self.assertTrue(ignored_with(report, "untrusted: failure-proof limit"))

    def test_stale_or_empty_limits_are_ignored(self):
        for changes, prefix in (
            ({"commit": NEW_HEAD}, "stale:"),
            ({"base_commit": NEW_MAIN}, "stale:"),
            ({"reason": " "}, "incomplete:"),
            ({"by": ""}, "untrusted:"),
        ):
            with self.subTest(changes=changes):
                report = run(proofs=[proof("ac2")], limits=[limit("ac1", **changes)])
                self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)
                self.assertTrue(ignored_with(report, prefix))

    def test_limit_for_another_criterion_does_not_help(self):
        report = run(proofs=[proof("ac2")], limits=[limit("ac2")])
        self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)

    def test_unusable_proofs_are_ignored(self):
        for changes, prefix in (
            ({"by": WORKER}, "untrusted:"),
            ({"by": "RNAVARRETE-FACTORY-BOT"}, "untrusted:"),
            ({"by": ""}, "untrusted:"),
            ({"tests_commit": NEW_HEAD}, "stale:"),
            ({"code_commit": MAIN}, "stale:"),  # the base branch tip, not the merge base
            ({"code_commit": HEAD}, "stale:"),  # ran against the new code
            ({"url": ""}, "untrusted:"),
            ({"test": ADD_TEST}, "unused:"),  # no usable link names this test
        ):
            with self.subTest(changes=changes):
                report = run(proofs=[proof("ac1", **changes), proof("ac2")])
                self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)
                self.assertTrue(ignored_with(report, prefix), report.ignored)

    def test_proof_for_another_criterion_does_not_help(self):
        report = run(proofs=[proof("ac2"), proof("ac2", test=AC1_TEST)])
        self.assert_flag(report, FlagKind.NO_FAILURE_PROOF)

    def test_passes_without_change_flag_can_be_cleared_by_the_reviewer(self):
        report = run(
            proofs=[proof("ac1", outcome=ProofOutcome.PASSED), proof("ac2")],
            clearances=[
                clearance(),
                clearance("passes-without-change:ac1", note="Regression already covered."),
            ],
        )
        self.assertTrue(report.ready, report.blockers)


# --- AC3 --------------------------------------------------------------------------


class FlagsForHumanReviewTest(unittest.TestCase):
    """AC3: deleted/weakened assertions, fixture-only success and mirroring are flagged."""

    def changed(self, old, new, *, path=TEST_FILE, base_text=BASE_TEST):
        self.assertNotEqual(old, new)
        head = HEAD_TEST.replace(old, new)
        self.assertNotEqual(head, HEAD_TEST, "replacement did not apply")
        return run(head, base_text=base_text)

    def test_deleted_test_file(self):
        path = "tests/storage.test.ts"
        cand = candidate(changed_paths=("src/books.ts", TEST_FILE, path))
        report = run(
            cand=cand,
            sources=[
                src(HEAD_TEST),
                src(BASE_TEST, BASE),
                src(None, path=path),
                src(BASE_TEST, BASE, path=path),
            ],
        )
        self.assertIn(f"deleted-test:{path}", open_keys(report))
        flag = next(f for f in report.flags if f.key == f"deleted-test:{path}")
        self.assertIn("1 tests", flag.detail)
        self.assertFalse(report.ready)

    def test_test_removed_from_a_file(self):
        head = HEAD_TEST.replace(BASE_TEST.split("describe(", 1)[1], "")
        head = head.replace("\ndescribe(", "\n", 1)  # drop the whole addBook suite
        report = run(head)
        key = f"deleted-test:{TEST_FILE} > {ADD_TEST}"
        self.assertIn(key, open_keys(report))
        flag = next(f for f in report.flags if f.key == key)
        self.assertIn("3 assertion(s)", flag.detail)
        self.assertFalse(report.ready)

    def test_renamed_test_is_a_deleted_test(self):
        report = self.changed("it('adds the book to the end", "it('appends the book to the end")
        self.assertIn(f"deleted-test:{TEST_FILE} > {ADD_TEST}", open_keys(report))

    def test_fewer_assertions(self):
        report = self.changed("    expect(next[0].title).toBe('Dune');\n", "")
        key = f"weakened-test:{TEST_FILE} > {ADD_TEST}"
        self.assertIn(key, open_keys(report))
        self.assertIn(
            "3 assertion(s) down to 2", next(f for f in report.flags if f.key == key).detail
        )
        self.assertFalse(report.ready)

    def test_newly_skipped_or_conditional(self):
        for old, new, why in (
            ("it('adds the book", "it.skip('adds the book", "now skipped"),
            ("it('adds the book", "it.todo('adds the book", "now skipped"),
            ("it('adds the book", "xit('adds the book", "now skipped"),
            ("describe('addBook'", "describe.skip('addBook'", "now skipped"),
            ("it('adds the book", "it.skipIf(true)('adds the book", "conditionally"),
            ("describe('addBook'", "describe.only('addBook'", None),
        ):
            with self.subTest(new=new):
                report = self.changed(old, new)
                key = f"weakened-test:{TEST_FILE} > {ADD_TEST}"
                if why is None:  # .only on the old test itself: other tests may not run
                    self.assertFalse(report.ready)
                    continue
                self.assertIn(key, open_keys(report))
                self.assertIn(why, next(f for f in report.flags if f.key == key).detail)

    def test_only_added_elsewhere_weakens_the_existing_test(self):
        report = self.changed('  it("returns every book', '  it.only("returns every book')
        key = f"weakened-test:{TEST_FILE} > {ADD_TEST}"
        self.assertIn(key, open_keys(report))
        self.assertIn(".only", next(f for f in report.flags if f.key == key).detail)

    def test_changed_test_body(self):
        report = self.changed("toBe('Dune')", "toBe(next[0].title)")
        key = f"changed-test:{TEST_FILE} > {ADD_TEST}"
        self.assertIn(key, open_keys(report))
        self.assertFalse(report.ready)

    def test_whitespace_only_change_is_still_a_file_level_change(self):
        line = "    expect(next).toHaveLength(1);"
        report = run(HEAD_TEST.replace(line, line + "   "), clearances=[])
        self.assertIn(FILE_FLAG, open_keys(report))

    def test_import_only_change_is_flagged(self):
        head = BASE_TEST.replace("import { addBook,", "import { addBook as add, addBook,")
        report = run(head, links=[], proofs=[], clearances=[])
        self.assertIn(FILE_FLAG, open_keys(report))
        self.assertIn(
            "outside its tests", next(f for f in report.flags if f.key == FILE_FLAG).detail
        )

    def test_change_outside_tests_is_flagged_even_when_a_test_also_changed(self):
        # Review found: the file-level "outside its tests" flag is only raised when no test-level
        # flag was; clearing the test flag then hides a weakened shared fixture or import.
        head = HEAD_TEST.replace("const NOW = 1_767_225_600_000;", "const NOW = 0;").replace(
            "toBe('Dune')", "toBe(next[0].title)"
        )
        report = run(head)
        self.assertIn(f"changed-test:{TEST_FILE} > {ADD_TEST}", open_keys(report))
        self.assertIn(FILE_FLAG, {f.key for f in report.flags})
        self.assertIn(
            "-const NOW = 1_767_225_600_000;",
            next(f for f in report.flags if f.key == FILE_FLAG).detail,
        )
        cleared = run(head, clearances=[clearance(f"changed-test:{TEST_FILE} > {ADD_TEST}")])
        self.assertIn(FILE_FLAG, open_keys(cleared))
        self.assertFalse(cleared.ready)

    def test_missing_base_or_head_source_for_a_changed_test_file(self):
        for sources, missing in (
            ([src(HEAD_TEST)], "its merge base"),
            ([src(BASE_TEST, BASE)], "the candidate"),
        ):
            with self.subTest(missing=missing):
                report = run(sources=sources, clearances=[])
                flag = next(f for f in report.open_flags if f.key == FILE_FLAG)
                self.assertIn(missing, flag.detail)
                self.assertFalse(report.ready)

    def test_brand_new_test_file_is_not_flagged(self):
        path = "tests/filter.test.ts"
        new = probe_file("", base=HEAD_TEST.replace(BASE_TEST.split("\ndescribe(")[1], "x"))
        cand = candidate(changed_paths=("src/books.ts", path))
        report = run(
            cand=cand,
            sources=[
                src(HEAD_TEST),
                src(BASE_TEST, BASE),
                src(new, path=path),
                src(None, BASE, path=path),
            ],
            clearances=[],
        )
        self.assertFalse([k for k in open_keys(report) if path in k], open_keys(report))

    def test_unchanged_test_file_is_not_flagged(self):
        cand = candidate(changed_paths=("src/books.ts", "src/main.ts"))
        report = run(
            BASE_TEST + FILTER_TESTS, cand=cand, clearances=[], base_text=BASE_TEST + FILTER_TESTS
        )
        self.assertNotIn(FlagKind.CHANGED_TEST, open_kinds(report))

    def test_unparseable_changed_test_file_is_flagged(self):
        for head, base in (
            (HEAD_TEST + "\nconst s = 'oops;\n", BASE_TEST),
            (HEAD_TEST, BASE_TEST + "\nconst s = 'oops;\n"),
        ):
            with self.subTest(head=head[-20:], base=base[-20:]):
                report = run(head, base_text=base, clearances=[])
                flag = next(f for f in report.open_flags if f.key == FILE_FLAG)
                self.assertIn("could not be read reliably", flag.detail)

    def test_non_test_helper_under_tests_is_checked_too(self):
        path = "tests/helpers.ts"
        cand = candidate(changed_paths=("src/books.ts", TEST_FILE, path))
        report = run(
            cand=cand,
            sources=[
                src(HEAD_TEST),
                src(BASE_TEST, BASE),
                src("export const n = 2;\n", path=path),
                src("export const n = 1;\n", BASE, path=path),
            ],
        )
        self.assertIn(f"changed-test:{path}", open_keys(report))

    # Fixture-only and mirrored expectations.

    def fixture_flagged(self, body, quote):
        report = probe_run(probe_test(body), quote)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        self.assertIn(f"fixture-only:{TEST_FILE} > {PROBE}", open_keys(report))
        self.assertFalse(report.ready)
        return report

    def test_literal_assertions_are_fixture_only(self):
        for assertion in (
            "expect(true).toBe(true)",
            "expect(1).toBe(1)",
            "expect('reading').toEqual('reading')",
            "expect([1, 2]).toHaveLength(2)",
            "expect(null).toBeNull()",
            "assert(true)",
        ):
            with self.subTest(assertion=assertion):
                body = f"    filterByStatus(shelf, 'reading');\n    {assertion};"
                self.fixture_flagged(body, assertion)

    def test_object_literal_assertion_is_fixture_only(self):
        # Review found: object keys count as names in _is_literal, so
        # expect({ done: true }).toEqual({ done: true }) is not seen as a literal.
        body = (
            "    filterByStatus(shelf, 'reading');\n"
            "    expect({ done: true }).toEqual({ done: true });"
        )
        self.fixture_flagged(body, "expect({ done: true }).toEqual({ done: true })")

    def test_test_that_never_calls_product_code_is_fixture_only(self):
        body = (
            "    const result = shelf.filter((b) => b.status === 'reading');\n"
            "    expect(result).toHaveLength(1);"
        )
        report = self.fixture_flagged(body, "expect(result).toHaveLength(1)")
        flag = next(f for f in report.open_flags if f.kind is FlagKind.FIXTURE_ONLY)
        self.assertIn("not traced to code imported from the product", flag.detail)
        self.assertEqual(flag.criterion, "ac1")

    def test_product_name_only_in_a_comment_or_property_does_not_count(self):
        body = (
            "    // filterByStatus(shelf, 'reading')\n"
            "    const result = shelf.filter((b) => b.status === 'reading');\n"
            "    const label = 'filterByStatus';\n"
            "    const fake = shelf.filterByStatus;\n"
            "    expect(result).toHaveLength(1);"
        )
        self.fixture_flagged(body, "expect(result).toHaveLength(1)")

    def test_mocked_product_module_is_fixture_only(self):
        for mock in (
            "vi.mock('../src/books');\n",
            'vi.mock("../src/books", () => ({ filterByStatus: () => [] }));\n',
            "vi.doMock(`../src/books`);\n",
        ):
            with self.subTest(mock=mock):
                text = probe_file(probe_test(TestThatMayNotRunTest.BODY)).replace(
                    "\nconst NOW", "\n" + mock + "const NOW", 1
                )
                report = run(
                    text,
                    links=[
                        link("ac1", test=PROBE, assertion=TestThatMayNotRunTest.QUOTE),
                        link("ac2"),
                    ],
                    proofs=[proof("ac1", test=PROBE), proof("ac2")],
                )
                flag = next(
                    f for f in report.open_flags if f.key == f"fixture-only:{TEST_FILE} > {PROBE}"
                )
                self.assertIn("mocks product code", flag.detail)
                self.assertFalse(report.ready)

    def test_mocking_a_test_helper_or_a_package_is_not_fixture_only(self):
        for mock in (
            "vi.mock('./helpers');\n",
            "vi.mock('nanoid');\n",
            "// vi.mock('../src/books');\n",
        ):
            with self.subTest(mock=mock):
                text = probe_file(probe_test(TestThatMayNotRunTest.BODY)).replace(
                    "\nconst NOW", "\n" + mock + "const NOW", 1
                )
                report = run(
                    text,
                    links=[
                        link("ac1", test=PROBE, assertion=TestThatMayNotRunTest.QUOTE),
                        link("ac2"),
                    ],
                    proofs=[proof("ac1", test=PROBE), proof("ac2")],
                )
                self.assertNotIn(FlagKind.FIXTURE_ONLY, open_kinds(report))
                self.assertTrue(report.ready, report.blockers)

    def test_type_only_import_is_not_product_code(self):
        text = probe_file(
            probe_test(
                "    const result = shelf.filter((b) => b.status === 'reading');\n"
                "    expect(result).toHaveLength(1);"
            )
        )
        report = run(
            text,
            links=[
                link("ac1", test=PROBE, assertion="expect(result).toHaveLength(1)"),
                link("ac2"),
            ],
            proofs=[proof("ac1", test=PROBE), proof("ac2")],
        )
        # `Book` is imported with `type`, so mentioning it does not count as calling product code.
        self.assertIn(f"fixture-only:{TEST_FILE} > {PROBE}", open_keys(report))

    def test_expected_value_computed_by_the_same_code_mirrors_it(self):
        assertion = "expect(filterByStatus(shelf, 'all')).toEqual(filterByStatus(shelf, 'all'))"
        report = probe_run(probe_test(f"    {assertion};"), assertion)
        key = f"mirrors-code:{TEST_FILE} > {PROBE}"
        self.assertIn(key, open_keys(report))
        self.assertIn("filterByStatus", next(f for f in report.flags if f.key == key).detail)
        self.assertFalse(report.ready)

    def test_independent_expected_value_does_not_mirror(self):
        report = run()
        self.assertNotIn(FlagKind.MIRRORS_CODE, open_kinds(report))
        self.assertNotIn(FlagKind.FIXTURE_ONLY, open_kinds(report))

    # Control changes.

    def test_changed_control_path_is_flagged(self):
        for path in (
            "package.json",
            ".github/workflows/ci.yml",
            "vite.config.ts",
            "tests/.eslintrc",
        ):
            with self.subTest(path=path):
                report = run(cand=candidate(changed_paths=("src/books.ts", TEST_FILE, path)))
                self.assertIn(f"control-change:{path}", open_keys(report))
                self.assertFalse(report.ready)

    def test_flagged_trusted_report_holds_readiness(self):
        report = run(
            control_change=control(flagged=True, reasons=("package.json scripts changed",))
        )
        flag = next(f for f in report.open_flags if f.key == "control-change:ci-report")
        self.assertIn("package.json scripts changed", flag.detail)
        self.assertIn(CC_URL, flag.detail)
        self.assertFalse(report.ready)

    def test_missing_report_holds_readiness(self):
        report = run(control_change=None)
        self.assertIn("control-change:ci-report", open_keys(report))
        self.assertFalse(report.ready)

    def test_untrusted_reports_are_not_trusted(self):
        for changes in (
            {"head_commit": NEW_HEAD},
            {"base_commit": NEW_MAIN},
            {"base_commit": BASE},  # the merge base, not the base branch tip
            {"workflow_path": ".github/workflows/control.yml"},
            {"app": "some-bot"},
            {"repository": "someone/fork"},
            {"url": ""},
        ):
            with self.subTest(changes=changes):
                report = run(control_change=control(**changes))
                self.assertIn("control-change:ci-report", open_keys(report))
                self.assertTrue(ignored_with(report, "untrusted: control-change report"))
                self.assertFalse(report.ready)


# --- AC4 --------------------------------------------------------------------------


class GapsCannotBeWaivedTest(unittest.TestCase):
    """AC4: uncovered/unknown block readiness unless the contract is revised and reapproved."""

    def test_uncovered_and_unknown_block_with_the_reason(self):
        report = run(links=[link("ac1", test="nope")])
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertEqual(cov(report, "ac2").status, Coverage.UNCOVERED)
        self.assertFalse(report.ready)
        for cid in ("ac1", "ac2"):
            b = next(b for b in report.blockers if b.startswith(cid))
            self.assertIn("only a revised contract, approved again", b)

    def test_clearances_naming_a_criterion_or_gap_are_refused(self):
        for name in ("ac2", "uncovered:ac2", "unknown:ac2", "failed:ac2", "covered:ac2"):
            with self.subTest(name=name):
                report = run(links=[link("ac1")], clearances=[clearance(), clearance(name)])
                self.assertEqual(cov(report, "ac2").status, Coverage.UNCOVERED)
                self.assertFalse(report.ready)
                if name != "covered:ac2":
                    self.assertTrue(ignored_with(report, "refused:"), report.ignored)

    def test_clearance_cannot_turn_an_unknown_into_covered(self):
        report = run(
            links=[link("ac1", test="nope"), link("ac2")],
            clearances=[clearance(), clearance("ac1"), clearance("unknown:ac1")],
        )
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertFalse(report.ready)

    def test_clearance_of_an_absent_flag_is_unused(self):
        report = run(clearances=[clearance(), clearance("fixture-only:nothing")])
        self.assertTrue(report.ready)
        self.assertTrue(ignored_with(report, "unused: clearance"))

    def test_only_policy_reviewers_clear_flags_with_a_note_on_this_revision(self):
        for changes, prefix in (
            ({"by": WORKER}, "untrusted:"),
            ({"by": "RNavarrete-Factory-Bot"}, "untrusted:"),
            ({"by": MAPPER}, "untrusted:"),
            ({"by": ""}, "untrusted:"),
            ({"note": "  "}, "incomplete:"),
            ({"commit": NEW_HEAD}, "stale:"),
            ({"base_commit": NEW_MAIN}, "stale:"),
        ):
            with self.subTest(changes=changes):
                report = run(clearances=[clearance(**changes)])
                self.assertIn(FILE_FLAG, open_keys(report))
                self.assertTrue(ignored_with(report, prefix), report.ignored)
                self.assertFalse(report.ready)

    def test_reviewer_is_matched_ignoring_case(self):
        for by in ("RNAVARRETE", "rnavarrete"):
            with self.subTest(by=by):
                report = run(clearances=[clearance(by=by)])
                self.assertTrue(report.ready, report.blockers)
                flag = next(f for f in report.flags if f.key == FILE_FLAG)
                self.assertEqual(flag.cleared_by, by)
                self.assertIn("addBook's test is unchanged", flag.note)

    def test_policy_cannot_make_the_worker_a_reviewer(self):
        policy = AssertionPolicy(reviewers=frozenset({REVIEWER, WORKER}))
        report = run(clearances=[clearance(by=WORKER)], policy=policy)
        self.assertIn(FILE_FLAG, open_keys(report))

    def test_revised_and_reapproved_contract_is_verified_afresh(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["acceptance_criteria"] = [c for c in raw["acceptance_criteria"] if c["id"] != "ac2"]
        revised = freeze(raw)
        approved = digest(revised)
        cand = candidate(approved)
        v = eng156(cand, contract=revised, approved=approved)
        fresh = str(approved)
        report = run(
            links=[link("ac1", contract_digest=fresh)],
            proofs=[proof("ac1", contract_digest=fresh)],
            clearances=[clearance(contract_digest=fresh)],
            cand=cand,
            verification=v,
            contract=revised,
            approved=approved,
        )
        self.assertTrue(report.ready, report.blockers)
        # Evidence collected for the old contract does not carry over to the new one.
        reused = run(
            links=[link("ac1")],
            proofs=[proof("ac1")],
            cand=cand,
            verification=v,
            contract=revised,
            approved=approved,
        )
        self.assertFalse(reused.ready)
        self.assertEqual(statuses(reused)["ac1"], Coverage.UNCOVERED)
        self.assertTrue(ignored_with(reused, "stale: link"))
        self.assertEqual(report.contract_digest, approved.value)
        # The old report for the original digest does not carry over.
        old = run(links=[link("ac1")], proofs=[proof("ac1")])
        self.assertFalse(old.ready)

    def test_report_is_invalidated_by_a_new_push_or_moved_base(self):
        report = run()
        self.assertEqual(report.still_current(candidate()), [])
        self.assertIn("candidate moved", report.still_current(candidate(head_commit=NEW_HEAD))[0])
        self.assertIn("base moved", report.still_current(candidate(base_commit=NEW_MAIN))[0])

    def test_old_evidence_on_a_new_push_is_not_ready(self):
        moved = candidate(head_commit=NEW_HEAD)
        v = verify(CONTRACT, DIGEST, moved, all_green(commit=NEW_HEAD), [seen(commit=NEW_HEAD)])
        report = run(cand=moved, verification=v)
        self.assertEqual(statuses(report)["ac1"], Coverage.UNCOVERED)
        self.assertFalse(report.ready)


# --- AC5 --------------------------------------------------------------------------


class IndependentOfWriterTest(unittest.TestCase):
    """AC5: runs independently of the writer; needs no custom reviewer infrastructure."""

    def test_worker_cannot_map_prove_excuse_or_clear(self):
        for login in (WORKER, WORKER.upper(), "RNavarrete-Factory-Bot"):
            with self.subTest(login=login):
                report = run(
                    links=[link("ac1", mapper=login), link("ac2", mapper=login)],
                    proofs=[proof("ac1", by=login), proof("ac2", by=login)],
                    limits=[limit("ac1", by=login)],
                    clearances=[clearance(by=login)],
                )
                self.assertEqual(cov(report).status, Coverage.UNCOVERED)
                self.assertEqual(cov(report, "ac2").status, Coverage.UNCOVERED)
                self.assertIn(FILE_FLAG, open_keys(report))
                self.assertFalse(report.ready)
                self.assertGreaterEqual(len(ignored_with(report, "untrusted:")), 3)

    def test_worker_proofs_do_not_count_even_with_good_links(self):
        report = run(proofs=[proof("ac1", by=WORKER), proof("ac2")], limits=[limit(by=WORKER)])
        self.assertIn("no-failure-proof:ac1", open_keys(report))

    def test_policy_naming_the_mapper_as_worker(self):
        policy = AssertionPolicy(trust=TrustPolicy(worker_logins=frozenset({MAPPER})))
        report = run(policy=policy)
        self.assertEqual(cov(report).status, Coverage.UNCOVERED)

    def test_inputs_are_unchanged(self):
        raw = json.loads(EXAMPLE.read_text())
        before = copy.deepcopy(raw)
        frozen = freeze(raw)
        cand = candidate()
        v = eng156(cand)
        lists = (
            [link("ac1"), link("ac2")],
            [src(HEAD_TEST), src(BASE_TEST, BASE)],
            [proof("ac1"), proof("ac2", outcome=ProofOutcome.PASSED)],
            [limit("ac2")],
            [clearance(), clearance("ac1")],
        )
        snapshot = copy.deepcopy(lists)
        v_snapshot = copy.deepcopy(v)
        for contract in (frozen, raw):
            map_assertions(contract, digest(raw), cand, v, *lists[:4], control(), lists[4])
        self.assertEqual(raw, before)
        self.assertEqual(lists, snapshot)
        self.assertEqual(v, v_snapshot)
        self.assertEqual(cand, candidate())

    def test_report_and_inputs_are_frozen(self):
        report = run()
        with self.assertRaises(FrozenInstanceError):
            report.criteria[0].status = Coverage.COVERED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            report.flags[0].cleared_by = WORKER  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            link().mapper = WORKER  # type: ignore[misc]

    def test_report_offers_no_approval_merge_or_release(self):
        names = {n for n in dir(run()) if not n.startswith("_")}
        for word in ("approve", "merge", "release", "deploy", "publish", "push"):
            self.assertFalse([n for n in names if word in n], word)

    def test_module_does_no_io_and_needs_no_custom_infrastructure(self):
        path = ROOT / "verify" / "assertions.py"
        tree = ast.parse(path.read_text(), str(path))
        allowed = {
            "__future__",
            "bisect",
            "posixpath",
            "re",
            "collections",
            "dataclasses",
            "enum",
            "controller",
            "verify",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
                if roots == {"controller"}:
                    self.assertIn(node.module, {"controller.contract", "controller.interfaces"})
            else:
                continue
            self.assertLessEqual(roots, allowed, roots)

    def test_inputs_reject_malformed_commits(self):
        with self.assertRaises(ValueError):
            src(HEAD_TEST, commit="main")
        with self.assertRaises(ValueError):
            link(commit=HEAD.upper())
        with self.assertRaises(ValueError):
            proof(code_commit="abc")
        with self.assertRaises(ValueError):
            proof(outcome="failed-assertion")
        with self.assertRaises(ValueError):
            clearance(commit="x")
        with self.assertRaises(ValueError):
            limit(base_commit="")


class ControlChangeReportTest(unittest.TestCase):
    """Reading the pilot's control-change.json; a flagged report is a flag, never a pass."""

    def read(self, data):
        return ControlChangeReport.from_json(
            data,
            url=CC_URL,
            repository=REPO,
            workflow_path=".github/workflows/ci.yml",
            app="github-actions",
        )

    def test_reads_pilot_keys(self):
        r = self.read(
            {"headCommit": HEAD, "baseCommit": MAIN, "flagged": True, "reasons": ["a", "b"]}
        )
        self.assertEqual((r.head_commit, r.base_commit), (HEAD, MAIN))
        self.assertTrue(r.flagged)
        self.assertEqual(r.reasons, ("a", "b"))
        self.assertEqual((r.url, r.repository), (CC_URL, REPO))

    def test_clean_report_from_json_keeps_readiness(self):
        r = self.read({"headCommit": HEAD, "baseCommit": MAIN, "flagged": False, "reasons": []})
        self.assertFalse(r.flagged)
        self.assertTrue(run(control_change=r).ready)

    def test_anything_but_explicit_false_is_flagged(self):
        for value in (None, "false", 0, "no"):
            with self.subTest(value=value):
                data = {"headCommit": HEAD, "baseCommit": MAIN, "reasons": []}
                if value is not None:
                    data["flagged"] = value
                r = self.read(data)
                self.assertTrue(r.flagged)
                self.assertFalse(run(control_change=r).ready)

    def test_missing_or_bad_commits_are_refused(self):
        for data in (
            {"baseCommit": MAIN},
            {"headCommit": HEAD},
            {"headCommit": "x", "baseCommit": MAIN},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.read(data)

    def test_reasons_that_are_not_a_list_are_dropped(self):
        r = self.read({"headCommit": HEAD, "baseCommit": MAIN, "flagged": True, "reasons": "x"})
        self.assertEqual(r.reasons, ())

    def test_constructor_refuses_non_bool_flag_and_string_reasons(self):
        with self.assertRaises(ValueError):
            control(flagged="false")
        with self.assertRaises(ValueError):
            control(reasons="package.json")


class RenderTest(unittest.TestCase):
    """AC1: the rendered map names digest, commit, each criterion and each flag."""

    def test_render_names_digest_commit_criteria_and_flags(self):
        report = run(proofs=[proof("ac2")], clearances=[])
        text = render(report)
        self.assertIn(DIGEST.value, text)
        self.assertIn(HEAD, text)
        self.assertIn(MAIN, text)
        self.assertIn("this is not an approval", text)
        self.assertIn("**no**", text)
        for cid in ("ac1", "ac2", "ac3"):
            self.assertIn(f"| {cid} | covered |", text)
        for f in report.flags:
            self.assertIn(f.key, text)
        self.assertIn(f"`{HEAD[:12]}`", text)

    def test_ready_report_says_yes(self):
        text = render(run())
        self.assertIn("**yes**", text)
        self.assertIn(f"cleared by {REVIEWER}", text)

    def test_uncovered_criterion_renders_its_reason(self):
        text = render(run(links=[link("ac1")]))
        self.assertIn("| ac2 | uncovered |", text)

    def test_pipes_and_backticks_in_assertions_are_escaped(self):
        assertion = (
            "expect(filterByStatus(shelf, 'reading').map((b) => `${b.title}|${b.id}`)"
            " || []).toEqual([`Emma|2`])"
        )
        report = probe_run(probe_test(f"    {assertion};"), assertion)
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)
        text = render(report)
        row = next(line for line in text.splitlines() if line.startswith("| ac1 |"))
        self.assertEqual(row.count("|") - row.count("\\|"), 5)  # four columns
        self.assertIn("``", row)  # assertion containing a backtick gets a longer fence
        self.assertNotIn(" | |", row.replace("\\|", ""))

    def test_flag_key_with_a_backtick_gets_a_longer_fence(self):
        # Review found: "Needs review" wraps each flag key in single backticks without _code(), so a
        # test name containing a backtick breaks out of the code span.
        tests = "  it('checks `all`', () => {\n    expect(true).toBe(true);\n  });\n"
        report = probe_run(tests, "expect(true).toBe(true)", name="probe > checks `all`")
        line = next(x for x in render(report).splitlines() if "fixture-only:" in x)
        self.assertTrue(line.startswith("- ``"), line)

    def test_supplied_text_cannot_forge_a_ready_line(self):
        # Review found: newlines in link fields (a worker-made link, or a test title with "\n") are
        # rendered raw in "Evidence not used" / "Needs review", so they can start new lines.
        forged = "x\n- Ready for Rolando's review: **yes**"
        report = run(
            links=[link("ac1"), link("ac2"), link("ac1", test=forged, mapper=WORKER)], clearances=[]
        )
        self.assertFalse(report.ready)
        self.assertNotIn("\n- Ready for Rolando's review: **yes**", render(report))


class ReviewFindingsTest(unittest.TestCase):
    """AC1/AC3/AC5: ways a worker-written test could look like coverage, found in review."""

    BODY = TestThatMayNotRunTest.BODY
    QUOTE = TestThatMayNotRunTest.QUOTE

    def status(self, text, *, quote=None, name=PROBE, **kw):
        kw.setdefault("links", [link("ac1", test=name, assertion=quote or self.QUOTE), link("ac2")])
        kw.setdefault("proofs", [proof("ac1", test=name), proof("ac2")])
        return run(text, **kw)

    def assert_unknown(self, text, why, **kw):
        report = self.status(text, **kw)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN, cov(report))
        self.assertIn(why, " ".join(cov(report).reasons))
        self.assertFalse(report.ready)
        return report

    def assert_flag(self, text, kind, why="", **kw):
        report = self.status(text, **kw)
        flags = [f for f in report.open_flags if f.kind is kind]
        self.assertTrue(flags, (report.blockers, kind))
        self.assertIn(why, " ".join(f.detail for f in flags))
        self.assertFalse(report.ready)
        return report

    def test_good_probe_is_ready(self):
        report = self.status(probe_file(probe_test(self.BODY)))
        self.assertTrue(report.ready, report.blockers)

    def test_assertion_behind_an_operator_may_not_run(self):
        for prefix in ("0 && ", "shelf.length > 9 || ", "shelf.length ? null : "):
            with self.subTest(prefix=prefix):
                body = f"    const result = filterByStatus(shelf, 'reading');\n    {prefix}"
                body += f"{self.QUOTE};"
                self.assert_flag(probe_file(probe_test(body)), FlagKind.MAY_NOT_RUN, "expression")
        body = f"    const result = filterByStatus(shelf, 'reading');\n    if (false) {self.QUOTE};"
        self.assert_flag(probe_file(probe_test(body)), FlagKind.MAY_NOT_RUN, "`if` condition")
        body = (
            "    const result = filterByStatus(shelf, 'reading');\n"
            f"    if (false) {{}} else {self.QUOTE};"
        )
        self.assert_flag(probe_file(probe_test(body)), FlagKind.MAY_NOT_RUN, "`else` branch")

    def test_await_before_the_assertion_is_fine(self):
        body = "    const result = filterByStatus(shelf, 'reading');\n    await " + self.QUOTE + ";"
        text = probe_file(probe_test(body).replace("() => {", "async () => {"))
        self.assertTrue(self.status(text).ready)

    def test_runtime_skips_make_it_unknown(self):
        cases = {
            "ctx.skip()": probe_file(
                "  it('checks it', (ctx) => {\n    ctx.skip();\n" + self.BODY + "\n  });\n"
            ),
            "destructured skip": probe_file(
                "  it('checks it', ({ skip }) => {\n    skip();\n" + self.BODY + "\n  });\n"
            ),
            "beforeEach": probe_file(
                "  beforeEach((ctx) => ctx.skip());\n" + probe_test(self.BODY)
            ),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assert_unknown(text, "skips tests at runtime")

    def test_options_argument_that_can_skip_makes_it_unknown(self):
        for opts in ("{ skip: true }", "{ fails: true }", "{ todo: true }", "{ ...opts }"):
            with self.subTest(opts=opts):
                text = probe_file(f"  it('checks it', {opts}, () => {{\n" + self.BODY + "\n  });\n")
                self.assert_unknown(text, "options argument")
        other_only = probe_file(
            "  it('other', { only: true }, () => {});\n" + probe_test(self.BODY)
        )
        self.assert_unknown(other_only, "options argument")

    def test_timeout_option_is_fine(self):
        text = probe_file(
            "  it('checks it', { timeout: 5000 }, () => {\n" + self.BODY + "\n  });\n"
        )
        self.assertTrue(self.status(text).ready, self.status(text).blockers)

    def test_shadowed_test_functions_make_it_unknown(self):
        cases = {
            "function it": "function it(_n: string, _f: () => void) {}\n",
            "const expect": "const expect = (_v: unknown) => ({ toHaveLength() {} });\n",
            "let test": "let test = 1;\n",
            "assignment": "globalThis.it = () => {};\n",
            "destructured": "const { describe: d, it } = await import('./helpers');\n",
        }
        for label, prefix in cases.items():
            with self.subTest(label):
                text = probe_file(probe_test(self.BODY)).replace(
                    "\nconst NOW", "\n" + prefix + "const NOW", 1
                )
                self.assert_unknown(text, "cannot be trusted")
        helper = probe_file(probe_test(self.BODY)).replace(
            "import { describe, expect, it } from 'vitest';",
            "import { describe, expect } from 'vitest';\nimport { it } from './helpers';",
        )
        self.assert_unknown(helper, "instead of vitest")

    def test_test_declared_where_it_may_never_register_is_unknown(self):
        cases = {
            "uncalled function": "  function never() {\n" + probe_test(self.BODY) + "  }\n",
            "if (false) block": "  if (false) {\n" + probe_test(self.BODY) + "  }\n",
            "braceless if": "  if (false)\n" + probe_test(self.BODY),
            "after return": "  return;\n" + probe_test(self.BODY),
            "arrow": "  const later = () =>\n" + probe_test(self.BODY),
        }
        for label, tests in cases.items():
            with self.subTest(label):
                self.assert_unknown(probe_file(tests), "line ")

    def test_aliased_or_bracketed_test_functions_make_it_unknown(self):
        for alias in (
            "  const d = describe.skip;\n  d('x', () => {});\n",
            "  describe['skip']('x', () => {});\n",
            "  test.extend({});\n",
            "  describe.call(null, 'x', () => {});\n",
        ):
            with self.subTest(alias=alias):
                text = probe_file(alias + probe_test(self.BODY))
                self.assert_unknown(text, "other than as a direct call")

    def test_division_after_increment_is_not_read_as_a_regex(self):
        # `n++ / 1 // it(...)` is a division followed by a comment: no live test in it.
        tests = (
            "  let n = 0;\n"
            "  n++ / 1 // it('checks it', () => { const result = filterByStatus(shelf, 'reading'); "
            "expect(result).toHaveLength(1); });\n"
        )
        self.assert_unknown(probe_file(tests), "no test with that full name")

    def test_weak_matchers_are_flagged(self):
        for assertion, why in (
            ("expect(filterByStatus(shelf, 'reading')).toBeDefined()", "toBeDefined"),
            ("expect(filterByStatus(shelf, 'reading')).toBeTruthy()", "toBeTruthy"),
            ("expect(filterByStatus(shelf, 'reading')).not.toEqual([])", "negated"),
            ("expect(filterByStatus(shelf, 'reading')).toEqual(expect.anything())", "anything"),
            ("expect(() => filterByStatus(shelf, 'x')).toThrow()", "any error"),
            ("assert(filterByStatus(shelf, 'reading'))", "truthy"),
        ):
            with self.subTest(assertion=assertion):
                text = probe_file(probe_test(f"    {assertion};"))
                self.assert_flag(text, FlagKind.WEAK_ASSERTION, why, quote=assertion)

    def test_precise_matchers_are_not_weak(self):
        assertion = "expect(() => filterByStatus(shelf, 'x')).toThrow('unknown status')"
        report = self.status(probe_file(probe_test(f"    {assertion};")), quote=assertion)
        self.assertNotIn(FlagKind.WEAK_ASSERTION, open_kinds(report))

    def test_expected_value_from_the_same_code_through_a_variable_mirrors_it(self):
        assertion = "expect(filterByStatus(shelf, 'reading')).toEqual(want)"
        body = f"    const want = filterByStatus(shelf, 'reading');\n    {assertion};"
        self.assert_flag(
            probe_file(probe_test(body)), FlagKind.MIRRORS_CODE, "filterByStatus", quote=assertion
        )

    def test_mentioning_product_code_without_asserting_on_it_is_fixture_only(self):
        body = (
            "    void filterByStatus;\n"
            "    const result = shelf.filter((b) => b.status === 'reading');\n"
            f"    {self.QUOTE};"
        )
        self.assert_flag(probe_file(probe_test(body)), FlagKind.FIXTURE_ONLY, "not traced")

    def test_same_variable_name_in_another_test_does_not_count(self):
        # `result` is bound to filterByStatus(...) in another test; this test's own
        # `result` is a fixture.
        body = (
            "    const result = shelf.filter((b) => b.status === 'reading');\n    "
            + self.QUOTE
            + ";"
        )
        self.assert_flag(probe_file(probe_test(body)), FlagKind.FIXTURE_ONLY, "not traced")

    def test_root_relative_mocks_and_spies_are_fixture_only(self):
        cases = {
            "root mock": "vi.mock('/src/books');\n",
            "spyOn": "import * as lib from '../src/books';\nvi.spyOn(lib, 'filterByStatus');\n",
            "computed mock": "vi.mock(name);\n",
        }
        for label, prefix in cases.items():
            with self.subTest(label):
                text = probe_file(probe_test(self.BODY)).replace(
                    "\nconst NOW", "\n" + prefix + "const NOW", 1
                )
                self.assert_flag(text, FlagKind.FIXTURE_ONLY, "")

    def test_two_findings_on_one_test_are_both_shown_under_one_key(self):
        text = probe_file(probe_test("    expect(true).toBe(true);")).replace(
            "\nconst NOW", "\nvi.mock('../src/books');\nconst NOW", 1
        )
        report = self.status(text, quote="expect(true).toBe(true)")
        flag = next(f for f in report.flags if f.key == f"fixture-only:{TEST_FILE} > {PROBE}")
        self.assertIn("literal", flag.detail)
        self.assertIn("mocks product code", flag.detail)

    def test_pathological_input_is_unknown_not_a_crash(self):
        deep = probe_file(
            probe_test("    const s = " + "`${" * 3000 + "1" + "}`" * 3000 + ";\n" + self.BODY)
        )
        report = self.status(deep)
        self.assertEqual(cov(report).status, Coverage.UNKNOWN)
        self.assertIn("could not be read reliably", " ".join(cov(report).reasons))

    def test_large_file_parses_quickly(self):
        import time

        many = "".join(
            f"  it('case {i}', () => {{\n"
            "    expect(filterByStatus(shelf, 'reading')).toHaveLength(1);\n  });\n"
            for i in range(3000)
        )
        text = probe_file(many + probe_test(self.BODY))
        started = time.monotonic()
        report = self.status(text)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(cov(report).status, Coverage.COVERED)

    def test_problems_with_other_links_are_reported_even_when_covered(self):
        report = run(links=[link("ac1"), link("ac1", test="no such test"), link("ac2")])
        self.assertEqual(cov(report).status, Coverage.COVERED)
        self.assertTrue([i for i in report.ignored if "no such test" in i])

    def test_worker_login_variants_are_not_independent(self):
        for login in (WORKER + " ", WORKER + "[bot]", WORKER.upper(), " " + WORKER):
            with self.subTest(login=login):
                report = run(links=[link("ac1", mapper=login), link("ac2")])
                self.assertEqual(cov(report).status, Coverage.UNCOVERED)

    def test_bot_spelling_of_the_reviewer_cannot_clear(self):
        report = run(clearances=[clearance(by=REVIEWER + "[bot]")])
        self.assertIn(FILE_FLAG, open_keys(report))

    def test_paths_vitest_does_not_collect_are_not_run(self):
        for path in ("tests/../x.test.ts", "tests/node_modules/a.test.ts", "tests//a.test.ts"):
            with self.subTest(path=path):
                report = run(links=[link("ac1", path=path), link("ac2")])
                self.assertIn("does not run", " ".join(cov(report).reasons))

    def test_escaped_title_matches_its_text(self):
        text = probe_file(probe_test(self.BODY, name="caf\\u00e9 \\x41"))
        report = self.status(text, name="probe > café A")
        self.assertEqual(cov(report).status, Coverage.COVERED, cov(report).reasons)


if __name__ == "__main__":
    unittest.main()
