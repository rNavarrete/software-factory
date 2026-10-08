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
    r"coverage|verification)\b",
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
    """Automatic repairs already used, before this one."""

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
    automatic_used: int,
) -> Plan | Stop:
    """``attempts_used``: attempts reserved for the task so far.
    ``automatic_used``: how many of those were automatic repairs."""
    open_ = [f for f in findings if f.blocking]
    if not open_:
        return Stop(
            "nothing-to-repair",
            "the review reported no open blocking problem for a repair to fix",
            "Look at the PR yourself.",
        )
    rolando = [f for f in open_ if f.route != "repair"]
    if rolando:
        return Stop(
            "needs-rolando",
            "the review raised something only you can settle: "
            + "; ".join(f.summary for f in rolando[:3]),
            "Settle it on the PR, then repair it by hand or close the attempt.",
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
    if len(open_) > MAX_FINDINGS:
        return Stop(
            "too-many-findings",
            f"the review found {len(open_)} problems, more than a routine repair covers",
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
    if automatic_used >= allowance:
        return Stop(
            "allowance-used",
            f"the factory has used its {allowance} automatic repair"
            f"{'' if allowance == 1 else 's'} for this ticket",
            f"Allow attempt {nxt} by hand to have the factory try again, or close the attempt.",
        )
    return Plan(nxt, tuple(open_), allowance, automatic_used)


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
    return bool(_WEAKEN_RE.search(f"{f.summary} {f.suggested_action}"))


__all__ = [
    "ROUTINE_CATEGORIES",
    "Plan",
    "Stop",
    "asks_to_weaken",
    "plan",
    "routine",
]
