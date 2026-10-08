"""Findings: what an independent review found, where each one goes next (ENG-156).

A review of one exact candidate produces a list of ``Finding`` records. Each has
a stable id, so the same problem keeps the same id across pushes and a repair
can be checked off: a finding from an earlier revision that the new revision
no longer raises is ``resolved``.

Every finding is routed:

- ``repair``: a routine technical failure (a failed check, a failed or
  uncovered criterion, a test that doesn't prove anything, a file out of
  scope). It goes to the repair worker (ENG-160), never to Rolando.
- ``rolando``: only what needs his judgment: a behavior only a person can
  check, a change to a protected control (workflows, check scripts, an
  existing test), something about the product a reviewer raised, or evidence
  the factory could not trust. These reach him through Linear (ENG-178).
- ``wait``: nothing is wrong yet; evidence is still coming (CI running).

A finding is evidence for Rolando, never an approval, a merge or a release.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from verify.assertions import AssertionReport, Coverage, Flag, FlagKind
from verify.criteria import Report, Verdict


class Severity(Enum):
    BLOCKING = "blocking"
    """The candidate can't be ready while this stands."""
    ADVISORY = "advisory"
    """Worth a look; does not hold the candidate back."""


class Route(Enum):
    REPAIR = "repair"
    ROLANDO = "rolando"
    WAIT = "wait"


@dataclass(frozen=True)
class Finding:
    id: str
    """Stable across revisions: the same problem keeps the same id."""
    severity: Severity
    category: str
    route: Route
    summary: str
    """One plain sentence."""
    evidence: str
    """What shows it: a check run link, a file and line, the gate that failed."""
    suggested_action: str
    commit: str
    """The exact candidate commit the finding was raised on."""
    criterion: str | None = None
    resolved: bool = False
    source: str = "verifier"
    """``verifier`` (computed from independent evidence) or the reviewer's login."""
    claimed_id: str | None = None
    """The earlier finding a reviewer says this one repeats (``adopt_ids``)."""

    def as_data(self) -> dict[str, object]:
        """The finding as JSON-ready data, for the ledger and the repair worker."""
        return {
            "id": self.id,
            "severity": self.severity.value,
            "category": self.category,
            "route": self.route.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "commit": self.commit,
            "criterion": self.criterion,
            "resolved": self.resolved,
            "source": self.source,
        }


def finding_id(category: str, criterion: str | None, subject: str) -> str:
    """``F-`` and 12 hex chars from what the finding is about, never the commit."""
    raw = "\0".join((category, criterion or "", subject)).encode()
    return "F-" + hashlib.sha256(raw).hexdigest()[:12]


def _f(
    category: str,
    route: Route,
    summary: str,
    evidence: str,
    action: str,
    commit: str,
    *,
    criterion: str | None = None,
    subject: str = "",
    severity: Severity = Severity.BLOCKING,
    source: str = "verifier",
    claimed_id: str | None = None,
) -> Finding:
    return Finding(
        id=finding_id(category, criterion, subject or summary),
        severity=severity,
        category=category,
        route=route,
        summary=summary,
        evidence=evidence,
        suggested_action=action,
        commit=commit,
        criterion=criterion,
        source=source,
        claimed_id=claimed_id,
    )


_ID_RE = re.compile(r"F-[0-9a-f]{12}")


def finding_from_data(d: object) -> Finding:
    """A finding read back from the ledger. Raises KeyError, TypeError or
    ValueError on anything malformed, so a bad record grants nothing."""
    if not isinstance(d, Mapping):
        raise TypeError("a finding must be an object")
    texts = {}
    for key in ("id", "category", "summary", "evidence", "suggested_action", "commit", "source"):
        value = d[key]
        if not isinstance(value, str):
            raise TypeError(f"finding {key} must be text")
        texts[key] = value
    if not _ID_RE.fullmatch(texts["id"]):
        raise ValueError("malformed finding id")
    criterion = d.get("criterion")
    if criterion is not None and not isinstance(criterion, str):
        raise TypeError("finding criterion must be text or null")
    resolved = d.get("resolved", False)
    if not isinstance(resolved, bool):
        raise TypeError("finding resolved must be true or false")
    return Finding(
        id=texts["id"],
        severity=Severity(d["severity"]),
        category=texts["category"],
        route=Route(d["route"]),
        summary=texts["summary"],
        evidence=texts["evidence"],
        suggested_action=texts["suggested_action"],
        commit=texts["commit"],
        criterion=criterion,
        resolved=resolved,
        source=texts["source"],
    )


# Gates of verify.criteria, and who fixes each one.
_GATE_ROUTES = {
    "pr title": (Route.REPAIR, "markers", "Use the PR title marker the task names."),
    "pr body": (Route.REPAIR, "markers", "Put the approved Contract-Digest line in the PR body."),
    "base": (Route.REPAIR, "base", "Start the branch from the approved base commit."),
    "scope": (Route.REPAIR, "scope", "Undo the changes to files the task doesn't permit."),
    "verification commands": (
        Route.REPAIR,
        "checks-failed",
        "Make every verification command pass on the candidate.",
    ),
}
# Anything else (the contract itself, the repository, the branch: a branch
# that isn't the attempt's, or an attempt over its approved budget) means the factory can't
# trust what it is looking at: that is never the repair worker's to fix.
_INTEGRITY = (Route.ROLANDO, "integrity", "Stop and check what the factory is reviewing.")

# Collector problems (controller/loop/collect.py's wording) that a repair push
# fixes. Every other problem means the factory can't trust what it is looking
# at (another repository or branch, a closed PR, an author that isn't the
# worker, evidence it can't read): Rolando's to look at.
# Each names the gate it matches, so the same problem keeps one id whichever
# way it was found.
_REPAIRABLE_PROBLEMS = (
    (
        re.compile(r"^The trusted CI run \(\S+\) did not pass"),
        "verification commands",
    ),
    (re.compile(r"^PR #\d+'s title is "), "pr title"),
    (re.compile(r"^PR #\d+'s body has no single Contract-Digest line"), "pr body"),
    (
        re.compile(
            r"^The CI run \(\S+\) checked [0-9a-f]+ on base [0-9a-f]+, not [0-9a-f]+ on"
            r" [0-9a-f]+: its results may be for an older main\. Re-run CI on the PR\.$"
        ),
        "stale-ci",
    ),
)
_STALE_CI = (
    Route.REPAIR,
    "stale-ci",
    "Bring main into the branch and push, so CI checks the current base.",
)


def _problem_route(problem: str) -> tuple[Route, str, str, str]:
    for pattern, gate in _REPAIRABLE_PROBLEMS:
        if pattern.search(problem):
            route, category, action = _GATE_ROUTES.get(gate, _STALE_CI)
            return route, category, action, gate
    return (
        Route.ROLANDO,
        "integrity",
        "Check the PR on GitHub; the factory won't review it as it is.",
        problem,
    )


# Flags that touch a protected control: only Rolando can accept them.
_PROTECTED = frozenset(
    {
        FlagKind.CONTROL_CHANGE,
        FlagKind.DELETED_TEST,
        FlagKind.WEAKENED_TEST,
        FlagKind.CHANGED_TEST,
        FlagKind.CHANGED_SETUP,
        FlagKind.SUPPRESSION,
    }
)


def from_reports(
    criteria: Report | None,
    assertions: AssertionReport | None,
    *,
    commit: str,
    problems: Sequence[str] = (),
    observable: Iterable[str] = (),
    mapped: bool = True,
) -> tuple[Finding, ...]:
    """Findings from the independent check of one exact revision.

    ``problems`` are the collector's: the PR could not be judged at all (it was
    closed, retargeted, its markers or CI couldn't be trusted). ``observable``
    are the criteria only a person can check. Before a reviewer's mapping
    exists (``mapped`` false) no criterion can be covered yet, so coverage is
    not reported; gates and flags still are.
    """
    observable = frozenset(observable)
    out: list[Finding] = []
    for p in problems:
        route, category, action, subject = _problem_route(p)
        out.append(
            _f(
                category,
                route,
                p,
                "read from GitHub by the factory's collector",
                action,
                commit,
                subject=subject,
            )
        )
    if criteria is not None:
        for g in criteria.gates:
            if g.ok:
                continue
            route, category, action = _GATE_ROUTES.get(g.name, _INTEGRITY)
            out.append(
                _f(
                    category,
                    route,
                    f"{g.name}: {g.detail}",
                    f"gate {g.name!r}",
                    action,
                    commit,
                    subject=g.name,
                )
            )
        for c in criteria.criteria:
            if c.verdict is Verdict.PASS:
                continue
            evidence = "; ".join(c.reasons) or c.verdict.value
            if c.verdict is Verdict.FAIL:
                out.append(
                    _f(
                        "criterion-failed",
                        Route.REPAIR,
                        f"{c.criterion} failed: {c.statement}",
                        evidence,
                        f"Make {c.criterion} hold on the candidate.",
                        commit,
                        criterion=c.criterion,
                    )
                )
            elif c.criterion in observable:
                out.append(
                    _f(
                        "needs-observation",
                        Route.ROLANDO,
                        f"{c.criterion} is a behavior only a person can check: {c.statement}",
                        evidence,
                        "Rolando looks at it on this commit and records what he saw.",
                        commit,
                        criterion=c.criterion,
                    )
                )
            else:
                out.append(
                    _f(
                        "criterion-unknown",
                        Route.REPAIR,
                        f"{c.criterion} is not shown either way: {c.statement}",
                        evidence,
                        "Get the missing check result for the candidate.",
                        commit,
                        criterion=c.criterion,
                    )
                )
    if assertions is not None:
        for g in assertions.gates:
            if not g.ok:
                route, category, action = _INTEGRITY
                out.append(
                    _f(
                        category,
                        route,
                        f"{g.name}: {g.detail}",
                        f"gate {g.name!r}",
                        action,
                        commit,
                        subject=g.name,
                    )
                )
        for c in assertions.criteria if mapped else ():
            if c.status is Coverage.COVERED or c.criterion in observable:
                continue
            out.append(
                _f(
                    f"assertion-{c.status.value}",
                    Route.REPAIR,
                    f"{c.criterion} has no test that independently shows it ({c.status.value})",
                    "; ".join(c.reasons) or c.status.value,
                    f"Add or fix a test whose assertion shows {c.criterion}, and that fails"
                    " without the change.",
                    commit,
                    criterion=c.criterion,
                )
            )
        for flag in assertions.open_flags:
            out.append(_from_flag(flag, commit))
    return _dedupe(out)


def _from_flag(flag: Flag, commit: str) -> Finding:
    if flag.kind in _PROTECTED:
        return _f(
            f"flag-{flag.kind.value}",
            Route.ROLANDO,
            f"A protected control changed ({flag.kind.value}): {flag.subject}",
            flag.detail,
            "Rolando decides whether this change to a protected control is acceptable.",
            commit,
            criterion=flag.criterion,
            subject=flag.key,
        )
    return _f(
        f"flag-{flag.kind.value}",
        Route.REPAIR,
        f"A test doesn't prove what it should ({flag.kind.value}): {flag.subject}",
        flag.detail,
        "Strengthen the test so it checks the behavior and fails without the change.",
        commit,
        criterion=flag.criterion,
        subject=flag.key,
    )


REVIEWER_CATEGORIES = ("code", "test", "scope", "product", "security")


def from_reviewer(
    items: Iterable[dict[str, object]], *, reviewer: str, commit: str
) -> tuple[Finding, ...]:
    """Findings the reviewer job raised beyond the mapping, from its result block.

    Its own ids are not used: the id comes from what it says, so a reviewer
    can't reuse an id to mark someone else's finding resolved. Product and
    security findings go to Rolando; the rest to repair. Malformed entries
    raise ValueError.
    """
    out = []
    for i, item in enumerate(items):
        where = f"findings[{i}]"
        category = item.get("category")
        if category not in REVIEWER_CATEGORIES:
            raise ValueError(f"{where}.category must be one of {list(REVIEWER_CATEGORIES)}")
        severity = item.get("severity")
        if severity not in ("blocking", "advisory"):
            raise ValueError(f"{where}.severity must be blocking or advisory")
        texts = {}
        for key in ("summary", "evidence", "suggested_action"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}.{key} must be non-empty text")
            texts[key] = value.strip()[:2000]
        criterion = item.get("criterion")
        if criterion is not None and not isinstance(criterion, str):
            raise ValueError(f"{where}.criterion must be text or absent")
        route = Route.ROLANDO if category in ("product", "security") else Route.REPAIR
        claimed = item.get("id")
        if claimed is not None and not (isinstance(claimed, str) and _ID_RE.fullmatch(claimed)):
            raise ValueError(f"{where}.id must be a finding id from previous_findings or absent")
        out.append(
            _f(
                f"review-{category}",
                route,
                texts["summary"],
                texts["evidence"],
                texts["suggested_action"],
                commit,
                criterion=criterion,
                severity=Severity(severity),
                source=reviewer,
                claimed_id=claimed,
            )
        )
    return _dedupe(out)


def adopt_ids(findings: Iterable[Finding], previous: Iterable[Finding]) -> tuple[Finding, ...]:
    """Give a reviewer's finding the earlier id it says it repeats, when that
    id was an open finding of the same category from the same reviewer.
    Rewording a finding then doesn't count as fixing it. Any other claimed id
    is ignored and the finding keeps the id of what it says."""
    earlier = {f.id: f for f in previous if not f.resolved}
    out = []
    for f in findings:
        old = earlier.get(f.claimed_id or "")
        if (
            old is not None
            and old.category == f.category
            and old.source.lower() == f.source.lower()
            and f.source != "verifier"
        ):
            f = replace(f, id=old.id)
        out.append(f)
    return _dedupe(out)


def _dedupe(items: Iterable[Finding]) -> tuple[Finding, ...]:
    seen: dict[str, Finding] = {}
    for f in items:
        seen.setdefault(f.id, f)
    return tuple(seen.values())


def carry_forward(
    previous: Iterable[Finding], current: Iterable[Finding], *, evaluated: bool = True
) -> tuple[Finding, ...]:
    """The current findings, plus the earlier open ones the current findings
    don't repeat.

    ``evaluated`` says the current findings come from a full check of the new
    revision (a review was read). Only then is an earlier finding the new
    revision no longer raises marked resolved; it is reported resolved once
    and does not keep coming back. Otherwise (CI failed or is still running,
    no review yet) nothing was re-checked, so earlier findings stay open.
    """
    current = tuple(current)
    now_ids = {f.id for f in current}
    left = tuple(f for f in previous if f.id not in now_ids and not f.resolved)
    if evaluated:
        left = tuple(replace(f, resolved=True) for f in left)
    return current + _dedupe(left)


def blocking(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    return tuple(f for f in findings if not f.resolved and f.severity is Severity.BLOCKING)


def for_route(findings: Iterable[Finding], route: Route) -> tuple[Finding, ...]:
    return tuple(f for f in findings if not f.resolved and f.route is route)


__all__ = [
    "REVIEWER_CATEGORIES",
    "Finding",
    "adopt_ids",
    "finding_from_data",
    "Route",
    "Severity",
    "blocking",
    "carry_forward",
    "finding_id",
    "for_route",
    "from_reports",
    "from_reviewer",
]
