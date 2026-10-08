"""Event kinds the ledger defines, and the shape each one must have.

The attempt gate (controller/attempts/events.py, ENG-146) defines the kinds
for reserving attempts, fires and their results, holds, snapshots and
escalations. The kinds here record the rest of a run's evidence and the
human side of the factory. ``SqliteLedgerStore.append`` checks every event of
a kind listed here and refuses the whole write if one is malformed, so a
decision without a named person or an exact digest never reaches the ledger
(G-D3).

The gate's own decision kinds (repair, re-fire, clearing) are checked here
too, to the shape the gate's constructors write: a named person and the
evidence the decision rests on. They carry no digest or authentication yet;
approval binding (ENG-151) decides whether they should. Other gate kinds are
stored as they are; the gate checks them.

``task``, ``attempt`` and ``run`` live on the event itself, never in ``data``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime

from controller.attempts import events as gate
from controller.interfaces import LedgerEvent

# What dispatch knows about a run just before it fires: the exact contract,
# base commit, runtime and model/config version (ADR 0001 section 4).
RUN_CONTEXT = "run-context"
# The marker branch, candidate commit and PR found on GitHub for a run.
CANDIDATE = "candidate"
# Check results read from GitHub for one exact commit.
CHECKS = "checks"
# Why and how a run ended, as far as anyone knows.
RUN_ENDED = "run-ended"
# An attempt given up on before it reached a usable result. Kept, never deleted.
ATTEMPT_ABANDONED = "attempt-abandoned"
# A raw failure (controller error, adapter error, unreadable response), kept verbatim.
FAILURE = "failure"
# A human decision bound to an exact contract or artifact digest (G-D3).
HUMAN_DECISION = "human-decision"
# Rolando's active minutes and why he had to step in (ENG-162).
HUMAN_TIME = "human-time"
# A usage number with its honesty label (docs/limits.md section 7).
METRIC = "metric"

RUNTIMES = ("cloud-routine", "local")
METRIC_LABELS = ("exact", "recorded-by-rolando", "approximate-account-wide", "unavailable")
DIGEST_TYPES = ("contract", "artifact")
CHECK_CONCLUSIONS = (
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "pending",
)
MAX_HUMAN_MINUTES = 24 * 60

_KIND_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https://\S+$")


class InvalidEvent(ValueError):
    """The ledger refused an event because it is missing evidence it must carry."""


def check(event: LedgerEvent) -> None:
    """Raise InvalidEvent if ``event`` may not be written to the ledger."""
    if not _KIND_RE.fullmatch(event.kind):
        raise InvalidEvent(f"invalid event kind {event.kind!r}")
    rule = _RULES.get(event.kind)
    if rule is None:
        return
    errors: list[str] = []
    rule(event, errors)
    if errors:
        raise InvalidEvent(f"{event.kind}: " + "; ".join(errors))


# --- Field checks ---------------------------------------------------------------


def _text(errors: list[str], data: Mapping[str, object], key: str, optional=False) -> None:
    value = data.get(key)
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be non-empty text")


def _match(errors, data, key, pattern: re.Pattern[str], what: str, optional=False) -> None:
    value = data.get(key)
    if value is None and optional:
        return
    if not isinstance(value, str) or not pattern.fullmatch(value):
        errors.append(f"{key} must be {what}")


def _one_of(errors, data, key, allowed: tuple[str, ...]) -> None:
    if data.get(key) not in allowed:
        errors.append(f"{key} must be one of {', '.join(allowed)}")


def _int(errors, data, key, low: int, high: int | None = None, optional=False) -> None:
    value = data.get(key)
    if value is None and optional:
        return
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= low
    if not ok or (high is not None and value > high):
        errors.append(f"{key} must be an int from {low}" + (f" to {high}" if high else " up"))


def _time(errors, data, key) -> None:
    value = data.get(key)
    try:
        ok = isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        ok = False
    if not ok:
        errors.append(f"{key} must be an ISO 8601 time with a timezone")


def _needs(errors, event: LedgerEvent, what: str) -> None:
    if getattr(event, what) is None:
        errors.append(f"needs a {what}")


def _no_session(errors, data) -> None:
    """Session ids come only from a launched fire result; never invent one (AC 3)."""
    for key in ("session_id", "session_url"):
        if key in data:
            errors.append(f"{key} is recorded only from the fire result, not here")


# --- Rules ------------------------------------------------------------------------


def _run_context(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    _needs(errors, event, "run")
    _match(errors, d, "contract_digest", _DIGEST_RE, "64 lowercase hex chars")
    _text(errors, d, "contract_version")
    _match(errors, d, "base_commit", _SHA_RE, "a 40-char commit sha")
    _one_of(errors, d, "runtime", RUNTIMES)
    _text(errors, d, "model_config_version")
    _int(errors, d, "attempt_budget", 1, 3)
    _no_session(errors, d)


def _candidate(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    _needs(errors, event, "run")
    _text(errors, d, "branch")
    _match(errors, d, "candidate_commit", _SHA_RE, "a 40-char commit sha")
    _int(errors, d, "pr_number", 1, optional=True)
    _match(errors, d, "pr_url", _URL_RE, "an https url", optional=True)
    _text(errors, d, "pr_body", optional=True)


def _checks(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    _needs(errors, event, "run")
    _match(errors, d, "revision", _SHA_RE, "a 40-char commit sha")
    results = d.get("results")
    if not isinstance(results, tuple) or not results:
        errors.append("results must be a non-empty list")
        return
    for i, r in enumerate(results):
        if not isinstance(r, Mapping):
            errors.append(f"results[{i}] must be an object")
            continue
        _text(errors, r, "name")
        _one_of(errors, r, "conclusion", CHECK_CONCLUSIONS)
        _text(errors, r, "workflow", optional=True)
        _match(errors, r, "url", _URL_RE, "an https url", optional=True)


def _run_ended(event: LedgerEvent, errors: list[str]) -> None:
    _needs(errors, event, "run")
    _text(errors, event.data, "stop_reason")
    _text(errors, event.data, "recorded_by")


def _attempt_abandoned(event: LedgerEvent, errors: list[str]) -> None:
    _needs(errors, event, "attempt")
    _text(errors, event.data, "reason")
    _text(errors, event.data, "recorded_by")


def _failure(event: LedgerEvent, errors: list[str]) -> None:
    _text(errors, event.data, "stage")
    # Kept even when empty: str(TimeoutError()) is "", and the failure still happened.
    if not isinstance(event.data.get("detail"), str):
        errors.append("detail must be text")


def _human_decision(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    _text(errors, d, "identity")
    _text(errors, d, "authenticated_by")
    _time(errors, d, "decided_at")
    _text(errors, d, "scope")
    _text(errors, d, "decision")
    _one_of(errors, d, "digest_type", DIGEST_TYPES)
    _match(errors, d, "digest", _DIGEST_RE, "64 lowercase hex chars")


def _human_time(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    minutes = d.get("minutes")
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int | float)
        or not 0 < minutes <= MAX_HUMAN_MINUTES
    ):
        errors.append(f"minutes must be a number above 0 and at most {MAX_HUMAN_MINUTES}")
    _text(errors, d, "activity")
    _text(errors, d, "intervention_reason", optional=True)
    _text(errors, d, "entered_by")


def _metric(event: LedgerEvent, errors: list[str]) -> None:
    d = event.data
    _text(errors, d, "name")
    _one_of(errors, d, "label", METRIC_LABELS)
    value = d.get("value")
    if d.get("label") == "unavailable":
        if value is not None:
            errors.append("an unavailable metric has no value (never 0 or an estimate)")
    elif isinstance(value, bool) or not isinstance(value, int | float):
        errors.append("value must be a number")
    _text(errors, d, "unit", optional=True)
    _text(errors, d, "source")


def _repair_authorized(event: LedgerEvent, errors: list[str]) -> None:
    _needs(errors, event, "attempt")
    if event.attempt is not None and event.attempt.number < 2:
        errors.append("attempt 1 needs the contract approval, not a repair authorization")
    _text(errors, event.data, "failure")
    _text(errors, event.data, "by")


def _refire_authorized(event: LedgerEvent, errors: list[str]) -> None:
    _needs(errors, event, "run")
    _text(errors, event.data, "by")


def _attempt_cleared(event: LedgerEvent, errors: list[str]) -> None:
    _needs(errors, event, "attempt")
    _text(errors, event.data, "by")
    try:
        basis = gate.ClearingBasis(event.data.get("basis"))
    except ValueError:
        errors.append("basis must be one of " + ", ".join(b.value for b in gate.ClearingBasis))
        return
    _text(errors, event.data, gate.clearing_evidence_field(basis))


_RULES: dict[str, Callable[[LedgerEvent, list[str]], None]] = {
    RUN_CONTEXT: _run_context,
    CANDIDATE: _candidate,
    CHECKS: _checks,
    RUN_ENDED: _run_ended,
    ATTEMPT_ABANDONED: _attempt_abandoned,
    FAILURE: _failure,
    HUMAN_DECISION: _human_decision,
    "source-authorization": _human_decision,
    HUMAN_TIME: _human_time,
    METRIC: _metric,
    gate.REPAIR_AUTHORIZED: _repair_authorized,
    gate.REFIRE_AUTHORIZED: _refire_authorized,
    gate.ATTEMPT_CLEARED: _attempt_cleared,
}

CHECKED_KINDS: frozenset[str] = frozenset(_RULES)
