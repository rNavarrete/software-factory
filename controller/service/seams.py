"""Where the background service meets the tickets that plug into it.

The service (ENG-194) owns the loop: it holds the queue, the cursor, the
outbox and the order things happen in, and it fires only through the
existing ``Dispatcher`` / ``Launcher``. Everything Linear-shaped or
judgement-shaped comes in through one of the protocols below, so each ticket
can be built and tested on its own against the fixtures in ``fixtures.py``:

- ``AuthorizationSource``: ENG-174. Reads Linear and returns only Todo moves
  it has proved were made by Rolando on an exact ticket revision, plus the
  moves it refused and Rolando's pause/resume controls.
- ``ContractPreparer``: ENG-175. Turns an authorized ticket into a contract,
  or into a product question for Rolando.
- ``Reporter``: ENG-178. Posts the service's messages on Linear tickets.
- ``ReviewStarter``: ENG-156. Starts the independent review of a worker PR.
- ``RepairAdvisor``: ENG-160. Says whether a failed attempt should be
  repaired, and why. It never authorizes the repair itself.

Nothing here approves a contract. The service fires only a contract that
already has Rolando's signed approval in the ledger (``Approvals.check``).
How a verified Todo move becomes that approval is ENG-174's to propose and
Rolando's to sign off; until then the service queues the ticket and reports
"waiting for approval" on it.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from controller.interfaces import AttemptId, TaskId

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
"""Linear ids (UUIDs) and the like. Kept plain so the ledger's redaction can
never change an id the service later compares."""
CONTROLS = ("pause", "resume")


def _text(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be non-empty text")


def _id(value: object, what: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{what} must be letters, digits, - or _, got {value!r}")


def _aware(value: datetime, what: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware")


@dataclass(frozen=True)
class Authorization:
    """One Todo move the source has verified: who moved which ticket, when,
    and the exact ticket text (``revision``, sha256) they moved."""

    event_id: str
    """Unique and stable per move (Linear's history entry id). A replay of the
    same move carries the same id and is ignored."""
    issue_id: str
    issue_key: str
    """``ENG-186``. The task id is its lowercase form."""
    project_id: str
    actor: str
    moved_at: datetime
    revision: str
    evidence: str
    """How the source verified it, in plain words, for the record."""

    def __post_init__(self) -> None:
        for name in ("event_id", "issue_id", "project_id"):
            _id(getattr(self, name), name)
        for name in ("actor", "evidence"):
            _text(getattr(self, name), name)
        if not _ISSUE_KEY_RE.fullmatch(self.issue_key):
            raise ValueError(f"not a Linear issue key: {self.issue_key!r}")
        if not _REVISION_RE.fullmatch(self.revision):
            raise ValueError("revision must be 64 lowercase hex chars")
        _aware(self.moved_at, "moved_at")

    @property
    def task(self) -> TaskId:
        return TaskId(self.issue_key.lower())


@dataclass(frozen=True)
class Refusal:
    """A Todo move the source would not accept (a bot moved it, an import, an
    edit after the move...). The service records it and says so on the ticket."""

    event_id: str
    issue_id: str
    issue_key: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("event_id", "issue_id"):
            _id(getattr(self, name), name)
        _text(self.reason, "reason")


@dataclass(frozen=True)
class Control:
    """Rolando's pause or resume, verified the same way as a Todo move."""

    event_id: str
    action: str
    actor: str
    at: datetime
    note: str
    issue_id: str | None = None
    """Where to acknowledge it; None means the factory status ticket."""

    def __post_init__(self) -> None:
        if self.action not in CONTROLS:
            raise ValueError(f"control must be one of {CONTROLS}, got {self.action!r}")
        _id(self.event_id, "event_id")
        if self.issue_id is not None:
            _id(self.issue_id, "issue_id")
        for name in ("actor", "note"):
            _text(getattr(self, name), name)
        _aware(self.at, "at")


@dataclass(frozen=True)
class IntakeBatch:
    authorizations: Sequence[Authorization] = ()
    refusals: Sequence[Refusal] = ()
    controls: Sequence[Control] = ()
    cursor: str = ""
    """Where the next poll starts. Saved only together with what this batch
    produced, so a crash re-reads the batch instead of losing it."""


@dataclass(frozen=True)
class Standing:
    """Whether an accepted Todo move still stands, read from Linear now."""

    withdrawn: str | None = None
    """The ticket left Todo (and isn't in progress), was deleted, or moved to
    a project the factory doesn't work on. Before launch the item closes;
    after launch nothing more starts for it and the attempt is reconciled."""
    changed: str | None = None
    """What defines the work changed after the move. Before launch the item
    closes (Rolando moves it to Todo again to approve the new text); after
    launch the worker keeps its contract and Rolando is told."""
    waiting_on: Sequence[str] = ()
    """Tickets that block this one and aren't done: it waits for them."""
    in_todo: bool = True
    """False when the ticket is in a started state (In Progress), or left
    Todo at any point since the move, even if it came back (whoever moved it
    back). That still stands while the worker runs, but before launch it
    means the ticket left Todo, so the queued work is cancelled."""

    @property
    def reason(self) -> str | None:
        return self.withdrawn or self.changed


@runtime_checkable
class AuthorizationSource(Protocol):
    """ENG-174. Must be safe to call again with the same cursor: the service
    ignores event ids it has already recorded."""

    def poll(self, cursor: str | None) -> IntakeBatch:
        """Everything since ``cursor`` (None: from the start of onboarding).
        Raises on network or API failure; the service then records nothing."""
        ...

    def revalidate(self, authorization: Authorization) -> str | None:
        """Checked again right before the first fire: None if the move still
        stands (ticket still in Todo or started by the factory, same revision,
        same project), else the reason in plain words."""
        ...

    def standing(self, authorization: Authorization) -> Standing:
        """The same check in detail (ENG-174): withdrawn, changed, or waiting
        on blocking tickets. The service uses it before every dispatch try and
        while the worker runs."""
        ...


@dataclass(frozen=True)
class Prepared:
    contract: Mapping[str, object]


@dataclass(frozen=True)
class Question:
    text: str
    """A product question for Rolando, posted on the ticket. The item closes;
    moving the ticket to Todo again after answering starts a new one."""


@runtime_checkable
class ContractPreparer(Protocol):
    """ENG-175."""

    def prepare(
        self, authorization: Authorization, project: Mapping[str, object]
    ) -> Prepared | Question:
        """``project`` is the onboarding entry (repository, checks, actions...)."""
        ...


class ReportFailed(Exception):
    """The message did not reach Linear; the service retries it later."""


@runtime_checkable
class Reporter(Protocol):
    """ENG-178."""

    def post(self, issue_id: str, key: str, text: str) -> None:
        """Post ``text`` on the issue. ``key`` is unique per message: posting the
        same key twice must not show two comments (the service retries after
        a failure it cannot tell from a lost answer). Raise ReportFailed."""
        ...


@dataclass(frozen=True)
class PullRequestRef:
    attempt: AttemptId
    number: int
    url: str = ""


@runtime_checkable
class ReviewStarter(Protocol):
    """ENG-156. Called when an attempt's PR is first seen, with a request key
    the service recorded beforehand."""

    def start(self, pr: PullRequestRef, key: str) -> str:
        """Start the review with the reviewer's own identity; return a short
        note for the record. Must be idempotent per ``key``: after a crash or a
        failed save the service calls again with the same key, and that call
        must return the review already started instead of starting another.
        Raise to have the service try again next round."""
        ...


@runtime_checkable
class RepairAdvisor(Protocol):
    """ENG-160."""

    def advise(self, attempt: AttemptId, detail: str) -> str | None:
        """The failure a repair should fix, or None for no repair. The service
        only reports the advice; a repair still needs its signed go-ahead."""
        ...


class AuthorizationRefused(Exception):
    """The signer would not turn the Todo move into an approval."""

    def __init__(self, reason: str, *, final: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final
        """True: asking again can't help (the item closes). False: try later."""


@runtime_checkable
class Authorizer(Protocol):
    """ENG-174. Rolando's Todo move as the approval of the drafted contract.
    On the host this asks the signer process, which checks the move with
    Linear itself and signs; the service never signs anything."""

    def authorize(self, authorization: Authorization, contract: Mapping[str, object]):
        """The signed ``source-authorization`` ledger event to append, or
        raise AuthorizationRefused."""
        ...


@dataclass(frozen=True)
class Integrations:
    source: AuthorizationSource
    preparer: ContractPreparer
    reporter: Reporter
    reviewer: ReviewStarter
    repair: RepairAdvisor
    authorizer: Authorizer | None = None
    """None: only Rolando's typed approvals count (the qualification run)."""


__all__ = [
    "CONTROLS",
    "Authorization",
    "AuthorizationRefused",
    "AuthorizationSource",
    "Authorizer",
    "ContractPreparer",
    "Control",
    "IntakeBatch",
    "Integrations",
    "Prepared",
    "PullRequestRef",
    "Question",
    "Refusal",
    "RepairAdvisor",
    "Standing",
    "ReportFailed",
    "Reporter",
    "ReviewStarter",
]
