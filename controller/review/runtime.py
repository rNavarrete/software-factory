"""Starting one review job (ENG-156).

A review job reviews one exact revision of one worker PR. It runs as one
dispatch of the factory's protected Codex review workflow
(``controller.review.workflow.WorkflowDispatchRuntime``); the result is read
back from that run, never from a PR comment.

``ReviewRuntime`` is how the reviewer starts a job. Any runtime keeps the same
rules: one launch per call, never a retry, and an answer of launched,
not-launched or launch-outcome-unknown. A future adapter from the worker pool
(ENG-154) can stand in on the same terms.

The job's request travels as a JSON envelope (``review_text``) holding the
contract, the exact revision, the trusted CI results and, on a verification
pass, the findings it must check. The workflow checks the envelope again
before doing anything (``controller.review.codex_job``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from controller.contract import canonical_bytes
from controller.interfaces import MAX_FIRE_TEXT_CHARS, LaunchResult

ENVELOPE = "factory-review-job/v1"


@dataclass(frozen=True)
class ReviewJob:
    key: str
    pass_kind: str
    repository: str
    pr: int
    pr_url: str
    contract_digest: str
    head: str
    base: str
    merge_base: str
    reviewer: str
    """The GitHub login the job must post as; anything else is not read."""
    contract: Mapping[str, object]
    previous_findings: Sequence[Mapping[str, object]] = ()
    ci: Sequence[Mapping[str, object]] = ()
    """The trusted CI run's results for this head (command, exit code, link)."""


class ReviewTooLarge(ValueError):
    """The envelope would not fit in one fire. Nothing was sent."""


def review_text(job: ReviewJob, limit: int = MAX_FIRE_TEXT_CHARS) -> str:
    """The fire text for ``job``. Raises ReviewTooLarge if it can't fit in
    ``limit`` characters, after dropping the previous findings' long fields."""
    body = {
        "envelope": ENVELOPE,
        "key": job.key,
        "pass": job.pass_kind,
        "repository": job.repository,
        "pr": job.pr,
        "pr_url": job.pr_url,
        "contract_digest": job.contract_digest,
        "head": job.head,
        "base": job.base,
        "merge_base": job.merge_base,
        "reviewer": job.reviewer,
        "contract": json.loads(canonical_bytes(job.contract)),
        "previous_findings": [dict(f) for f in job.previous_findings],
    }
    if job.ci:
        body["ci"] = [dict(c) for c in job.ci]
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        body["previous_findings"] = [
            {k: f.get(k) for k in ("id", "category", "criterion", "summary")}
            for f in job.previous_findings
        ]
        text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        raise ReviewTooLarge(f"review job text is {len(text)} chars, limit {limit}")
    return text


@runtime_checkable
class ReviewRuntime(Protocol):
    def launch(self, text: str) -> LaunchResult:
        """One launch, never retried. Network failures come back as
        launch-outcome-unknown, never as an exception. May raise before sending
        anything (a missing start key, a refused payload): then nothing started."""
        ...


__all__ = [
    "ENVELOPE",
    "ReviewJob",
    "ReviewRuntime",
    "ReviewTooLarge",
    "review_text",
]
