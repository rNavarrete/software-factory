"""Seeded attempts to get past the factory's verification and approval (ENG-158).

``python -m redteam`` runs every case and prints the results as Markdown. It
exits 0 only when every offline case is blocked; live cases that have not run
yet are listed but do not change the exit code.
"""

from redteam.cases import (
    CASES,
    LIVE_CASES,
    Case,
    CaseResult,
    Group,
    Observed,
    Result,
    every_case,
    holds_qualification,
    render,
    run_all,
    run_case,
)

__all__ = [
    "CASES",
    "LIVE_CASES",
    "Case",
    "CaseResult",
    "Group",
    "Observed",
    "Result",
    "every_case",
    "holds_qualification",
    "render",
    "run_all",
    "run_case",
]
