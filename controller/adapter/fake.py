"""A scripted RuntimeAdapter for tests. It never touches the network.

Each ``launch`` call consumes the next step of the script. The fake also keeps
the ground truth a real runtime hides from the controller: which calls really
created a session. Recovery and dispatch tests (ENG-153, ENG-176) use
``sessions_created`` to check that a lost response is never treated as
"nothing started".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from controller.interfaces import (
    NOT_LAUNCHED_STATUSES,
    LaunchOutcome,
    LaunchRequest,
    LaunchResult,
    RunId,
)


class FakeBehavior(Enum):
    LAUNCH = "launch"
    """200 with a session id: a session starts."""
    REJECTED = "rejected"
    """A documented no-session status (429 rate limit, 400 paused, 401, 403, 404)."""
    LOST_RESPONSE = "lost-response"
    """No usable answer: timeout, dropped connection, 5xx, or 200 without a
    session id. Whether a session started is hidden from the caller;
    ``session_created`` sets the hidden truth."""


@dataclass(frozen=True)
class FakeStep:
    behavior: FakeBehavior
    status: int | None = None
    retry_after_seconds: int | None = None
    body: str | None = None
    session_created: bool = False

    def __post_init__(self) -> None:
        if self.behavior is FakeBehavior.REJECTED and self.status not in NOT_LAUNCHED_STATUSES:
            raise ValueError(f"rejected needs a no-session status, got {self.status!r}")
        if self.behavior is FakeBehavior.LOST_RESPONSE and self.status in NOT_LAUNCHED_STATUSES:
            raise ValueError(f"status {self.status} is a rejection, not a lost response")
        if self.behavior is FakeBehavior.REJECTED and self.session_created:
            raise ValueError("a rejected fire creates no session")
        launch_ok = self.status == 200 and self.session_created
        if self.behavior is FakeBehavior.LAUNCH and not launch_ok:
            raise ValueError("a launch step is a 200 that creates a session")

    @classmethod
    def launch(cls) -> FakeStep:
        return cls(FakeBehavior.LAUNCH, status=200, session_created=True)

    @classmethod
    def rate_limited(cls, retry_after_seconds: int | None = 60, body: str = "") -> FakeStep:
        """A 429. Pass ``retry_after_seconds=None`` for a 429 with no Retry-After."""
        return cls(FakeBehavior.REJECTED, 429, retry_after_seconds, body)

    @classmethod
    def rejected(cls, status: int = 400, body: str = "") -> FakeStep:
        return cls(FakeBehavior.REJECTED, status, body=body)

    @classmethod
    def lost_response(
        cls, session_created: bool = True, status: int | None = None, body: str | None = None
    ) -> FakeStep:
        """No response (``status=None``), or a 5xx / 200-without-session response."""
        return cls(FakeBehavior.LOST_RESPONSE, status, body=body, session_created=session_created)


@dataclass(frozen=True)
class FakeSession:
    session_id: str
    url: str
    run: RunId


class ScriptExhausted(AssertionError):
    """The test fired more often than its script allows."""


class FakeRuntimeAdapter:
    def __init__(self, steps: Iterable[FakeStep]) -> None:
        self._steps = list(steps)
        self.requests: list[LaunchRequest] = []
        self.sessions_created: list[FakeSession] = []

    def launch(self, request: LaunchRequest) -> LaunchResult:
        if len(self.requests) >= len(self._steps):
            raise ScriptExhausted(f"unscripted launch for run {request.run}")
        step = self._steps[len(self.requests)]
        self.requests.append(request)

        session = self._create_session(request) if step.session_created else None
        if step.behavior is FakeBehavior.LAUNCH:
            return LaunchResult(
                LaunchOutcome.LAUNCHED,
                http_status=200,
                session_id=session.session_id if session else None,
                session_url=session.url if session else None,
            )
        if step.behavior is FakeBehavior.REJECTED:
            return LaunchResult(
                LaunchOutcome.NOT_LAUNCHED,
                http_status=step.status,
                retry_after_seconds=step.retry_after_seconds,
                response_body=step.body,
                detail="rejected",
            )
        return LaunchResult(
            LaunchOutcome.OUTCOME_UNKNOWN,
            http_status=step.status,
            response_body=step.body,
            detail="response lost",
        )

    @property
    def remaining_steps(self) -> int:
        return len(self._steps) - len(self.requests)

    def _create_session(self, request: LaunchRequest) -> FakeSession:
        session_id = f"cse_fake{len(self.sessions_created) + 1:04d}"
        session = FakeSession(session_id, f"https://claude.ai/code/{session_id}", request.run)
        self.sessions_created.append(session)
        return session
