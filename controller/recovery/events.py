"""Ledger event kinds recovery writes and reads.

The gate's kinds (controller/attempts/events.py) and the ledger's own kinds
(controller/ledger/kinds.py) cover reservations, fires, clearings, candidates,
checks and abandoned attempts. These add what reconciling an interrupted run
needs on top. They grant nothing: none of them clears an attempt or allows a
dispatch. Only a signed clearing record does that (ADR 0002 section 6.1).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from controller.interfaces import LaunchResult, LedgerEvent, RunId, TaskId

LAUNCH_RECONCILED = "launch-reconciled"
"""What Rolando found when he looked for the session of a launch whose outcome
is unknown (ADR 0002 section 6): the session, several sessions, or nothing."""
LATE_FIRE_RESULT = "late-fire-result"
"""A fire response that arrived after recovery had already recorded the fire
as launch-outcome-unknown (say, the Mac slept mid-call). Kept for its session
URL. The gate's own fire result is never replaced."""
PR_OBSERVED = "pr-observed"
"""A pull request read from GitHub for an attempt, written when it first
appears and whenever its state, draft flag or head commit changes."""
RELEASE_STATUS = "release-status"
"""Whether a merged task was released. Kept apart from the task's state: a
merge is not a release and a release says nothing about the work."""

RELEASE_STATUSES = ("released", "release-failed")

SESSION_URL_RE = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]+(?![A-Za-z0-9_/.-])")
"""A cloud session URL. The lookahead stops ``.../cse_1x`` or ``.../cse_1/x``
from counting as ``.../cse_1``."""


class Finding(Enum):
    """What Rolando found when he reconciled an unknown launch."""

    SESSION_FOUND = "session-found"
    """One session in the routine's run list, its URL recorded."""
    DUPLICATES = "duplicates"
    """More than one session for the one fire; every URL recorded."""
    NOT_FOUND = "not-found"
    """No session seen. Never proof that none started (ADR 0002 section 6.1):
    the attempt stays unresolved."""


def session_url(value: str) -> str:
    """``value`` if it is exactly one cloud session URL, else ValueError."""
    if not isinstance(value, str) or SESSION_URL_RE.fullmatch(value) is None:
        raise ValueError(f"not a session URL: {value!r}")
    return value


def listed_urls(evidence: str) -> tuple[str, ...]:
    """The session URLs a completed or terminated clearing vouches for: the
    whitespace-separated URLs at the start of its evidence, up to the first
    word that is not one. A URL mentioned later, in a note ("have not checked
    https://..."), is not vouched for."""
    out = []
    for word in evidence.split():
        if SESSION_URL_RE.fullmatch(word) is None:
            break
        out.append(word)
    return tuple(out)


def launch_reconciled(
    run: RunId,
    finding: Finding,
    session_urls: tuple[str, ...],
    how_checked: str,
    by: str,
    at: datetime,
) -> LedgerEvent:
    urls = tuple(session_url(u) for u in session_urls)
    if len(set(urls)) != len(urls):
        raise ValueError("a session URL is listed twice")
    wanted = {Finding.SESSION_FOUND: "exactly one", Finding.DUPLICATES: "two or more"}
    counts_ok = {
        Finding.SESSION_FOUND: len(urls) == 1,
        Finding.DUPLICATES: len(urls) >= 2,
        Finding.NOT_FOUND: not urls,
    }
    if not counts_ok[finding]:
        raise ValueError(f"{finding.value} needs {wanted.get(finding, 'no')} session URL(s)")
    if not how_checked.strip() or not by.strip():
        raise ValueError("a reconciliation says how it was checked and who checked")
    return LedgerEvent(
        LAUNCH_RECONCILED,
        at,
        run.attempt.task,
        run.attempt,
        run,
        {
            "finding": finding.value,
            "session_urls": list(urls),
            "how_checked": how_checked,
            "by": by,
        },
    )


def late_fire_result(run: RunId, result: LaunchResult, at: datetime) -> LedgerEvent:
    return LedgerEvent(
        LATE_FIRE_RESULT,
        at,
        run.attempt.task,
        run.attempt,
        run,
        {
            "outcome": result.outcome.value,
            "http_status": result.http_status,
            "session_id": result.session_id,
            "session_url": result.session_url,
            "response_body": result.response_body,
            "detail": result.detail,
        },
    )


def release_status(task: TaskId, status: str, evidence: str, by: str, at: datetime) -> LedgerEvent:
    if status not in RELEASE_STATUSES:
        raise ValueError(f"release status must be one of {', '.join(RELEASE_STATUSES)}")
    if not evidence.strip() or not by.strip():
        raise ValueError("a release status needs its evidence and who recorded it")
    return LedgerEvent(
        RELEASE_STATUS, at, task, data={"status": status, "evidence": evidence, "by": by}
    )
