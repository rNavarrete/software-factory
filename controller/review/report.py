"""What the service tells Rolando on the Linear ticket about the review (ENG-156).

``review_messages`` turns one ``ReviewStatus`` into the ticket comments it
calls for, each with an outbox key. The service posts each key once, so the
same verdict on the same revision is never reported twice, and a new revision
or a new verdict is. Built on ENG-178's message texts (controller/report).

- passed: "Ready for your review", naming the exact commit.
- failed: what the repair step has to fix; nothing is asked of Rolando.
- needs Rolando: one observation request per behavior only he can check
  (his ``Observation:`` reply is bound to that commit), and one note listing
  anything else only he can settle.
- not reviewable, blocked, unknown: one plain notice each.
- waiting for CI, running: nothing; the earlier "Reviewing" entry stands.

A message is a report, never an approval.
"""

from __future__ import annotations

import hashlib

from controller.report import messages as m
from controller.report.messages import Stage
from controller.review.reviewer import ReviewState, ReviewStatus
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

    if s is ReviewState.PASSED:
        advisory = [f.summary for f in status.findings if not f.resolved and f not in open_]
        checks = [m.Check("Independent review", "passed")]
        if status.review_url:
            checks.append(m.Check("Review comment", status.review_url))
        r = m.Readiness(
            pr_url=pr_url,
            commit=commit,
            changed=f"The worker's change in PR #{n}.",
            checks=checks,
            limitations=advisory,
        )
        return [(f"ready:{status.attempt}:{commit}", m.ready(r))]
    if s is ReviewState.FAILED:
        text = (
            f"The independent review of PR #{n} at `{m.short_sha(commit)}` found problems for"
            f" the repair step to fix: {lines(Route.REPAIR) or status.note} Nothing is needed"
            " from you for these."
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
