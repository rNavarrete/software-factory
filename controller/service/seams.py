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
_OPTION_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")
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


@dataclass(frozen=True)
class Prepared:
    contract: Mapping[str, object]
    summary: str = ""
    """A few plain sentences on what the worker will change and how it will be
    checked, posted once on the ticket. Information only: it asks for nothing."""


QUESTION_KINDS = ("product", "split", "scope", "changed", "factory")
"""Why the preparer stopped: ``product`` missing or unclear behavior; ``split``
the ticket is too big (the text may propose a split); ``scope`` it asks for
something this project's policy doesn't allow; ``changed`` the ticket no longer
matches the move that authorized it; ``factory`` the factory couldn't draft a
task it trusts (its own fault, not the ticket's). ``changed`` and ``factory``
are notices, not questions: they carry no options."""


@dataclass(frozen=True)
class Option:
    """One answer Rolando can pick for a ``Question``."""

    id: str
    """Short and unique within the question: ``A``, ``B``..."""
    label: str
    consequence: str
    """What happens if he picks it, in one sentence."""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _OPTION_RE.fullmatch(self.id):
            raise ValueError(f"option id must be 1-12 letters or digits, got {self.id!r}")
        _text(self.label, "option label")
        _text(self.consequence, "option consequence")


@dataclass(frozen=True)
class Question:
    """A product question for Rolando, posted on the ticket. The item closes;
    moving the ticket to Todo again after answering starts a new one.

    Only ``text`` is required. The rest is what makes it answerable in one
    go: why it is asked, the choices and what each one does, which one the
    factory would pick (a recommendation, never consent), and what waiting
    costs."""

    text: str
    context: str = ""
    options: Sequence[Option] = ()
    recommended: str | None = None
    """An ``Option.id``. Shown as a recommendation; never taken as his answer."""
    if_no_answer: str = ""
    key: str = ""
    """Stable for the same question on the same ticket text, so it is never
    asked twice. Letters, digits, - and _ only."""
    kind: str = "product"
    """One of ``QUESTION_KINDS``."""

    def __post_init__(self) -> None:
        _text(self.text, "question text")
        if self.kind not in QUESTION_KINDS:
            raise ValueError(f"question kind must be one of {QUESTION_KINDS}, got {self.kind!r}")
        ids = [o.id.lower() for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        if self.recommended is not None and self.recommended.lower() not in ids:
            raise ValueError(f"recommended option {self.recommended!r} is not one of the options")
        if self.key:
            _id(self.key, "question key")


@dataclass(frozen=True)
class Answer:
    """Rolando's reply to one of the factory's questions, read from Linear.

    Product input for drafting only. It is never an approval, a repair
    go-ahead or a clearing: those stay signed records."""

    question_key: str
    option: str | None
    """The option id his reply names, if it names exactly one."""
    text: str
    """His reply as written. Untrusted text, like any ticket text."""
    comment_id: str
    at: datetime
    body_sha256: str
    """The reply exactly as read, so a later edit can be told apart."""


@runtime_checkable
class DecisionReader(Protocol):
    """ENG-178. Answers to the factory's questions on a ticket."""

    def answers(self, issue_id: str) -> Sequence[Answer]:
        """Replies Rolando himself wrote (his Linear user, no bot, app or
        integration, not edited after posting) in the thread of the factory's
        own question comment, oldest first. Raises on network or API failure."""
        ...


@runtime_checkable
class ContractPreparer(Protocol):
    """ENG-175."""

    def prepare(
        self, authorization: Authorization, project: Mapping[str, object]
    ) -> Prepared | Question:
        """``project`` is the onboarding entry (repository, checks, actions...)."""
        ...


class ReportFailed(Exception):
    """The message did not reach Linear; the service retries it later.

    ``hold_all`` says Linear itself is unavailable (down, or rate limiting
    the factory): the service then stops posting for this round instead of
    trying every queued message against it."""

    def __init__(self, message: str = "", *, hold_all: bool = False) -> None:
        super().__init__(message)
        self.hold_all = hold_all


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


@dataclass(frozen=True)
class Integrations:
    source: AuthorizationSource
    preparer: ContractPreparer
    reporter: Reporter
    reviewer: ReviewStarter
    repair: RepairAdvisor
    decisions: DecisionReader | None = None
    """For the preparer (ENG-175); None until it is wired."""


__all__ = [
    "CONTROLS",
    "QUESTION_KINDS",
    "Answer",
    "Authorization",
    "AuthorizationSource",
    "ContractPreparer",
    "Control",
    "DecisionReader",
    "IntakeBatch",
    "Integrations",
    "Option",
    "Prepared",
    "PullRequestRef",
    "Question",
    "Refusal",
    "RepairAdvisor",
    "ReportFailed",
    "Reporter",
    "ReviewStarter",
]
