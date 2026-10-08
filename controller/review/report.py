"""What the service tells Rolando on the Linear ticket about the review (ENG-156).

``review_messages`` turns one ``ReviewStatus`` into the ticket comments it
calls for, each with an outbox key. The service posts each key once, so the
same verdict on the same revision is never reported twice, and a new revision
or a new verdict is. Built on ENG-178's message texts (controller/report).

- running: once per revision, that the review has started (with its link).
- passed: "Ready for your review", naming the exact commit, and saying when
  it is a corrected version that passed its verification pass.
- failed: what a correction has to fix. It never says a correction is
  running: that is said only when one actually starts (ENG-160).
- needs Rolando: one observation request per behavior only he can check
  (his ``Observation:`` reply is bound to that commit), and one note listing
  anything else only he can settle.
- not reviewable, blocked, unknown: one plain notice each.
- waiting for CI: explain that this revision is not ready and earlier readiness
  does not apply, including when a new push follows a passing review.

A message is a report, never an approval.
"""

from __future__ import annotations

import hashlib

from controller.report import messages as m
from controller.report.messages import Stage
from controller.review.reviewer import ReviewState, ReviewStatus
from controller.review.workflow import IDENTITY
from verify.findings import Route, Severity


def review_messages(status: ReviewStatus, pr_url: str = "") -> list[tuple[str, str]]:
    """(outbox key, message) pairs for this status; empty when there is nothing new."""
    s, n = status.state, status.pr
    commit = status.revision.head if status.revision else ""
    base = f"review:{status.attempt}:{status.key or 'none'}"
    open_ = [f for f in status.findings if not f.resolved and f.severity is Severity.BLOCKING]

    def lines(route: Route) -> str:
        items = [f for f in open_ if f.route is route]
        return " ".join(f"({i}) {f.summary}" for i, f in enumerate(items, 1))

    codex = status.reviewer == IDENTITY
    who = "Codex review" if codex else "Independent review"
    name = "Codex review" if codex else "independent review"
    verified = status.pass_kind == "verify"
    if s is ReviewState.WAITING_CI:
        text = (
            f"PR #{n} at `{m.short_sha(commit)}` is not ready for your review."
            " CI checks are pending for the current revision. Any earlier ready report"
            " does not apply; the factory will report again when the checks and review pass."
        )
        return [(f"{base}:waiting-ci", m.progress(Stage.REVIEWING, text, pr_url=pr_url))]
    if s is ReviewState.RUNNING and status.pass_kind:
        text = (
            f"PR #{n} at `{m.short_sha(commit)}` is being checked; the {name} is running"
            f" ({'verification pass on the corrected version' if verified else 'full review'})."
        )
        return [(f"{base}:running", m.progress(Stage.REVIEWING, text, pr_url=pr_url))]
    if s is ReviewState.PASSED:
        advisory = [f.summary for f in status.findings if not f.resolved and f not in open_]
        fixed = [f for f in status.findings if f.resolved]
        checks = [m.Check(who, "passed, verification pass" if verified else "passed")]
        if status.review_url:
            checks.append(m.Check("Review evidence", status.review_url))
        changed = (
            f"The corrected version in PR #{n}; {len(fixed)} earlier finding(s) checked as fixed."
            if verified
            else f"The worker's change in PR #{n}."
        )
        r = m.Readiness(
            pr_url=pr_url,
            commit=commit,
            changed=changed,
            checks=checks,
            limitations=advisory,
        )
        return [(f"ready:{status.attempt}:{status.key}", m.ready(r))]
    if s is ReviewState.FAILED:
        count = len([f for f in open_ if f.route is Route.REPAIR])
        text = (
            f"The {name} of PR #{n} at `{m.short_sha(commit)}` found {count} problem(s)"
            f" for a correction: {lines(Route.REPAIR) or status.note} A correction can start"
            " only within the task's approved allowance, and only after the previous worker is"
            " confirmed finished; you'll get a separate note when one actually starts."
            " Nothing is needed from you for these."
        )
        return [(f"{base}:failed", m.progress(Stage.FAILED, text, pr_url=pr_url))]
    if s is ReviewState.NEEDS_ROLANDO:
        out = []
        observe = [f for f in open_ if f.category == "needs-observation"]
        for f in observe:
            out.append(
                (
                    f"observe:{status.attempt}:{commit}:{f.criterion or f.id}",
                    m.observation_request(commit, f.summary),
                )
            )
        rest = [f for f in open_ if f.route is Route.ROLANDO and f not in observe]
        if rest:
            items = " ".join(f"({i}) {f.summary}" for i, f in enumerate(rest, 1))
            text = (
                f"The independent review of PR #{n} at `{m.short_sha(commit)}` found things only"
                f" you can settle: {items} Each needs your own recorded decision; a reply here"
                " doesn't approve or clear anything."
            )
            out.append(
                (f"{base}:needs-rolando", m.progress(Stage.NEEDS_DECISION, text, pr_url=pr_url))
            )
        return out
    if s is ReviewState.NOT_REVIEWABLE:
        text = f"{status.note} No review job was started."
        return [(f"{base}:not-reviewable", m.progress(Stage.NOTICE, text, pr_url=pr_url))]
    if s is ReviewState.BLOCKED:
        reason = "; ".join(status.blocks) or status.note
        return [
            (
                f"{base}:blocked:{hashlib.sha256(reason.encode()).hexdigest()[:12]}",
                m.progress(
                    Stage.WAITING,
                    f"The independent review of PR #{n} can't start yet.",
                    wait_reason=reason,
                    pr_url=pr_url,
                ),
            )
        ]
    if s is ReviewState.UNKNOWN:
        return [(f"{base}:unknown", m.progress(Stage.UNCLEAR, status.note, pr_url=pr_url))]
    return []


__all__ = ["review_messages"]
