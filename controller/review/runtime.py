"""Starting one review job (ENG-156).

A review job is a fresh, scoped session that reviews one exact revision of one
worker PR and posts its result as a single comment on the PR, as the reviewer's
own GitHub account. It has no write access to the repository: on a public repo
any account can comment, so the reviewer account needs no permission at all,
and so it can't push, approve, merge or release (docs/review.md has the setup).

``ReviewRuntime`` is how the reviewer starts a job. ``RoutineReviewRuntime``
fires the reviewer's own cloud routine (a separate routine and start key from
the worker's, ideally on a separate account). A future adapter from the worker
pool (ENG-154) can stand in, as long as it keeps the same rules: one launch per
call, never a retry, and an answer of launched, not-launched or
launch-outcome-unknown decided by HTTP status alone.

The job's instructions travel as a JSON envelope (``review_text``) holding the
contract, the exact revision and, on a verification pass, the findings it must
check. The routine's saved prompt (reviewer_prompt.md) checks the envelope
again before doing anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from controller.adapter.routine import RoutineAdapter
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


class ReviewTooLarge(ValueError):
    """The envelope would not fit in one fire. Nothing was sent."""


def review_text(job: ReviewJob) -> str:
    """The fire text for ``job``. Raises ReviewTooLarge if it can't fit, after
    dropping the previous findings' long fields first."""
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
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_FIRE_TEXT_CHARS:
        body["previous_findings"] = [
            {k: f.get(k) for k in ("id", "category", "criterion", "summary")}
            for f in job.previous_findings
        ]
        text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_FIRE_TEXT_CHARS:
        raise ReviewTooLarge(f"review job text is {len(text)} chars, limit {MAX_FIRE_TEXT_CHARS}")
    return text


@runtime_checkable
class ReviewRuntime(Protocol):
    def launch(self, text: str) -> LaunchResult:
        """One launch, never retried. Network failures come back as
        launch-outcome-unknown, never as an exception. May raise before sending
        anything (a missing start key, a refused payload): then nothing started."""
        ...


class RoutineReviewRuntime:
    """The reviewer's cloud routine, fired the same way as the worker's.

    Uses the routine adapter's single POST (``RoutineAdapter._post``) without
    its worker-envelope check, which describes a worker run, not a review.
    The review envelope is checked here instead.
    """

    def __init__(self, adapter: RoutineAdapter) -> None:
        self._adapter = adapter

    @property
    def routine(self) -> str:
        return self._adapter.trig_id

    def launch(self, text: str) -> LaunchResult:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("envelope") != ENVELOPE:
            raise ValueError("not a review job envelope")
        if len(text) > MAX_FIRE_TEXT_CHARS:
            raise ReviewTooLarge("review job text is too long")
        return self._adapter._post(text)


__all__ = [
    "ENVELOPE",
    "ReviewJob",
    "ReviewRuntime",
    "ReviewTooLarge",
    "RoutineReviewRuntime",
    "review_text",
]
