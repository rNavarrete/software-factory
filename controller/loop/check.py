"""Judge one worker PR against its approved contract (ENG-145).

``assess`` runs the whole independent check, in this order, on what the
collector read from GitHub and what is in the ledger:

1. ``controller.loop.collect``: the PR, the trusted CI run for its exact head, and the
   text of every changed file at the head and at its merge base.
2. ``verify.review``: the independent mapper's review comment on the PR.
3. Rolando's signed observations and clearances for this exact revision.
4. ``verify.criteria`` then ``verify.assertions``, exactly as the bypass cases
   in redteam/ run them.

``ready`` means ready for Rolando's review, nothing more. The worker's own
account (its PR body, a green session, its own test run) never makes a
candidate ready: only evidence it did not produce does. Merging and releasing
stay Rolando's, on GitHub.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from controller.interfaces import ContractDigest, LedgerEvent, RunId
from controller.ledger import kinds
from controller.loop.collect import Collected
from verify.assertions import AssertionReport, Coverage, map_assertions
from verify.assertions import render as render_assertions
from verify.criteria import Report, Verdict, verify
from verify.criteria import render as render_criteria
from verify.review import Review, read_review

CHECK_NAME = "independent-verification"
CI_CHECK_NAME = "trusted-ci/verified"


@dataclass(frozen=True)
class Assessment:
    collected: Collected
    review: Review = field(default_factory=Review)
    criteria: Report | None = None
    assertions: AssertionReport | None = None
    observable: frozenset[str] = frozenset()
    """Criteria checked by a person (observable-behavior, human-review)."""

    @property
    def ready(self) -> bool:
        return (
            self.collected.usable
            and self.criteria is not None
            and self.assertions is not None
            and self.criteria.ready
            and self.assertions.ready
        )

    @property
    def blockers(self) -> tuple[str, ...]:
        out = list(self.collected.problems)
        if self.collected.pending:
            out.append("CI has not finished for this exact commit.")
        if self.criteria is not None:
            out += self.criteria.blockers
        if self.assertions is not None:
            out += self.assertions.blockers
        # A behavior only Rolando can look at is not a gap in the evidence:
        # the verifiers word it as one that "cannot be cleared", which sent
        # him the wrong way in the first live run. Say what is really missing.
        owed = sorted(self._owed())
        if owed:
            stale = tuple(
                f"{prefix}{c} is unknown" for c in owed for prefix in ("", "criterion check: ")
            )
            out = [b for b in out if not b.startswith(stale)]
            out += [
                f"{c} needs your own look: nothing you saw is recorded for this commit yet"
                " (run this same command again to give it)"
                for c in owed
            ]
        return tuple(dict.fromkeys(out))

    @property
    def definite_failure(self) -> bool:
        """Something is wrong that no review comment and no answer of Rolando's
        can fix: a collection problem, a failed gate (scope, base, markers,
        untrusted or failed checks) or a criterion that failed."""
        if self.collected.problems:
            return True
        if self.criteria is None or self.assertions is None:
            return False
        if not all(g.ok for g in (*self.criteria.gates, *self.assertions.gates)):
            return True
        return any(c.verdict is Verdict.FAIL for c in self.criteria.criteria)

    @property
    def waiting_on_review(self) -> bool:
        """The independent mapping isn't posted, and nothing it can't fix is wrong."""
        return self.collected.usable and self.review.url is None and not self.definite_failure

    def owed_observations(self, contract: Mapping[str, object]) -> tuple[Mapping, ...]:
        """Observable criteria with no verdict yet, which only Rolando can supply."""
        owed = self._owed()
        return tuple(c for c in contract["acceptance_criteria"] if c["id"] in owed)

    def _owed(self) -> frozenset[str]:
        if self.criteria is None:
            return frozenset()
        return frozenset(
            c.criterion
            for c in self.criteria.criteria
            if c.verdict is Verdict.UNKNOWN and c.criterion in self.observable
        )

    @property
    def only_rolando_missing(self) -> bool:
        """Everything holds except what only Rolando can supply: an observation
        of a behavior criterion, or his look at a flag. Asking him anything
        when something else is wrong would waste his time: no answer of his
        can make such a PR ready."""
        if not self.collected.usable or self.review.url is None:
            return False
        if self.criteria is None or self.assertions is None:
            return False
        if not all(g.ok for g in (*self.criteria.gates, *self.assertions.gates)):
            return False
        owed = self._owed()
        return all(
            c.verdict is Verdict.PASS or c.criterion in owed for c in self.criteria.criteria
        ) and all(
            c.status is Coverage.COVERED or c.criterion in owed for c in self.assertions.criteria
        )

    def open_flags(self):
        """Flags Rolando may look at and clear. Gaps (uncovered, unknown or
        failed criteria) are not flags: nothing clears them."""
        if self.assertions is None:
            return ()
        return self.assertions.open_flags

    def conclusion(self) -> str:
        """For the ledger's checks record: what state the candidate is in."""
        if self.ready:
            return "success"
        if self.collected.pending or self.waiting_on_review:
            return "pending"
        return "action_required" if self.only_rolando_missing else "failure"

    def checks_event(self, run: RunId, now: datetime) -> LedgerEvent | None:
        """The ledger record of this assessment for the candidate commit."""
        c = self.collected
        if c.candidate is None:
            return None
        results = [
            {
                "name": CHECK_NAME,
                "conclusion": self.conclusion(),
                "url": c.pr_url,
            }
        ]
        if c.ci is not None:
            ci = c.ci.verified if c.ci.verified in kinds.CHECK_CONCLUSIONS else None
            results.append(
                {
                    "name": CI_CHECK_NAME,
                    "conclusion": ci or ("pending" if c.pending else "failure"),
                    "workflow": c.ci.workflow_path,
                    "url": c.ci.url,
                }
            )
        return LedgerEvent(
            kinds.CHECKS,
            now,
            run.attempt.task,
            run.attempt,
            run,
            data={"revision": c.candidate.head_commit, "results": tuple(results)},
        )


def assess(
    contract: Mapping[str, object],
    digest: ContractDigest,
    collected: Collected,
    *,
    observations=(),
    clearances=(),
    mappers: frozenset[str] | None = None,
    review: Review | None = None,
) -> Assessment:
    """``review``, when given, is the independent review to use, already
    authenticated by its source (controller.review.workflow); the PR's
    comments are then not read for one."""
    if collected.candidate is None or collected.pending:
        return Assessment(collected)
    candidate = collected.candidate
    if review is None:
        review = (
            read_review(collected.comments, digest.value, candidate)
            if mappers is None
            else read_review(collected.comments, digest.value, candidate, mappers=mappers)
        )
    criteria = verify(
        contract,
        digest,
        candidate,
        collected.results,
        observations,
        collected.claims,
    )
    assertions = map_assertions(
        contract,
        digest,
        candidate,
        criteria,
        links=review.links,
        sources=collected.sources,
        proofs=review.proofs,
        limits=review.limits,
        control_change=collected.control_change,
        clearances=clearances,
    )
    observable = frozenset(
        c["id"]
        for c in contract["acceptance_criteria"]
        if c["evidence"]["type"] in ("observable-behavior", "human-review")
    )
    return Assessment(collected, review, criteria, assertions, observable)


def render(a: Assessment) -> str:
    """The full report for Rolando, as Markdown."""
    c = a.collected
    lines = [f"# Check of PR #{c.pr_number}", "", c.pr_url, ""]
    if c.candidate is not None:
        lines += [
            f"- Candidate commit: `{c.candidate.head_commit}`",
            f"- Base (main) when checked: `{c.candidate.base_commit}`",
            f"- Branch starts from: `{c.candidate.merge_base}`",
            f"- Trusted CI run: {c.ci.url if c.ci else 'none yet'}",
            f"- Independent review: {a.review.url or 'not posted yet'}",
            "",
        ]
    lines += ["## Verdict", "", "Ready for your review." if a.ready else "Not ready.", ""]
    if a.blockers:
        lines += ["## What blocks it", ""] + [f"- {b}" for b in a.blockers] + [""]
    if a.review.ignored:
        lines += ["## Review comments not used", ""] + [f"- {i}" for i in a.review.ignored]
        lines.append("")
    if a.criteria is not None:
        lines += [render_criteria(a.criteria), ""]
    if a.assertions is not None:
        lines += [render_assertions(a.assertions), ""]
    return "\n".join(lines)
