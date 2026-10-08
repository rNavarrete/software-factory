"""An honest pilot task, and the checking pipeline the bypass cases run it through.

``honest()`` builds every record an honest run of the pilot's filter-by-status
task would produce: the approved contract and its digest, the pull request,
trusted CI results, a reviewer's observation, the test file at the candidate
and at its merge base, an independent mapping of each criterion to its
assertion, failure proofs, the CI control-change report and Rolando's clearance
of the one flag an honest change raises. ``evaluate`` runs the criterion
verifier and then the assertion map over it, exactly as the controller will.

Each bypass case changes one thing in this scenario, the way a worker (or text
planted in an issue, PR or CI log) would try to, and checks that the pipeline
stops it for the right reason. The honest scenario itself must come out ready,
otherwise a "blocked" result would mean nothing.

Everything here is offline: no network, no GitHub, no cloud session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from controller.contract import digest, freeze
from controller.interfaces import ContractDigest
from verify.assertions import (
    DEFAULT_ASSERTION_POLICY,
    AssertionLink,
    AssertionPolicy,
    AssertionReport,
    Clearance,
    ControlChangeReport,
    FailureProof,
    FailureProofLimit,
    ProofOutcome,
    TestSource,
    map_assertions,
)
from verify.assertions import render as render_assertions
from verify.criteria import (
    DEFAULT_POLICY,
    Candidate,
    CheckResult,
    Observation,
    Report,
    Source,
    TrustPolicy,
    Verdict,
    WriterClaim,
    verify,
)
from verify.criteria import render as render_criteria

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "schema" / "examples" / "filter-by-status.json"

HEAD = "a" * 40
NEW_HEAD = "b" * 40
MAIN = "c" * 40
"""The tip of the pilot's main when the PR was checked."""
NEW_MAIN = "d" * 40
OLD_HEAD = "e" * 40
REPO = "rNavarrete/factory-pilot-demo"
FORK = "someone-else/factory-pilot-demo"
CI_WORKFLOW = ".github/workflows/ci.yml"
CI_APP = "github-actions"
CI_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/101"
CC_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/102"
PROOF_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/103"
REVIEWER = "rNavarrete"
WORKER = "rnavarrete-factory-bot"
MAPPER = "factory-verifier"
"""An independent read-only verifier session, not the worker."""
TEST_FILE = "tests/books.test.ts"

CONTRACT = freeze(json.loads(EXAMPLE.read_text()))
DIGEST = digest(CONTRACT)
BASE = CONTRACT["base_commit"]
"""The approved base commit, which is also the candidate's merge base."""

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
"""The one flag an honest change raises: adding the import changes the file
outside its tests. Rolando clears it in the honest scenario."""


# --- Record builders --------------------------------------------------------------


def candidate(approved: ContractDigest = DIGEST, **changes) -> Candidate:
    c = Candidate(
        repository=REPO,
        head_commit=HEAD,
        base_commit=MAIN,
        merge_base=BASE,
        branch="claude/filter-by-status-a1",
        pr_title=f"[filter-by-status a1 {approved.short}] Filter books by status",
        pr_body=f"Adds a status filter.\n\n{approved.pr_body_line}\n",
        changed_paths=("src/books.ts", "src/main.ts", TEST_FILE),
    )
    return replace(c, **changes)


def ci(command: str, exit_code: int | None = 0, **changes) -> CheckResult:
    r = CheckResult(
        command=command,
        commit=HEAD,
        base_commit=MAIN,
        exit_code=exit_code,
        source=Source.CI,
        url=CI_URL,
        output_excerpt=f"{command}: ok",
        repository=REPO,
        workflow_path=CI_WORKFLOW,
        app=CI_APP,
    )
    return replace(r, **changes)


def all_green(**changes) -> tuple[CheckResult, ...]:
    return tuple(ci(c, **changes) for c in CONTRACT["verification_commands"])


def observation(**changes) -> Observation:
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


def source(text: str | None, commit: str = HEAD, path: str = TEST_FILE) -> TestSource:
    return TestSource(path=path, commit=commit, text=text)


def link(criterion: str = "ac1", **changes) -> AssertionLink:
    lk = AssertionLink(
        criterion=criterion,
        path=TEST_FILE,
        test=AC1_TEST if criterion == "ac1" else AC2_TEST,
        assertion=AC1_ASSERT if criterion == "ac1" else AC2_ASSERT,
        contract_digest=str(DIGEST),
        commit=HEAD,
        base_commit=MAIN,
        mapper=MAPPER,
        why=f"The assertion checks {criterion}'s statement directly on filterByStatus.",
    )
    return replace(lk, **changes)


def proof(criterion: str = "ac1", **changes) -> FailureProof:
    p = FailureProof(
        criterion=criterion,
        path=TEST_FILE,
        test=AC1_TEST if criterion == "ac1" else AC2_TEST,
        contract_digest=str(DIGEST),
        tests_commit=HEAD,
        code_commit=BASE,
        outcome=ProofOutcome.FAILED_ASSERTION,
        by=MAPPER,
        url=PROOF_URL,
        output_excerpt="AssertionError: expected [ '1', '2', '3', '4' ] to deeply equal",
    )
    return replace(p, **changes)


def control_change(**changes) -> ControlChangeReport:
    r = ControlChangeReport(
        head_commit=HEAD,
        base_commit=MAIN,
        flagged=False,
        reasons=(),
        url=CC_URL,
        repository=REPO,
        workflow_path=CI_WORKFLOW,
        app=CI_APP,
    )
    return replace(r, **changes)


def clearance(flag: str = FILE_FLAG, **changes) -> Clearance:
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


# --- The scenario and the pipeline ------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """Every input the controller's checking step gets for one candidate."""

    contract: object = CONTRACT
    approved: ContractDigest = DIGEST
    """From Rolando's approval record, never from the PR."""
    candidate: Candidate = field(default_factory=candidate)
    results: tuple[CheckResult, ...] = field(default_factory=all_green)
    observations: tuple[Observation, ...] = field(default_factory=lambda: (observation(),))
    claims: tuple[WriterClaim, ...] = ()
    links: tuple[AssertionLink, ...] = field(default_factory=lambda: (link("ac1"), link("ac2")))
    sources: tuple[TestSource, ...] = field(
        default_factory=lambda: (source(HEAD_TEST), source(BASE_TEST, BASE))
    )
    proofs: tuple[FailureProof, ...] = field(default_factory=lambda: (proof("ac1"), proof("ac2")))
    limits: tuple[FailureProofLimit, ...] = ()
    control_change: ControlChangeReport | None = field(default_factory=control_change)
    clearances: tuple[Clearance, ...] = field(default_factory=lambda: (clearance(),))
    trust: TrustPolicy = DEFAULT_POLICY
    policy: AssertionPolicy = DEFAULT_ASSERTION_POLICY
    verification: Report | None = None
    """Use this criterion report instead of computing one (to test a stale report)."""

    def but(self, **changes) -> Scenario:
        return replace(self, **changes)

    def with_head_test(self, text: str | None) -> Scenario:
        """The same scenario with the test file at the candidate replaced."""
        keep = tuple(s for s in self.sources if not (s.path == TEST_FILE and s.commit == HEAD))
        return replace(self, sources=(source(text), *keep))


def honest() -> Scenario:
    return Scenario()


@dataclass(frozen=True)
class Outcome:
    """What the pipeline said about one scenario."""

    ready: bool
    blockers: tuple[str, ...]
    ignored: tuple[str, ...]
    criteria: Report
    assertions: AssertionReport

    @property
    def text(self) -> str:
        """Everything a reason could be found in: blockers, then unused evidence."""
        return "\n".join((*self.blockers, *self.ignored))

    def rendered(self) -> str:
        return render_criteria(self.criteria) + "\n" + render_assertions(self.assertions)


def evaluate(s: Scenario) -> Outcome:
    """Run the criterion verifier and the assertion map, as the controller will."""
    criteria = s.verification or verify(
        s.contract,
        s.approved,
        s.candidate,
        s.results,
        s.observations,
        s.claims,
        policy=s.trust,
    )
    assertions = map_assertions(
        s.contract,
        s.approved,
        s.candidate,
        criteria,
        links=s.links,
        sources=s.sources,
        proofs=s.proofs,
        limits=s.limits,
        control_change=s.control_change,
        clearances=s.clearances,
        policy=s.policy,
    )
    return Outcome(
        ready=criteria.ready and assertions.ready,
        blockers=tuple(dict.fromkeys((*criteria.blockers, *assertions.blockers))),
        ignored=tuple(dict.fromkeys((*criteria.ignored, *assertions.ignored))),
        criteria=criteria,
        assertions=assertions,
    )
