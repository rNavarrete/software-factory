"""Where every attempt stands, rebuilt from the ledger each time.

Pure functions, no I/O, no clock: callers pass ``now`` and the ledger's events.
Nothing here remembers anything between calls, so a restarted controller sees
exactly what the old one saw.

Two separate questions are answered for each attempt:

- **State**: how far the work got (``State``). Only a merged PR that carries
  the attempt's markers is success. A PR existing, a draft, failing or passing
  checks, or a PR closed without merging never is.
- **Writer**: whether a cloud session may still be pushing for it. Only a
  definite not-launched response or a signed clearing that meets ADR 0002
  section 6.1 settles that. Silence, run time, a quiet branch or a PR never do.

``stored`` must be the ledger as ``Approvals.trusted_events`` returns it, so an
unsigned clearing counts for nothing here, just as it does at dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from controller.attempts import events as gate
from controller.attempts.policy import AttemptState, LedgerView, Notice
from controller.interfaces import AttemptId, LaunchOutcome, RunId, StoredEvent, TaskId
from controller.ledger import kinds as ledger
from controller.recovery import events as ev


class State(Enum):
    READY = "ready"
    """No attempt yet. Whether dispatch may start one is the approval's call."""
    DISPATCHING = "dispatching"
    """The fire is recorded as intended and its response is not in yet."""
    UNKNOWN = "launch-outcome-unknown"
    """No usable response, or the controller stopped mid-launch. A session may
    exist. Rolando reconciles; nothing is retried."""
    RUNNING = "running"
    """A session started and has produced no PR yet."""
    VERIFYING = "verifying"
    """A PR with the attempt's markers is open and its checks are not all in."""
    AWAITING_HUMAN = "awaiting-human"
    """Rolando decides next: review, repair, re-fire, or close."""
    FAILED = "failed"
    CANCELED = "canceled"
    MERGED = "accepted-merged"
    """Rolando merged the attempt's PR."""


FINISHED = frozenset({State.FAILED, State.CANCELED, State.MERGED})
CLOSE_OUTCOMES = frozenset({State.FAILED, State.CANCELED})

INTERRUPTED_AFTER = timedelta(minutes=10)
"""A fire with no recorded response this long after its intent means the
controller stopped mid-launch. The adapter gives up after 180 seconds, so a
live launch call has long returned by then."""

_PASSED = frozenset({"success", "neutral", "skipped"})
_PENDING = frozenset({"pending"})


@dataclass(frozen=True)
class AttemptStatus:
    attempt: AttemptId
    digest: str
    state: State
    detail: str
    writer: str
    """In plain words: whether a session may still be pushing for this attempt."""
    writer_cleared: bool
    """True when nothing can still be writing for this attempt as far as the
    rules go: a definite not-launched response, or a signed clearing that meets
    ADR 0002 section 6.1 (including Rolando's recorded exception)."""
    latest_run: RunId | None
    session_urls: tuple[str, ...]
    """Every session URL on record for the latest fire."""
    pull_requests: tuple[int, ...]
    """PRs found on GitHub that carry this attempt's markers."""
    next_step: str
    problem: str | None = None
    """Why a signed clearing on record does not meet the rules, if it doesn't."""


@dataclass(frozen=True)
class TaskStatus:
    task: TaskId
    state: State
    attempts: tuple[AttemptStatus, ...]
    release: str
    """Kept apart from ``state``: "not recorded", or the latest release record."""


@dataclass
class _Pr:
    number: int
    url: str
    state: str
    draft: bool
    merged: bool
    head_sha: str


@dataclass
class _Facts:
    """What the ledger says about one attempt beyond the gate's view."""

    gate: AttemptState
    findings: list[tuple[ev.Finding, tuple[str, ...]]] = field(default_factory=list)
    late_urls: list[str] = field(default_factory=list)
    prs: dict[int, _Pr] = field(default_factory=dict)
    branch_pushed: bool = False
    work_recorded: bool = False
    """A candidate or a matching PR was ever recorded, whatever happened after."""
    closed: tuple[State, str] | None = None
    clearings: list[tuple[gate.ClearingBasis, str]] = field(default_factory=list)

    @property
    def latest(self) -> RunId | None:
        last = self.gate.last_fire
        return None if last is None else last.run

    @property
    def not_launched(self) -> bool:
        last = self.gate.last_fire
        return last is not None and last.outcome is LaunchOutcome.NOT_LAUNCHED

    @property
    def work_seen(self) -> bool:
        """GitHub shows a matching PR or a push to the marker branch."""
        return bool(self.prs) or self.work_recorded

    def session_urls(self) -> tuple[str, ...]:
        urls: list[str] = []
        last = self.gate.last_fire
        if last is not None and last.outcome is LaunchOutcome.LAUNCHED and last.session_url:
            urls.append(last.session_url)
        urls += self.late_urls
        for _, found in self.findings:
            urls += found
        return tuple(dict.fromkeys(urls))


@dataclass
class _Ledger:
    view: LedgerView
    facts: dict[AttemptId, _Facts]
    checks: dict[str, tuple[Mapping[str, object], ...]]
    releases: dict[TaskId, str]


def _build(stored: Sequence[StoredEvent]) -> _Ledger:
    view = LedgerView.build(stored)
    facts = {a: _Facts(s) for a, s in view.attempts.items()}
    checks: dict[str, tuple[Mapping[str, object], ...]] = {}
    releases: dict[TaskId, str] = {}
    for item in sorted(stored, key=lambda s: s.seq):
        try:
            _apply(item, facts, checks, releases)
        except (KeyError, TypeError, ValueError):
            # A malformed record counts for nothing.
            continue
    return _Ledger(view, facts, checks, releases)


def _apply(item, facts, checks, releases) -> None:
    e = item.event
    d = e.data
    if e.kind == ledger.CHECKS:
        results = d["results"]
        if (
            isinstance(d["revision"], str)
            and isinstance(results, tuple)
            and all(isinstance(r, Mapping) for r in results)
        ):
            checks[d["revision"]] = results
        return
    if e.kind == ev.RELEASE_STATUS and e.task is not None:
        if d["status"] in ev.RELEASE_STATUSES:
            releases[e.task] = f"{d['status']}: {d['evidence']}"
        return
    f = facts.get(e.attempt) if e.attempt is not None else None
    if f is None:
        return
    on_latest = e.run is not None and e.run == f.latest
    if e.kind == ev.LAUNCH_RECONCILED and on_latest:
        finding = ev.Finding(d["finding"])
        urls = tuple(ev.session_url(u) for u in d["session_urls"])
        f.findings.append((finding, urls))
    elif e.kind == ev.LATE_FIRE_RESULT and on_latest:
        url = d.get("session_url")
        if d.get("outcome") == LaunchOutcome.LAUNCHED.value and isinstance(url, str):
            f.late_urls.append(ev.session_url(url))
    elif e.kind == ev.PR_OBSERVED and d["matches"] is not True:
        # A PR edited so it no longer carries the markers stops counting for
        # the state, unless it was already merged. It still counts as work
        # seen on GitHub (``work_recorded``), so the edit hides nothing.
        number = int(d["number"])
        if number in f.prs and not f.prs[number].merged:
            del f.prs[number]
    elif e.kind == ev.PR_OBSERVED:
        f.work_recorded = True
        number = int(d["number"])
        if number in f.prs and f.prs[number].merged and d["merged"] is not True:
            # GitHub never un-merges a PR: a later "open" is an older snapshot.
            return
        f.prs[number] = _Pr(
            int(d["number"]),
            str(d["url"]),
            str(d["state"]),
            d["draft"] is True,
            d["merged"] is True,
            str(d["head_sha"]),
        )
    elif e.kind == ledger.CANDIDATE:
        f.work_recorded = True
        if "pr_number" not in d:
            f.branch_pushed = True
    elif e.kind == ledger.ATTEMPT_ABANDONED:
        # A plain abandoned record (controller/ledger/records.py) means failed.
        outcome = State(d.get("outcome", State.FAILED.value))
        if outcome in CLOSE_OUTCOMES:
            f.closed = (outcome, str(d["reason"]))
    elif e.kind == gate.ATTEMPT_CLEARED and on_latest:
        basis = gate.ClearingBasis(d["basis"])
        f.clearings.append((basis, str(d[gate.clearing_evidence_field(basis)])))


# --- The writer ---------------------------------------------------------------


def _writer(f: _Facts, now: datetime) -> tuple[str, bool, str | None]:
    """(plain words, cleared, problem with a clearing on record)."""
    last = f.gate.last_fire
    if f.not_launched and not f.work_seen:
        return "none: the fire was definitely not launched", True, None
    known = set(f.session_urls())
    covered: set[str] = set()
    access_removed = accepted = False
    for basis, evidence in f.clearings:
        if basis in (gate.ClearingBasis.COMPLETED, gate.ClearingBasis.TERMINATED):
            covered.update(ev.listed_urls(evidence))
        elif basis is gate.ClearingBasis.WRITE_ACCESS_REMOVED:
            access_removed = True
        elif basis is gate.ClearingBasis.UNRESOLVED_ACCEPTED:
            accepted = True
    if known and known <= covered:
        return "stopped: every session on record was found finished or stopped", True, None
    if access_removed:
        return "cut off: the bot's push access was removed and checked", True, None
    searched = any(finding is ev.Finding.NOT_FOUND for finding, _ in f.findings)
    if accepted and not known and searched:
        return (
            "unknown: never found, and Rolando accepted that it may still exist",
            True,
            None,
        )
    finished = f.gate.finished_by_pr(now)
    if finished is not None and not f.not_launched:
        return f"finished: {finished}", True, None
    if not f.clearings:
        if f.not_launched:
            return (
                "unexplained: GitHub shows work for this attempt although no fire launched",
                False,
                None,
            )
        if last is None or last.outcome is None:
            return "may be starting: the launch response is not recorded", False, None
        return "may still be running", False, None
    problem = _clearing_problem(known, covered, accepted, searched)
    return f"may still be running: {problem}", False, problem


def _clearing_problem(known: set[str], covered: set[str], accepted: bool, searched: bool) -> str:
    if known - covered:
        missing = ", ".join(sorted(known - covered))
        if accepted:
            return f"the exception was recorded although a session is on record ({missing})"
        return f"no completed or terminated record covers {missing}"
    if accepted and not searched:
        return "the exception was recorded without a recorded search for the session"
    return "no session URL is on record for the completed or terminated record to cover"


# --- The state ----------------------------------------------------------------


def _state(
    f: _Facts, checks: Mapping[str, tuple[Mapping[str, object], ...]], now: datetime, wait
) -> tuple[State, str]:
    last = f.gate.last_fire
    prs = sorted(f.prs.values(), key=lambda p: p.number)
    merged = [p for p in prs if p.merged]
    if merged:
        return State.MERGED, f"PR #{merged[0].number} was merged ({merged[0].url})."
    if f.closed is not None:
        outcome, reason = f.closed
        return outcome, f"Closed by Rolando: {reason}"
    opened = [p for p in prs if p.state == "open"]
    if len(opened) > 1:
        numbers = ", ".join(f"#{p.number}" for p in opened)
        return State.AWAITING_HUMAN, f"Several open PRs carry this attempt's markers: {numbers}."
    if opened:
        return _open_pr(opened[0], checks)
    if prs:
        numbers = ", ".join(f"#{p.number}" for p in prs)
        return (
            State.AWAITING_HUMAN,
            f"PR {numbers} was closed without merging. That is not success:"
            " record whether the attempt failed or was canceled.",
        )
    if last is None or last.outcome is None:
        if last is not None and now - last.at < wait:
            return State.DISPATCHING, "The launch call may still be in flight."
        return State.UNKNOWN, "The controller stopped before the launch response was recorded."
    if last.outcome is LaunchOutcome.NOT_LAUNCHED:
        return (
            State.AWAITING_HUMAN,
            f"{last.run} was not launched (HTTP {last.http_status}). Re-firing needs"
            " Rolando's decision; otherwise close the attempt.",
        )
    if last.outcome is LaunchOutcome.OUTCOME_UNKNOWN and not f.session_urls():
        if any(finding is ev.Finding.NOT_FOUND for finding, _ in f.findings):
            return State.UNKNOWN, "No session was found. That does not prove none started."
        return State.UNKNOWN, "No usable launch response; a session may exist."
    if f.branch_pushed:
        return State.RUNNING, "The worker pushed its branch; no PR yet."
    return State.RUNNING, "A session started; no PR found yet."


def _open_pr(pr: _Pr, checks) -> tuple[State, str]:
    draft = "draft " if pr.draft else ""
    results = checks.get(pr.head_sha)
    if not results:
        return State.VERIFYING, f"Open {draft}PR #{pr.number}; no checks recorded for its head."
    conclusions = {str(r.get("conclusion")) for r in results}
    if conclusions & _PENDING:
        return State.VERIFYING, f"Open {draft}PR #{pr.number}; checks still running."
    failed = sorted(str(r.get("name")) for r in results if str(r.get("conclusion")) not in _PASSED)
    if failed:
        return (
            State.AWAITING_HUMAN,
            f"Open {draft}PR #{pr.number}: checks failed ({', '.join(failed)}). Rolando decides"
            " on a repair or closes the attempt.",
        )
    if pr.draft:
        return State.AWAITING_HUMAN, f"Draft PR #{pr.number} passed its checks; awaiting review."
    return State.AWAITING_HUMAN, f"PR #{pr.number} passed its checks; awaiting Rolando's review."


def _next_step(state: State, cleared: bool, problem: str | None, f: _Facts) -> str:
    urls = f.session_urls()
    where = ", ".join(urls) if urls else "the routine's run list on claude.ai"
    if problem is not None:
        return f"The clearing on record does not hold ({problem}). Record a valid one."
    if state is State.DISPATCHING:
        return "Wait: the launch may still be in flight. Run recovery again later."
    if state is State.UNKNOWN:
        return (
            f"Look for the session in {where} and on GitHub (reconcile), record what you found,"
            " then a clearing record. Nothing else starts until then."
        )
    if not cleared:
        return (
            f"A session may still be writing. Check {where}; when it shows finished or stopped,"
            " record a completed or terminated clearing with its URL."
        )
    if state in FINISHED:
        return "Nothing to do."
    if state is State.AWAITING_HUMAN:
        return "Rolando decides: review, repair, re-fire or close."
    return "Wait for the worker's PR and its checks."


# --- Public -------------------------------------------------------------------


def attempt_statuses(
    stored: Sequence[StoredEvent], now: datetime, interrupted_after: timedelta = INTERRUPTED_AFTER
) -> list[AttemptStatus]:
    """Every attempt in the ledger, in task then attempt order."""
    led = _build(stored)
    return [_status(led.facts[a], led, now, interrupted_after) for a in sorted(led.facts)]


def _status(f: _Facts, led: _Ledger, now: datetime, wait: timedelta) -> AttemptStatus:
    words, cleared, problem = _writer(f, now)
    state, detail = _state(f, led.checks, now, wait)
    return AttemptStatus(
        attempt=f.gate.attempt,
        digest=f.gate.digest,
        state=state,
        detail=detail,
        writer=words,
        writer_cleared=cleared,
        latest_run=f.latest,
        session_urls=f.session_urls(),
        pull_requests=tuple(sorted(n for n in f.prs)),
        next_step=_next_step(state, cleared, problem, f),
        problem=problem,
    )


def task_status(
    stored: Sequence[StoredEvent],
    task: TaskId,
    now: datetime,
    interrupted_after: timedelta = INTERRUPTED_AFTER,
) -> TaskStatus:
    led = _build(stored)
    attempts = tuple(
        _status(led.facts[a], led, now, interrupted_after)
        for a in sorted(led.facts)
        if a.task == task
    )
    state = attempts[-1].state if attempts else State.READY
    return TaskStatus(task, state, attempts, led.releases.get(task, "not recorded"))


def interrupted_runs(
    stored: Sequence[StoredEvent], now: datetime, interrupted_after: timedelta = INTERRUPTED_AFTER
) -> list[RunId]:
    """Fires whose intent is recorded with no response, older than ``interrupted_after``."""
    view = LedgerView.build(stored)
    return [f.run for f in view.all_fires if f.outcome is None and now - f.at >= interrupted_after]


def blocks(
    stored: Sequence[StoredEvent], now: datetime, interrupted_after: timedelta = INTERRUPTED_AFTER
) -> tuple[Notice, ...]:
    """What recovery says must stop any new dispatch, factory-wide."""
    out = [
        Notice(
            "launch-interrupted",
            f"{run} was being launched when the controller stopped; run recovery first.",
        )
        for run in interrupted_runs(stored, now, interrupted_after)
    ]
    led = _build(stored)
    for a in sorted(led.facts):
        f = led.facts[a]
        words, cleared, problem = _writer(f, now)
        if problem is not None:
            out.append(Notice("clearing-invalid", f"Attempt {a}: {problem}."))
        elif not cleared and f.not_launched:
            # The gate reads a definite not-launched as settled; this is not.
            out.append(Notice("unexplained-work", f"Attempt {a}: {words}."))
    return tuple(out)


def restore_problems(stored: Sequence[StoredEvent], now: datetime) -> tuple[str, ...]:
    """Why the bot's push access may not be given back yet (ADR 0002 section 6.1):
    every attempt that may have started a session needs a completed or
    terminated record covering every session on record for it."""
    led = _build(stored)
    out = []
    for a in sorted(led.facts):
        f = led.facts[a]
        if f.not_launched and not f.work_seen:
            continue
        known = set(f.session_urls())
        covered = {
            url
            for basis, evidence in f.clearings
            if basis in (gate.ClearingBasis.COMPLETED, gate.ClearingBasis.TERMINATED)
            for url in ev.listed_urls(evidence)
        }
        if not known:
            out.append(f"{a}: no session is on record, so none can be confirmed finished.")
        elif not known <= covered:
            missing = ", ".join(sorted(known - covered))
            out.append(f"{a}: no completed or terminated record covers {missing}.")
    return tuple(out)
