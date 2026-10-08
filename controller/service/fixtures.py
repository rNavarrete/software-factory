"""Stand-ins for the integrations, for tests and the qualification run.

Each one implements a protocol from ``seams.py`` without touching Linear or
GitHub, so the service can be built, tested and run on the host before
ENG-174, 175, 178, 156 and 160 land. The qualification run on the host uses
these with the fake runtime adapter, so it never spends a real worker fire.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from controller.interfaces import AttemptId
from controller.service.seams import (
    Answer,
    Authorization,
    Control,
    FailureReport,
    IntakeBatch,
    Prepared,
    PullRequestRef,
    Question,
    Refusal,
    ReportFailed,
    Standing,
)

log = logging.getLogger("factory.service.fixtures")


class FixtureSource:
    """Serves a fixed list of events. The cursor is how many have been read,
    so polling again from an old cursor replays events, as a real source might."""

    def __init__(
        self,
        events: Sequence[Authorization | Refusal | Control] = (),
        *,
        withdrawn: Mapping[str, str] | None = None,
    ) -> None:
        self.events = list(events)
        self.withdrawn = dict(withdrawn or {})
        """issue_id -> reason ``revalidate`` gives."""
        self.changed: dict[str, str] = {}
        """issue_id -> what changed after the move."""
        self.blocked: dict[str, tuple[str, ...]] = {}
        """issue_id -> keys of open tickets that block it."""
        self.fail_next: Exception | None = None
        self.polls: list[str | None] = []

    def poll(self, cursor: str | None) -> IntakeBatch:
        self.polls.append(cursor)
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e
        start = int(cursor or 0)
        batch = self.events[start:]
        return IntakeBatch(
            authorizations=[e for e in batch if isinstance(e, Authorization)],
            refusals=[e for e in batch if isinstance(e, Refusal)],
            controls=[e for e in batch if isinstance(e, Control)],
            cursor=str(len(self.events)),
        )

    def revalidate(self, authorization: Authorization) -> str | None:
        return self.standing(authorization).reason

    def standing(self, authorization: Authorization) -> Standing:
        i = authorization.issue_id
        return Standing(self.withdrawn.get(i), self.changed.get(i), self.blocked.get(i, ()))


class FixturePreparer:
    """Contracts (or questions) by ticket key."""

    def __init__(self, by_issue: Mapping[str, Mapping[str, object] | str]) -> None:
        self.by_issue = dict(by_issue)
        self.calls: list[str] = []

    def prepare(self, authorization: Authorization, project: Mapping[str, object]):
        self.calls.append(authorization.issue_key)
        out = self.by_issue[authorization.issue_key]
        if isinstance(out, str):
            return Question(out)
        return Prepared(out)


class RecordingReporter:
    """Keeps what would be posted, by key; a key posted twice shows once.
    Set ``down`` to make every post fail."""

    def __init__(self) -> None:
        self.posts: dict[str, tuple[str, str]] = {}
        self.calls = 0
        self.down = False

    def post(self, issue_id: str, key: str, text: str) -> None:
        self.calls += 1
        if self.down:
            raise ReportFailed("Linear is unreachable")
        self.posts.setdefault(key, (issue_id, text))

    def texts(self, issue_id: str | None = None) -> list[str]:
        return [t for i, t in self.posts.values() if issue_id is None or i == issue_id]


class LogReporter:
    """Writes each message to the service log instead of Linear."""

    def post(self, issue_id: str, key: str, text: str) -> None:
        log.info("[%s] %s: %s", issue_id, key, text)


class RecordingReviewer:
    """Starts at most one review per request key, as a real reviewer must."""

    def __init__(self) -> None:
        self.started: list[PullRequestRef] = []
        self.calls: list[str] = []

    def start(self, pr: PullRequestRef, key: str) -> str:
        self.calls.append(key)
        if key not in self.calls[:-1]:
            self.started.append(pr)
            return f"fixture review of PR #{pr.number}"
        return f"fixture review of PR #{pr.number} (already started)"


class FixtureDecisions:
    """Answers by issue id, as ``DecisionReader`` would return them."""

    def __init__(self, by_issue: Mapping[str, Sequence[Answer]] | None = None) -> None:
        self.by_issue = {k: list(v) for k, v in (by_issue or {}).items()}
        self.calls: list[str] = []

    def answers(self, issue_id: str) -> Sequence[Answer]:
        self.calls.append(issue_id)
        return list(self.by_issue.get(issue_id, ()))


class NoRepair:
    """No failure is ever reported, so nothing is repaired automatically."""

    def failure(self, attempt: AttemptId) -> FailureReport | None:
        return None


class FixtureFailures:
    """Failure reports by attempt, as a review would give them."""

    def __init__(self, by_attempt: Mapping[AttemptId, FailureReport] | None = None) -> None:
        self.by_attempt = dict(by_attempt or {})
        self.calls: list[AttemptId] = []
        self.fail_next: Exception | None = None

    def failure(self, attempt: AttemptId) -> FailureReport | None:
        self.calls.append(attempt)
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e
        return self.by_attempt.get(attempt)


def load_events(path: Path) -> list[Authorization | Refusal | Control]:
    """Fixture events from a JSON list (the qualification run's input)."""
    out: list[Authorization | Refusal | Control] = []
    for raw in json.loads(path.read_text()):
        kind = raw.pop("type")
        if kind == "authorization":
            raw["moved_at"] = datetime.fromisoformat(raw["moved_at"])
            out.append(Authorization(**raw))
        elif kind == "refusal":
            out.append(Refusal(**raw))
        elif kind == "control":
            raw["at"] = datetime.fromisoformat(raw["at"])
            out.append(Control(**raw))
        else:
            raise ValueError(f"unknown fixture event type {kind!r}")
    return out


__all__ = [
    "FixtureDecisions",
    "FixtureFailures",
    "FixturePreparer",
    "FixtureSource",
    "LogReporter",
    "NoRepair",
    "RecordingReporter",
    "RecordingReviewer",
    "load_events",
]
