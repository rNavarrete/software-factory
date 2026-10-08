"""The bridge from the independent review (ENG-156) to the repair step.

``ReviewFailures(reviewer).failure(attempt)`` reads the review's latest
recorded verdict for the attempt (``reviewer.evidence``, ledger only, no
GitHub reads: the service already runs the review's ``check`` every round)
and turns a final "failed" verdict into a ``FailureReport``. Every other
verdict (passed, waiting for CI, running, unknown, blocked, not reviewable,
needs Rolando) is None: nothing for an automatic repair to act on.

The review module isn't imported: the verdict is read by its documented
fields, so this works the same against the review and against a test double.
"""

from __future__ import annotations

from controller.interfaces import AttemptId
from controller.repair.findings import RepairFinding
from controller.service.seams import FailureReport

_FAILED = "failed"


class ReviewFailures:
    def __init__(self, reviewer: object) -> None:
        self._reviewer = reviewer

    def failure(self, attempt: AttemptId) -> FailureReport | None:
        evidence = getattr(self._reviewer, "evidence", None)
        if evidence is None:
            return None
        status = evidence(attempt)
        if status is None or getattr(status.state, "value", None) != _FAILED:
            return None
        if status.attempt != attempt:
            return None
        head = getattr(status.revision, "head", "")
        if not head or not status.pr:
            return None
        findings = tuple(_finding(f) for f in status.findings if not getattr(f, "resolved", False))
        return FailureReport(
            attempt, int(status.pr), str(head), findings, str(status.key or "review")
        )


def _severity(value: object) -> object:
    return getattr(value, "value", value)


def _finding(f: object) -> RepairFinding:
    """One review finding as the repair sees it. One it can't read is kept as
    a finding only Rolando can settle, so it can never be skipped over."""
    try:
        d = f.as_data()  # type: ignore[attr-defined]
        route = d.get("route")
        return RepairFinding(
            id=str(d["id"]),
            category=str(d["category"]),
            summary=str(d["summary"]),
            evidence=str(d["evidence"]),
            suggested_action=str(d["suggested_action"]),
            route=route if route in ("repair", "rolando") else "rolando",
            # Only a finding the review marks advisory is left out; a
            # missing or unexpected severity counts as blocking.
            blocking=_severity(d.get("severity")) != "advisory",
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return RepairFinding(
            id="unreadable-finding",
            category="integrity",
            summary="The factory could not read one of the review's findings",
            evidence="the review's verdict as recorded",
            suggested_action="Look at the review on the PR.",
            route="rolando",
        )


__all__ = ["ReviewFailures"]
