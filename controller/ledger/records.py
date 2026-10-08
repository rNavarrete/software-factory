"""Constructors for the ledger's own event kinds, plus the few read helpers
other tickets need.

Each constructor returns a ``LedgerEvent``; the caller appends it while holding
the writer lock, usually together with the events it belongs with. The store
re-checks every event on append (kinds.check), so these are a convenience,
not the only guard.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LedgerEvent,
    RunId,
    StoredEvent,
    TaskId,
)
from controller.ledger import kinds

LEDGER_TRAILER = "Factory-Ledger-Run: "
_TRAILER_RE = re.compile(
    rf"^{LEDGER_TRAILER}(?P<task>[a-z0-9]+(?:-[a-z0-9]+)*)-a(?P<a>[1-9][0-9]*)-f(?P<f>[1-9][0-9]*)\s*$",
    re.M,
)


# --- A run's evidence -----------------------------------------------------------------


def run_context(
    run: RunId,
    digest: ContractDigest,
    contract_version: str,
    base_commit: str,
    runtime: str,
    model_config_version: str,
    attempt_budget: int,
    at: datetime,
) -> LedgerEvent:
    """What dispatch is about to fire. Write it before contacting the runtime."""
    return LedgerEvent(
        kinds.RUN_CONTEXT,
        at,
        run.attempt.task,
        run.attempt,
        run,
        {
            "contract_digest": digest.value,
            "contract_version": contract_version,
            "base_commit": base_commit,
            "runtime": runtime,
            "model_config_version": model_config_version,
            "attempt_budget": attempt_budget,
        },
    )


def candidate(
    run: RunId,
    branch: str,
    candidate_commit: str,
    at: datetime,
    pr_number: int | None = None,
    pr_url: str | None = None,
    pr_body: str | None = None,
) -> LedgerEvent:
    """The marker branch and PR found on GitHub. ``pr_body`` is kept as a record
    only: nothing in it is ever read as authorization (G-D10)."""
    data: dict[str, object] = {"branch": branch, "candidate_commit": candidate_commit}
    for key, value in (("pr_number", pr_number), ("pr_url", pr_url), ("pr_body", pr_body)):
        if value is not None:
            data[key] = value
    return LedgerEvent(kinds.CANDIDATE, at, run.attempt.task, run.attempt, run, data)


def checks(
    run: RunId, revision: str, results: Sequence[Mapping[str, object]], at: datetime
) -> LedgerEvent:
    """Check results for one exact commit. ``results`` items carry ``name`` and
    ``conclusion``, and optionally ``workflow`` and ``url``."""
    return LedgerEvent(
        kinds.CHECKS,
        at,
        run.attempt.task,
        run.attempt,
        run,
        {"revision": revision, "results": [dict(r) for r in results]},
    )


def run_ended(run: RunId, stop_reason: str, recorded_by: str, at: datetime) -> LedgerEvent:
    return LedgerEvent(
        kinds.RUN_ENDED,
        at,
        run.attempt.task,
        run.attempt,
        run,
        {"stop_reason": stop_reason, "recorded_by": recorded_by},
    )


def attempt_abandoned(
    attempt: AttemptId, reason: str, recorded_by: str, at: datetime
) -> LedgerEvent:
    """Marks an attempt given up on. It does not clear the attempt (ADR 0002
    section 6.1); the gate still needs a clearing record for that."""
    return LedgerEvent(
        kinds.ATTEMPT_ABANDONED,
        at,
        attempt.task,
        attempt,
        data={"reason": reason, "recorded_by": recorded_by},
    )


def failure(
    stage: str,
    detail: str,
    at: datetime,
    task: TaskId | None = None,
    attempt: AttemptId | None = None,
    run: RunId | None = None,
) -> LedgerEvent:
    """A raw failure, kept verbatim apart from secret redaction."""
    return LedgerEvent(kinds.FAILURE, at, task, attempt, run, {"stage": stage, "detail": detail})


# --- People ---------------------------------------------------------------------------


def human_decision(
    identity: str,
    authenticated_by: str,
    decided_at: datetime,
    scope: str,
    decision: str,
    digest: ContractDigest,
    at: datetime,
    *,
    digest_type: str = "contract",
    task: TaskId | None = None,
    attempt: AttemptId | None = None,
    note: str | None = None,
) -> LedgerEvent:
    """A person's decision, bound to one exact contract or artifact digest (G-D3).

    ``authenticated_by`` says how the controller knew who it was (for example
    "local-os-user:rolando"). Approval rules (ENG-151) decide which scopes and
    identities count; the ledger only refuses a decision missing any of these.
    """
    if decided_at.tzinfo is None:
        raise ValueError("decision time must be timezone-aware")
    data: dict[str, object] = {
        "identity": identity,
        "authenticated_by": authenticated_by,
        "decided_at": decided_at.isoformat(),
        "scope": scope,
        "decision": decision,
        "digest_type": digest_type,
        "digest": digest.value,
    }
    if note is not None:
        data["note"] = note
    return LedgerEvent(kinds.HUMAN_DECISION, at, task, attempt, data=data)


def human_time(
    minutes: float,
    activity: str,
    entered_by: str,
    at: datetime,
    *,
    task: TaskId | None = None,
    intervention_reason: str | None = None,
) -> LedgerEvent:
    """Rolando's active minutes, and why he had to step in if he did (ENG-162)."""
    data: dict[str, object] = {"minutes": minutes, "activity": activity, "entered_by": entered_by}
    if intervention_reason is not None:
        data["intervention_reason"] = intervention_reason
    return LedgerEvent(kinds.HUMAN_TIME, at, task, data=data)


def metric(
    name: str,
    label: str,
    value: float | None,
    source: str,
    at: datetime,
    *,
    unit: str | None = None,
    task: TaskId | None = None,
    attempt: AttemptId | None = None,
    run: RunId | None = None,
) -> LedgerEvent:
    """A usage number with its label (docs/limits.md section 7). A number the
    platform does not give is recorded with label "unavailable" and no value."""
    data: dict[str, object] = {"name": name, "label": label, "value": value, "source": source}
    if unit is not None:
        data["unit"] = unit
    return LedgerEvent(kinds.METRIC, at, task, attempt, run, data)


# --- Reading --------------------------------------------------------------------------


def decisions(
    stored: Iterable[StoredEvent], digest: ContractDigest, scope: str | None = None
) -> list[LedgerEvent]:
    """Human decisions on ``digest`` (optionally one scope), from the ledger only.

    PR bodies, trailers and comments are never consulted: a line like
    "Approved-by: Rolando" on GitHub is not evidence of anything (G-D10).
    """
    return [
        s.event
        for s in stored
        if s.event.kind == kinds.HUMAN_DECISION
        and s.event.data.get("digest") == digest.value
        and (scope is None or s.event.data.get("scope") == scope)
    ]


def ledger_trailer(run: RunId) -> str:
    """The line a worker's PR body carries to point back at its ledger run.

    It only helps a person find the record. It proves nothing (G-D10)."""
    return f"{LEDGER_TRAILER}{run}"


def run_from_trailer(body: str) -> RunId | None:
    """The run a PR body points back to, or None if it has no single valid line."""
    matches = list(_TRAILER_RE.finditer(body))
    if len(matches) != 1:
        return None
    m = matches[0]
    try:
        return RunId(AttemptId(TaskId(m["task"]), int(m["a"])), int(m["f"]))
    except ValueError:
        return None


HUMAN_TIME_COLUMNS = ("seq", "at", "task", "minutes", "activity", "intervention_reason", "by")


def human_time_csv(stored: Iterable[StoredEvent]) -> str:
    """Every human-time entry as CSV, for a spreadsheet. No dashboard needed."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HUMAN_TIME_COLUMNS)
    for s in stored:
        e = s.event
        if e.kind != kinds.HUMAN_TIME:
            continue
        writer.writerow(
            [
                s.seq,
                e.at.isoformat(),
                e.task or "",
                e.data.get("minutes"),
                e.data.get("activity"),
                e.data.get("intervention_reason") or "",
                e.data.get("entered_by"),
            ]
        )
    return out.getvalue()
