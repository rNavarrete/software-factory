"""Whether a failed attempt may be repaired without asking Rolando (ENG-160).

Pure: the service passes what the review found and where the task stands,
and gets back either a ``Plan`` for the next attempt or a ``Stop`` that says,
in plain words, why Rolando decides instead.

The rules, from the ticket's adopted policy:

- Only concrete, independently evidenced, blocking problems the review routed
  to repair, in a category known to be a routine technical fix. Anything the
  review routed to Rolando, or a category this module doesn't know, stops the
  automatic repair, even if other findings are routine.
- The finding's text is read for what it asks, not for its severity label: a
  finding asking to delete, skip, disable or weaken a test, a check, a
  criterion or a workflow is never routine.
- The next attempt must fit the contract's budget, the project's limit and
  the repair allowance the Todo move came with. At the limit the factory
  stops with one report; it never loops.

Whether the Todo move still stands, whether the earlier worker has stopped,
and the fire limits are checked elsewhere (the signer, recovery and the
attempt gate). Nothing here authorizes anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from controller.repair.findings import MAX_FINDINGS, RepairFinding

MAX_DETAIL_CHARS = 24_000
"""The most finding text one repair carries, so the worker's task message
always fits (``MAX_FIRE_TEXT_CHARS``) beside the contract."""

ROUTINE_CATEGORIES = frozenset(
    {
        "markers",
        "base",
        "scope",
        "checks-failed",
        "criterion-failed",
        "criterion-unknown",
        "review-code",
        "review-test",
        "review-scope",
    }
)
"""Review categories (verify/findings.py) a repair worker may fix on its own,
plus every ``assertion-*`` and the test-quality ``flag-*`` kinds below."""
ROUTINE_PREFIXES = ("assertion-",)
ROUTINE_FLAGS = frozenset(
    {
        "flag-fixture-only",
        "flag-weak-assertion",
        "flag-mirrors-code",
        "flag-may-not-run",
        "flag-no-failure-proof",
        "flag-passes-without-change",
    }
)
"""Test-quality flags: the fix is a stronger test. Flags about protected
controls (a changed workflow, a deleted, weakened or changed existing test,
setup or a suppression) are Rolando's."""

_WEAKEN_RE = re.compile(
    r"\b(delet\w*|remov\w*|skip\w*|disabl\w*|weaken\w*|loosen\w*|relax\w*|xfail\w*|"
    r"comment\w*\s+out|turn\w*\s+off|ignor\w*|suppress\w*|lower\w*|drop\w*)\b"
    r"[^.]{0,60}?\b(tests?|assert\w*|checks?|criteri\w*|workflows?|ci|lint\w*|typecheck\w*|"
    r"coverage|verification)\b"
    # Changing what a test expects, rather than the code under test.
    r"|\b(chang\w*|updat\w*|rewrit\w*|edit\w*|modif\w*|adjust\w*|alter\w*|fix\w*)\b"
    r"[^.]{0,60}?\b(expect\w*|snapshots?|assert\w*|test\w*\s+(data|values?|output))\b"
    r"|\bmark\w*\b[^.]{0,60}?\b(todo|skip\w*|xfail|pending|flaky|only|known)\b"
    r"|\bno\s+longer\b[^.]{0,30}?\b(check|assert|test|verif|cover|fail)\w*"
    r"|\b(it|test|describe)\.(skip|todo|only)\b|\bx(it|describe)\s*\("
    r"|\b(less\s+strict|accept\s+any|any\s+value|tolerance)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Plan:
    """Start ``attempt`` (the next one) to fix ``findings`` found on
    ``prior_head`` of ``prior_pr``."""

    attempt: int
    findings: tuple[RepairFinding, ...]
    allowance: int
    """The repair allowance the Todo move came with."""
    used: int
    """Repairs (typed or automatic) already used, before this one."""

    @property
    def failure(self) -> str:
        """One line for the ledger and the escalation report."""
        names = "; ".join(f.summary for f in self.findings[:3])
        more = f" (and {len(self.findings) - 3} more)" if len(self.findings) > 3 else ""
        return f"{names}{more}"[:600]


@dataclass(frozen=True)
class Stop:
    """No automatic repair. ``code`` is for the record; ``reason`` is the
    plain sentence Rolando reads; ``decision`` is what he can do."""

    code: str
    reason: str
    decision: str
    capped: bool = False
    """The task has no attempt left at all: only a new ticket goes on."""


def plan(
    findings: Sequence[RepairFinding],
    *,
    attempts_used: int,
    contract_budget: int,
    project_max: int,
    allowance: int,
    repairs_used: int,
) -> Plan | Stop:
    """``attempts_used``: attempts reserved for the task so far.
    ``repairs_used``: how many of those were repairs, typed or automatic.
    Every attempt after the first counts against the allowance, as the
    signer and the approval check count it."""
    open_ = [f for f in findings if f.blocking]
    # Anything routed to Rolando stops it, blocking or not.
    rolando = [f for f in findings if f.route != "repair"]
    if rolando:
        return Stop(
            "needs-rolando",
            "the review raised something only you can settle: "
            + "; ".join(f.summary for f in rolando[:3]),
            "Settle it on the PR, then repair it by hand or close the attempt.",
        )
    if not open_:
        return Stop(
            "nothing-to-repair",
            "the review reported no open blocking problem for a repair to fix",
            "Look at the PR yourself.",
        )
    unknown = [f for f in open_ if not routine(f.category)]
    if unknown:
        kinds = ", ".join(sorted({f.category for f in unknown}))
        return Stop(
            "not-routine",
            f"some problems are not routine fixes the factory may make on its own ({kinds})",
            "Look at the PR; repair it by hand or close the attempt.",
        )
    weakening = [f for f in open_ if asks_to_weaken(f)]
    if weakening:
        return Stop(
            "asks-to-weaken",
            "a finding asks to remove or weaken a test or check, which is never a routine fix: "
            + weakening[0].summary,
            "Decide on the PR whether that change is right.",
        )
    if len(open_) > MAX_FINDINGS or detail_chars(open_) > MAX_DETAIL_CHARS:
        return Stop(
            "too-many-findings",
            f"the review found {len(open_)} problems, or more detail than a routine repair covers",
            "Look at the PR; repair it by hand or close the attempt.",
        )
    nxt = attempts_used + 1
    limit = min(contract_budget, project_max)
    if nxt > limit:
        return Stop(
            "capped",
            f"the task has used all {limit} of its attempts",
            "Merge the PR as it is after checking it yourself, fix it by hand, or close it."
            " Further work needs a new ticket.",
            capped=True,
        )
    if allowance <= 0:
        return Stop(
            "no-allowance",
            "this project's Todo moves come with no automatic repairs",
            f"Allow attempt {nxt} by hand to have the factory try again, or close the attempt.",
        )
    if repairs_used >= allowance:
        return Stop(
            "allowance-used",
            f"this ticket has used the {allowance} repair"
            f"{'' if allowance == 1 else 's'} its Todo move allows",
            f"Allow attempt {nxt} by hand to have the factory try again, or close the attempt.",
        )
    return Plan(nxt, tuple(open_), allowance, repairs_used)


def detail_chars(findings: Sequence[RepairFinding]) -> int:
    return sum(len(f.summary) + len(f.evidence) + len(f.suggested_action) for f in findings)


def routine(category: str) -> bool:
    return (
        category in ROUTINE_CATEGORIES
        or category.startswith(ROUTINE_PREFIXES)
        or category in ROUTINE_FLAGS
    )


def asks_to_weaken(f: RepairFinding) -> bool:
    """True if the finding's own words ask to remove or loosen a test, check,
    criterion or workflow. Errs towards yes: a false alarm only sends the
    repair to Rolando."""
    return bool(_WEAKEN_RE.search(f"{f.summary}. {f.suggested_action}"))


__all__ = [
    "MAX_DETAIL_CHARS",
    "ROUTINE_CATEGORIES",
    "Plan",
    "Stop",
    "asks_to_weaken",
    "plan",
    "routine",
]
