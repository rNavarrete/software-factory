"""Shared types for the factory controller.

Every controller package builds on these names. Change them only through a
PR that says so in its title (see README, "Changing the shared interfaces");
never edit them in passing as part of another ticket.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

# --- Identifiers --------------------------------------------------------------

_TASK = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_TASK_ID_RE = re.compile(rf"^{_TASK}$")
_TASK_ID_MAX = 64
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_BRANCH_RE = re.compile(rf"^claude/(?P<task>{_TASK})-a(?P<attempt>[1-9][0-9]*)$")
_TITLE_RE = re.compile(
    rf"^\[(?P<task>{_TASK}) a(?P<attempt>[1-9][0-9]*) (?P<short>[0-9a-f]{{12}})\]"
)

CONTRACT_DIGEST_PREFIX = "Contract-Digest: "
_BODY_LINE_RE = re.compile(rf"^{CONTRACT_DIGEST_PREFIX}(?P<digest>[0-9a-f]{{64}})\s*$", re.M)
_BODY_PREFIX_RE = re.compile(rf"^{CONTRACT_DIGEST_PREFIX.rstrip()}", re.M)


@dataclass(frozen=True, order=True)
class TaskId:
    """A task's id: lowercase letters, digits and single hyphens, at most 64 chars."""

    value: str

    def __post_init__(self) -> None:
        if len(self.value) > _TASK_ID_MAX or not _TASK_ID_RE.fullmatch(self.value):
            raise ValueError(f"invalid task id: {self.value!r}")

    def __str__(self) -> str:
        return self.value


def _positive_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be an int >= 1, got {value!r}")


@dataclass(frozen=True, order=True)
class AttemptId:
    """One attempt at a task. Attempt numbers start at 1 (ADR 0002 section 3)."""

    task: TaskId
    number: int

    def __post_init__(self) -> None:
        _positive_int(self.number, "attempt number")

    def __str__(self) -> str:
        return f"{self.task}-a{self.number}"

    @property
    def branch(self) -> str:
        """The marker branch the worker must push to: ``claude/<task>-a<n>``."""
        return f"claude/{self}"

    def pr_title_marker(self, digest: ContractDigest) -> str:
        """The PR title prefix: ``[<task> a<n> <digest12>]``."""
        return f"[{self.task} a{self.number} {digest.short}]"

    @classmethod
    def from_branch(cls, branch: str) -> AttemptId | None:
        """Parse a marker branch; return None for any other branch name."""
        m = _BRANCH_RE.fullmatch(branch)
        if m is None or len(m["task"]) > _TASK_ID_MAX:
            return None
        return cls(TaskId(m["task"]), int(m["attempt"]))

    @classmethod
    def from_pr_title(cls, title: str) -> tuple[AttemptId, str] | None:
        """Parse the marker at the start of a PR title.

        Returns the attempt and the 12-char digest prefix, or None.
        """
        m = _TITLE_RE.match(title)
        if m is None or len(m["task"]) > _TASK_ID_MAX:
            return None
        return cls(TaskId(m["task"]), int(m["attempt"])), m["short"]


@dataclass(frozen=True, order=True)
class RunId:
    """One fire of the runtime for an attempt. An attempt fires again only after
    a definite not-launched result (docs/limits.md)."""

    attempt: AttemptId
    fire: int

    def __post_init__(self) -> None:
        _positive_int(self.fire, "fire number")

    def __str__(self) -> str:
        return f"{self.attempt}-f{self.fire}"


@dataclass(frozen=True)
class ContractDigest:
    """sha256 of an approved contract's canonical bytes, as 64 lowercase hex chars.

    What the canonical bytes are is defined by controller/contract/ (ENG-144).
    """

    value: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.value):
            raise ValueError(f"invalid contract digest: {self.value!r}")

    @classmethod
    def of(cls, canonical_bytes: bytes) -> ContractDigest:
        return cls(hashlib.sha256(canonical_bytes).hexdigest())

    @property
    def short(self) -> str:
        """First 12 hex chars, used in the PR title marker."""
        return self.value[:12]

    @property
    def pr_body_line(self) -> str:
        """The ``Contract-Digest:`` line the pilot's CI reads from the PR body."""
        return CONTRACT_DIGEST_PREFIX + self.value

    @classmethod
    def from_pr_body(cls, body: str) -> ContractDigest | None:
        """The digest from the body's single ``Contract-Digest:`` line.

        Returns None if there is no such line, more than one, or a malformed one.
        """
        if len(_BODY_PREFIX_RE.findall(body)) != 1:
            return None
        m = _BODY_LINE_RE.search(body)
        return None if m is None else cls(m["digest"])

    def __str__(self) -> str:
        return self.value


# --- Runtime adapter ----------------------------------------------------------


class LaunchOutcome(Enum):
    """The only three results a launch can have (ADR 0002 section 6)."""

    LAUNCHED = "launched"
    NOT_LAUNCHED = "not-launched"
    OUTCOME_UNKNOWN = "launch-outcome-unknown"


# HTTP statuses the fire API documents as creating no session.
NOT_LAUNCHED_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 429})
# Fire API limits (ADR 0002 sections 4 and 6).
MAX_FIRE_TEXT_CHARS = 65_536
LAUNCH_TIMEOUT_SECONDS = 180


def classify(http_status: int | None, session_id: str | None) -> LaunchOutcome:
    """ADR 0002 section 6: map a fire response to its outcome.

    ``http_status`` is None when no response arrived (timeout, reset, crash).
    A 200 counts as launched only with a session id.
    """
    if http_status == 200 and session_id:
        return LaunchOutcome.LAUNCHED
    if http_status in NOT_LAUNCHED_STATUSES:
        return LaunchOutcome.NOT_LAUNCHED
    return LaunchOutcome.OUTCOME_UNKNOWN


@dataclass(frozen=True)
class LaunchRequest:
    run: RunId
    digest: ContractDigest
    text: str
    """The full fire payload (contract body and markers), built by dispatch."""

    def __post_init__(self) -> None:
        if len(self.text) > MAX_FIRE_TEXT_CHARS:
            raise ValueError(f"fire text is {len(self.text)} chars, limit {MAX_FIRE_TEXT_CHARS}")


@dataclass(frozen=True)
class LaunchResult:
    outcome: LaunchOutcome
    http_status: int | None = None
    session_id: str | None = None
    session_url: str | None = None
    retry_after_seconds: int | None = None
    """From a 429's Retry-After header; None if the header was absent."""
    response_body: str | None = None
    """Raw body of any non-200 response, kept for the record."""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome is not classify(self.http_status, self.session_id):
            raise ValueError(
                f"{self.outcome.value} contradicts status {self.http_status!r}"
                f" with session id {self.session_id!r}"
            )
        if self.outcome is LaunchOutcome.LAUNCHED and not self.session_url:
            raise ValueError("a launched result needs a session url")


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Starts one worker run. Implementations: adapter/fake.py, adapter/routine.py.

    Rules every adapter follows:
    - ``launch`` makes at most one launch attempt and never retries.
    - It never raises for network, timeout or parse failures; it returns
      OUTCOME_UNKNOWN instead, so the caller can record it.
    - It makes no model calls.
    """

    def launch(self, request: LaunchRequest) -> LaunchResult: ...


# --- Ledger store -------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEvent:
    """One append-only ledger entry. ``kind`` values are defined by ledger/ (ENG-147).

    ``task`` is None for factory-wide events (holds, usage snapshots, rate-limit
    waits). ``data`` values must be JSON-serializable; it is copied and frozen.
    """

    kind: str
    at: datetime
    task: TaskId | None = None
    attempt: AttemptId | None = None
    run: RunId | None = None
    data: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("ledger event times must be timezone-aware")
        if self.run is not None and self.run.attempt != self.attempt:
            raise ValueError("event run does not belong to its attempt")
        if self.attempt is not None and self.attempt.task != self.task:
            raise ValueError("event attempt does not belong to its task")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    """Position in the ledger, assigned by the store, strictly increasing."""
    event: LedgerEvent


class LedgerLocked(Exception):
    """Another controller process holds the single-writer lock."""


@runtime_checkable
class LedgerStore(Protocol):
    """The controller's durable record (SQLite at ~/.software-factory, ENG-147).

    Events are append-only: there is no update or delete.

    The writer lock only stops two controller processes writing at once. It is
    not the dispatch block: "nothing else may dispatch until reconciled"
    (ADR 0002 section 6.1) is decided from ledger events, across runs.
    """

    def append(self, *events: LedgerEvent) -> Sequence[StoredEvent]:
        """Write all events in one transaction, durably, before returning.

        Only valid while this process holds ``writer_lock()``.
        """
        ...

    def events(self, task: TaskId | None = None) -> Sequence[StoredEvent]:
        """Events in seq order: all of them, or only one task's."""
        ...

    def writer_lock(self) -> AbstractContextManager[None]:
        """Hold the single-writer lock; raises LedgerLocked if another process has it."""
        ...


__all__ = [
    "CONTRACT_DIGEST_PREFIX",
    "LAUNCH_TIMEOUT_SECONDS",
    "MAX_FIRE_TEXT_CHARS",
    "NOT_LAUNCHED_STATUSES",
    "AttemptId",
    "ContractDigest",
    "LaunchOutcome",
    "LaunchRequest",
    "LaunchResult",
    "LedgerEvent",
    "LedgerLocked",
    "LedgerStore",
    "RunId",
    "RuntimeAdapter",
    "StoredEvent",
    "TaskId",
    "classify",
]
