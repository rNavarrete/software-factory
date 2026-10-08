"""Ledger records of the automatic independent review (ENG-156), and the view
rebuilt from them.

Everything the reviewer knows comes back from the ledger on every call, so a
restart loses nothing and two reviewers sharing one ledger agree:

- ``review-watch``: the service asked for the review of an attempt's PR.
- ``review-job-intent``: a review job for one exact revision is about to be
  launched. Written, under the ledger's writer lock, before the launch: it is
  the claim that stops a second launch for the same request key, and it counts
  against the weekly fire allowance whether or not the launch works.
- ``review-job-launched``: what the launch returned (launched, not-launched or
  launch-outcome-unknown, decided by HTTP status alone, as for workers).
- ``review-verdict``: the verdict for one exact revision, with its findings.
  A new one is written only when the verdict or its evidence changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from controller.interfaces import AttemptId, LaunchOutcome, LedgerEvent, StoredEvent, TaskId

REVIEW_WATCH = "review-watch"
REVIEW_JOB_INTENT = "review-job-intent"
REVIEW_JOB_LAUNCHED = "review-job-launched"
REVIEW_VERDICT = "review-verdict"
KINDS = frozenset({REVIEW_WATCH, REVIEW_JOB_INTENT, REVIEW_JOB_LAUNCHED, REVIEW_VERDICT})

PASS_FULL = "full"
PASS_VERIFY = "verify"


@dataclass(frozen=True)
class Job:
    key: str
    attempt: AttemptId
    cycle: str
    pass_kind: str
    number: int
    """This job's pass number in its review cycle, from 1."""
    pr: int
    head: str
    base: str
    claims: tuple[datetime, ...]
    """When each launch was claimed (an intent written)."""
    outcomes: tuple[tuple[str, datetime, str], ...] = ()
    """(outcome, when, detail) for each launch whose answer was recorded."""

    @property
    def unanswered(self) -> bool:
        """A launch was claimed and nothing about it was recorded: the process
        died mid-launch, or a launch is in flight right now."""
        return len(self.claims) > len(self.outcomes)

    @property
    def outcome(self) -> str | None:
        return self.outcomes[-1][0] if self.outcomes else None

    @property
    def launched_at(self) -> datetime | None:
        for outcome, at, _ in self.outcomes:
            if outcome == LaunchOutcome.LAUNCHED.value:
                return at
        return None


@dataclass(frozen=True)
class VerdictRecord:
    key: str
    attempt: AttemptId
    cycle: str
    verdict: str
    head: str
    base: str
    merge_base: str
    digest: str
    pr: int
    review_url: str | None
    findings: tuple[Mapping[str, object], ...]
    at: datetime
    seq: int


@dataclass
class ReviewView:
    watches: dict[AttemptId, tuple[int, str]] = field(default_factory=dict)
    """Attempt -> (PR number, the service's request key)."""
    jobs: dict[str, Job] = field(default_factory=dict)
    intents_at: list[datetime] = field(default_factory=list)
    """Every review launch claimed, for the weekly fire allowance."""
    verdicts: dict[str, VerdictRecord] = field(default_factory=dict)
    """Latest verdict per request key."""
    by_cycle: dict[str, list[VerdictRecord]] = field(default_factory=dict)
    """Every verdict in each review cycle, in ledger order."""

    @classmethod
    def build(cls, stored: Sequence[StoredEvent]) -> ReviewView:
        view = cls()
        for item in sorted(stored, key=lambda s: s.seq):
            if item.event.kind not in KINDS:
                continue
            try:
                view._apply(item)
            except (KeyError, TypeError, ValueError):
                continue  # a malformed record grants nothing
        return view

    def _apply(self, item: StoredEvent) -> None:
        e, d = item.event, item.event.data
        if e.attempt is None:
            return
        if e.kind == REVIEW_WATCH:
            self.watches.setdefault(e.attempt, (int(d["pr"]), str(d["request"])))
        elif e.kind == REVIEW_JOB_INTENT:
            key = str(d["key"])
            self.intents_at.append(e.at)
            old = self.jobs.get(key)
            if old is None:
                self.jobs[key] = Job(
                    key,
                    e.attempt,
                    str(d["cycle"]),
                    str(d["pass"]),
                    int(d["number"]),
                    int(d["pr"]),
                    str(d["head"]),
                    str(d["base"]),
                    (e.at,),
                )
            else:
                self.jobs[key] = replace(old, claims=old.claims + (e.at,))
        elif e.kind == REVIEW_JOB_LAUNCHED:
            key = str(d["key"])
            job = self.jobs[key]
            if not job.unanswered:
                return  # an answer with no claim waiting for it grants nothing
            outcome = LaunchOutcome(str(d["outcome"])).value
            self.jobs[key] = replace(
                job, outcomes=job.outcomes + ((outcome, e.at, str(d.get("detail") or "")),)
            )
        elif e.kind == REVIEW_VERDICT:
            record = VerdictRecord(
                key=str(d["key"]),
                attempt=e.attempt,
                cycle=str(d["cycle"]),
                verdict=str(d["verdict"]),
                head=str(d["head"]),
                base=str(d["base"]),
                merge_base=str(d["merge_base"]),
                digest=str(d["digest"]),
                pr=int(d["pr"]),
                review_url=d.get("review_url") or None,
                findings=tuple(d.get("findings") or ()),
                at=e.at,
                seq=item.seq,
            )
            self.verdicts[record.key] = record
            self.by_cycle.setdefault(record.cycle, []).append(record)

    def cycle_jobs(self, cycle: str) -> list[Job]:
        return sorted((j for j in self.jobs.values() if j.cycle == cycle), key=lambda j: j.number)


def watch(attempt: AttemptId, pr: int, request: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        REVIEW_WATCH, now, attempt.task, attempt, data={"pr": pr, "request": request}
    )


def intent(
    attempt: AttemptId,
    *,
    key: str,
    cycle: str,
    pass_kind: str,
    number: int,
    pr: int,
    head: str,
    base: str,
    merge_base: str,
    digest: str,
    now: datetime,
) -> LedgerEvent:
    return LedgerEvent(
        REVIEW_JOB_INTENT,
        now,
        attempt.task,
        attempt,
        data={
            "key": key,
            "cycle": cycle,
            "pass": pass_kind,
            "number": number,
            "pr": pr,
            "head": head,
            "base": base,
            "merge_base": merge_base,
            "digest": digest,
        },
    )


def launched(
    attempt: AttemptId,
    key: str,
    outcome: LaunchOutcome,
    now: datetime,
    *,
    http_status: int | None = None,
    session_url: str | None = None,
    detail: str = "",
) -> LedgerEvent:
    return LedgerEvent(
        REVIEW_JOB_LAUNCHED,
        now,
        attempt.task,
        attempt,
        data={
            "key": key,
            "outcome": outcome.value,
            "http_status": http_status,
            "session_url": session_url,
            "detail": detail[:500],
        },
    )


def verdict(
    attempt: AttemptId,
    *,
    key: str,
    cycle: str,
    value: str,
    head: str,
    base: str,
    merge_base: str,
    digest: str,
    pr: int,
    review_url: str | None,
    findings: Sequence[Mapping[str, object]],
    now: datetime,
) -> LedgerEvent:
    return LedgerEvent(
        REVIEW_VERDICT,
        now,
        attempt.task,
        attempt,
        data={
            "key": key,
            "cycle": cycle,
            "verdict": value,
            "head": head,
            "base": base,
            "merge_base": merge_base,
            "digest": digest,
            "pr": pr,
            "review_url": review_url,
            "findings": [dict(f) for f in findings],
        },
    )


def cycle_of(task: TaskId, digest: str) -> str:
    """One review cycle per task and approved contract."""
    return f"{task}:{digest}"


__all__ = [
    "KINDS",
    "PASS_FULL",
    "PASS_VERIFY",
    "REVIEW_JOB_INTENT",
    "REVIEW_JOB_LAUNCHED",
    "REVIEW_VERDICT",
    "REVIEW_WATCH",
    "Job",
    "ReviewView",
    "VerdictRecord",
    "cycle_of",
    "intent",
    "launched",
    "verdict",
    "watch",
]
