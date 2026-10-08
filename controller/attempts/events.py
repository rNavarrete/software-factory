"""Ledger event kinds the attempt gate writes and reads.

The gate keeps no counters of its own. Every count is derived from these
events each time, so a restart cannot reset it (docs/limits.md section 8).

Kinds marked "written by the gate" come from gate.py. The others are human
decisions that approval (ENG-151) and recovery (ENG-153) record; the
constructors below fix the shape the gate reads, and an event of these kinds
with a missing or malformed field is ignored, which keeps the gate closed.
Whether the person behind a decision was really Rolando is checked by the
writer of that event, not here.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from controller.interfaces import AttemptId, ContractDigest, LedgerEvent, RunId

# Written by the gate.
ATTEMPT_RESERVED = "attempt-reserved"
FIRE_INTENT = "fire-intent"
FIRE_RESULT = "fire-result"
RATE_LIMIT_WAIT = "rate-limit-wait"
HOLD_SET = "hold-set"
HOLD_CLEARED = "hold-cleared"
USAGE_SNAPSHOT = "usage-snapshot"
ESCALATION = "escalation"
DISPATCH_REFUSED = "dispatch-refused"

# Human decisions the gate reads.
REPAIR_AUTHORIZED = "repair-authorized"
SOURCE_REPAIR_AUTHORIZED = "source-repair-authorized"
"""A repair started under the repair allowance of Rolando's Todo move (ENG-160),
signed by the signer process, never by Rolando at a terminal. The gate counts
it like a repair go-ahead; whether it really counts is ``Approvals``' call."""
REFIRE_AUTHORIZED = "refire-authorized"
ATTEMPT_CLEARED = "attempt-cleared"

SUBSCRIPTION_EXHAUSTED = "subscription-exhausted"
"""The reason on the only hold the gate sets by itself (docs/limits.md section 5)."""


class ClearingBasis(Enum):
    """ADR 0002 section 6.1: what lets a later attempt start after a launch that
    may have created a session. A definite not-launched fire result clears an
    attempt by itself and has no record here."""

    COMPLETED = "completed"
    """The session was found finished; its URL is recorded."""
    TERMINATED = "terminated"
    """The session was found stopped and archived; its URL is recorded."""
    WRITE_ACCESS_REMOVED = "write-access-removed"
    """The bot's push access to the pilot repo was removed and a check from
    outside the worker confirmed it can no longer push."""
    UNRESOLVED_ACCEPTED = "unresolved-accepted"
    """Rolando's recorded exception (ADR 0001 section 4): the session was never
    found, and he accepts that it may still exist."""


_NEEDS = {
    ClearingBasis.COMPLETED: "session_url",
    ClearingBasis.TERMINATED: "session_url",
    ClearingBasis.WRITE_ACCESS_REMOVED: "how_checked",
    ClearingBasis.UNRESOLVED_ACCEPTED: "exception_note",
}


def clearing_evidence_field(basis: ClearingBasis) -> str:
    """The data field a clearing record of this basis must carry."""
    return _NEEDS[basis]


def repair_authorized(attempt: AttemptId, failure: str, by: str, at: datetime) -> LedgerEvent:
    """Rolando's go-ahead for repair attempt ``attempt`` (number 2 or more), naming
    the failure it repairs (G-C2)."""
    if attempt.number < 2:
        raise ValueError("attempt 1 needs the contract approval, not a repair authorization")
    if not failure.strip() or not by.strip():
        raise ValueError("a repair authorization names the failure and who decided")
    return LedgerEvent(
        REPAIR_AUTHORIZED, at, attempt.task, attempt, data={"failure": failure, "by": by}
    )


def refire_authorized(prior: RunId, by: str, at: datetime) -> LedgerEvent:
    """Rolando's decision to fire attempt ``prior.attempt`` again after ``prior``
    came back definitely not launched (docs/limits.md section 3)."""
    if not by.strip():
        raise ValueError("a re-fire decision names who decided")
    return LedgerEvent(REFIRE_AUTHORIZED, at, prior.attempt.task, prior.attempt, prior, {"by": by})


def attempt_cleared(
    attempt: AttemptId, basis: ClearingBasis, evidence: str, by: str, at: datetime
) -> LedgerEvent:
    """A clearing record (ADR 0002 section 6.1). ``evidence`` is the session URL,
    how the loss of push access was checked, or the exception note."""
    if not evidence.strip() or not by.strip():
        raise ValueError("a clearing record needs its evidence and who recorded it")
    return LedgerEvent(
        ATTEMPT_CLEARED,
        at,
        attempt.task,
        attempt,
        data={"basis": basis.value, clearing_evidence_field(basis): evidence, "by": by},
    )


def attempt_reserved(attempt: AttemptId, digest: ContractDigest, at: datetime) -> LedgerEvent:
    return LedgerEvent(ATTEMPT_RESERVED, at, attempt.task, attempt, data={"digest": digest.value})
