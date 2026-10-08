"""Recovering interrupted runs without ever starting a second writer (ENG-153).

The fire API has no idempotency key and no way to read or stop a session, so
the controller never retries a launch that might have worked and never treats
silence as an ending (ADR 0002 sections 6 and 6.1). What it can do:

- ``recover`` at every start: a fire whose intent is on record with no
  response, from a controller that stopped mid-launch, becomes
  launch-outcome-unknown. It is never fired again.
- ``status``: each attempt's state and whether a session may still be writing,
  from the ledger alone.
- ``reconcile``: read the attempt's marker branch and PRs from GitHub and
  record what is there, so a PR the controller never saved is found, never
  duplicated.
- ``record_launch_finding``: Rolando's account of an unknown launch (the
  session, several, or none found).
- ``clear``: Rolando's signed clearing record, refused unless it meets the
  section 6.1 rules. This is the only thing that frees the single lane after a
  launch that may have started a session.
- ``close_attempt``: an explicit failed or canceled decision. A PR closed
  without merging is never read as success on its own.
- ``blocks``: what dispatch must add to its checks, so a clearing written some
  other way that breaks the rules still stops the next fire.

Every write goes through the ledger's writer lock. Nothing here fires, retries,
creates a PR, or reads a decision from GitHub text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from controller.approval import APPROVER, Approvals, Confirm, tty_confirm
from controller.attempts import AttemptGate
from controller.attempts import events as gate_events
from controller.attempts.policy import LedgerView, Notice
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    LedgerStore,
    RunId,
    StoredEvent,
    TaskId,
)
from controller.ledger import kinds as ledger_kinds
from controller.ledger import records
from controller.ledger.redact import redact_json
from controller.recovery import events as ev
from controller.recovery import state as st
from controller.recovery.github import GitHubReader, GitHubUnreadable, PullRequest

PILOT_REPO = "rNavarrete/factory-pilot-demo"
WORKER_LOGINS = frozenset({"rnavarrete-factory-bot"})
"""The GitHub logins worker sessions push and open PRs as (ENG-183)."""

_URL_BASES = (gate_events.ClearingBasis.COMPLETED, gate_events.ClearingBasis.TERMINATED)
# Approvals.trusted_events filters repair records by the contract being
# dispatched. Recovery never reads repairs, so any digest will do.
_NO_REPAIRS = ContractDigest("0" * 64)


class RecoveryRefused(Exception):
    """A recovery step was not allowed. Nothing was written."""


@dataclass(frozen=True)
class Reconciliation:
    status: st.AttemptStatus
    """The attempt after what was found was recorded."""
    recorded: int
    """How many new events this reconcile wrote (0 when nothing changed)."""
    warnings: tuple[str, ...]
    """PRs or branches that name this attempt but do not match its markers,
    and anything else Rolando should look at."""
    session_hints: tuple[str, ...]
    """Session URLs mentioned in the attempt's PR bodies. Hints for where to
    look, never evidence: PR text is written by the worker."""


class Recovery:
    def __init__(
        self,
        store: LedgerStore,
        approvals: Approvals,
        github: GitHubReader | None = None,
        *,
        repo: str = PILOT_REPO,
        worker_logins: frozenset[str] = WORKER_LOGINS,
        gate: AttemptGate | None = None,
        confirm: Confirm = tty_confirm,
        identity: str = APPROVER,
        interrupted_after: timedelta = st.INTERRUPTED_AFTER,
    ) -> None:
        if interrupted_after <= timedelta(0):
            raise ValueError("interrupted_after must be positive")
        if not worker_logins:
            raise ValueError("name the worker's GitHub login")
        self._store = store
        self._approvals = approvals
        self._github = github
        self._repo = repo
        self._workers = frozenset(worker_logins)
        self._gate = gate or AttemptGate(store)
        self._confirm = confirm
        self._identity = identity
        self._wait = interrupted_after

    # --- reading --------------------------------------------------------------

    def _trusted(self) -> list[StoredEvent]:
        return self._approvals.trusted_events(self._store.events(), _NO_REPAIRS)

    def status(self, task: TaskId, now: datetime) -> st.TaskStatus:
        """Where ``task`` stands, from the ledger alone."""
        _aware(now)
        return st.task_status(self._trusted(), task, now, self._wait)

    def attempt_status(self, attempt: AttemptId, now: datetime) -> st.AttemptStatus:
        _aware(now)
        for s in st.task_status(self._trusted(), attempt.task, now, self._wait).attempts:
            if s.attempt == attempt:
                return s
        raise RecoveryRefused(f"{attempt} was never started")

    def blocks(self, now: datetime) -> tuple[Notice, ...]:
        """Reasons no new fire may start, factory-wide, beyond the gate's own.

        Dispatch adds these to ``Approvals.check`` and ``AttemptGate.reserve``:
        a launch interrupted and not yet recovered, a signed clearing that does
        not meet ADR 0002 section 6.1 (for instance written straight through
        ``Approvals.record_clearing`` with the wrong session), and work on
        GitHub for an attempt none of whose fires launched.
        """
        _aware(now)
        return st.blocks(self._trusted(), now, self._wait)

    def may_restore_bot_access(self, now: datetime) -> tuple[bool, tuple[str, ...]]:
        """ADR 0002 section 6.1: give the bot its push access back only once every
        attempt that may have started a session is confirmed finished or stopped."""
        _aware(now)
        problems = st.restore_problems(self._trusted(), now)
        return not problems, problems

    # --- writing: machine records ----------------------------------------------

    def recover(self, now: datetime) -> list[RunId]:
        """Run at every controller start. Each fire left with no response by a
        controller that stopped mid-launch is recorded as launch-outcome-unknown.
        A younger one is left alone: its launch call may still be in flight.
        Never fires anything. Returns the runs it marked."""
        _aware(now)
        marked = []
        for run in st.interrupted_runs(self._store.events(), now, self._wait):
            result = LaunchResult(
                LaunchOutcome.OUTCOME_UNKNOWN,
                detail="controller stopped before the launch response was recorded;"
                " found by recovery at restart",
            )
            try:
                self._gate.record_launch(run, result, now)
            except ValueError:
                continue  # its own result landed in the meantime
            marked.append(run)
        return marked

    def record_late_result(self, run: RunId, result: LaunchResult, now: datetime) -> StoredEvent:
        """Keep a fire response that arrived after ``recover`` had already marked
        the run unknown (``AttemptGate.record_launch`` refuses it as already
        recorded). A launched one adds its session URL to the record. The gate
        still reads the run as launch-outcome-unknown, so nothing is unblocked."""
        _aware(now)
        with self._store.writer_lock():
            view = LedgerView.build(self._store.events())
            fire = next((f for f in view.all_fires if f.run == run), None)
            if fire is None or fire.outcome is not LaunchOutcome.OUTCOME_UNKNOWN:
                raise RecoveryRefused(f"{run} has no launch-outcome-unknown result to add to")
            (stored,) = self._store.append(ev.late_fire_result(run, result, now))
        return stored

    def reconcile(self, attempt: AttemptId, now: datetime) -> Reconciliation:
        """Read ``attempt``'s marker branch and PRs from GitHub and record what
        changed. Finds a PR the controller never saved (say, it crashed after
        the worker opened it) and records it once. Reads only."""
        _aware(now)
        if self._github is None:
            raise RecoveryRefused("reconcile needs a GitHub reader")
        current = self.attempt_status(attempt, now)
        run = current.latest_run
        if run is None or not current.digest:
            raise RecoveryRefused(f"{attempt} has no recorded fire")
        digest = ContractDigest(current.digest)
        try:
            head = self._github.branch_head(self._repo, attempt.branch)
            pulls = {p.number: p for p in self._github.recent_pulls(self._repo)}
            pulls.update(
                {p.number: p for p in self._github.pulls_for_branch(self._repo, attempt.branch)}
            )
        except GitHubUnreadable as e:
            raise RecoveryRefused(f"GitHub could not be read, nothing recorded: {e}") from e

        warnings: list[str] = []
        seen: list[tuple[PullRequest, tuple[str, ...]]] = []
        for pr in sorted(pulls.values(), key=lambda p: p.number):
            if not self._names(pr, attempt):
                continue
            problems = self._mismatches(pr, attempt, digest)
            seen.append((pr, problems))
            if problems:
                warnings.append(
                    f"PR #{pr.number} ({pr.url}) is not this attempt's: {'; '.join(problems)}."
                )
            elif ContractDigest.from_pr_body(pr.body) != digest:
                warnings.append(
                    f"PR #{pr.number} has no single Contract-Digest line matching {digest.short}."
                )
        matching = [pr for pr, problems in seen if not problems]
        if head is not None and not matching:
            warnings.append(f"Branch {attempt.branch} exists with no matching PR (head {head}).")
        if (head is not None or matching) and current.writer.startswith("none"):
            warnings.append(
                "GitHub shows work for this attempt although none of its fires launched."
                " Find the session it came from before anything else starts."
            )
        hints = tuple(
            dict.fromkeys(
                url
                for pr in matching
                for url in ev.SESSION_URL_RE.findall(pr.body)
                if url not in current.session_urls
            )
        )

        with self._store.writer_lock():
            new = self._new_records(attempt, run, head, seen, now)
            if new:
                self._store.append(*new)
        return Reconciliation(self.attempt_status(attempt, now), len(new), tuple(warnings), hints)

    def _names(self, pr: PullRequest, attempt: AttemptId) -> bool:
        """Whether the PR claims to be this attempt's, by branch or title."""
        parsed = AttemptId.from_pr_title(pr.title)
        return pr.head_branch == attempt.branch or (parsed is not None and parsed[0] == attempt)

    def _mismatches(
        self, pr: PullRequest, attempt: AttemptId, digest: ContractDigest
    ) -> tuple[str, ...]:
        out = []
        if pr.head_branch != attempt.branch:
            out.append(f"its branch is {pr.head_branch!r}, not {attempt.branch!r}")
        if pr.head_repo != self._repo:
            out.append(f"its branch lives in {pr.head_repo or 'a deleted repository'}")
        parsed = AttemptId.from_pr_title(pr.title)
        if parsed is None or parsed[0] != attempt or parsed[1] != digest.short:
            out.append(f"its title does not start with {attempt.pr_title_marker(digest)}")
        if pr.author not in self._workers:
            out.append(f"it was opened by {pr.author}, not the worker")
        return tuple(out)

    def _new_records(
        self,
        attempt: AttemptId,
        run: RunId,
        head: str | None,
        seen: Sequence[tuple[PullRequest, tuple[str, ...]]],
        now: datetime,
    ) -> list[LedgerEvent]:
        """The events not already in the ledger, read afresh under the lock."""
        observed: dict[int, Mapping[str, object]] = {}
        candidates: set[tuple[int | None, str]] = set()
        for s in self._store.events(attempt.task):
            e = s.event
            if e.attempt != attempt:
                continue
            if e.kind == ev.PR_OBSERVED:
                observed[int(e.data["number"])] = e.data
            elif e.kind == ledger_kinds.CANDIDATE:
                number = e.data.get("pr_number")
                candidates.add(
                    (number if isinstance(number, int) else None, str(e.data["candidate_commit"]))
                )
        new: list[LedgerEvent] = []
        for pr, problems in seen:
            data = _observation(pr, problems)
            old = observed.get(pr.number)
            # Compared as the ledger stores it: secret-looking text is redacted.
            if old is None or _thaw(old) != redact_json(data):
                new.append(LedgerEvent(ev.PR_OBSERVED, now, attempt.task, attempt, run, data))
            if not problems and (pr.number, pr.head_sha) not in candidates:
                new.append(
                    records.candidate(
                        run, attempt.branch, pr.head_sha, now, pr_number=pr.number, pr_url=pr.url
                    )
                )
                candidates.add((pr.number, pr.head_sha))
        recorded_shas = {sha for _, sha in candidates}
        if head is not None and head not in recorded_shas:
            new.append(records.candidate(run, attempt.branch, head, now))
        return new

    # --- writing: Rolando's records ------------------------------------------

    def record_launch_finding(
        self,
        run: RunId,
        finding: ev.Finding,
        session_urls: Iterable[str],
        how_checked: str,
        now: datetime,
    ) -> StoredEvent:
        """What Rolando found when he looked for the session of an unknown
        launch (ADR 0002 section 6). ``not-found`` is recorded as what he
        checked, never as proof: the attempt stays unresolved."""
        _aware(now)
        try:
            event = ev.launch_reconciled(
                run, finding, tuple(session_urls), how_checked, self._identity, now
            )
        except ValueError as e:
            raise RecoveryRefused(str(e)) from None
        current = self.attempt_status(run.attempt, now)
        if current.latest_run != run:
            raise RecoveryRefused(f"{run} is not {run.attempt}'s latest fire")
        launch = self._launch_outcome(run)
        if launch is None:
            raise RecoveryRefused(f"{run} has no recorded response yet; run recovery first")
        if launch is LaunchOutcome.LAUNCHED:
            raise RecoveryRefused(f"{run} launched and its session is on record")
        if launch is LaunchOutcome.NOT_LAUNCHED and current.writer.startswith("none"):
            raise RecoveryRefused(f"{run} was definitely not launched; there is no session")
        urls = ", ".join(event.data["session_urls"]) or "none"
        self._ask(
            f"Record for {run}: {finding.value}. Sessions: {urls}.\nHow checked: {how_checked}",
            str(run),
        )
        with self._store.writer_lock():
            (stored,) = self._store.append(event)
        return stored

    def clear(
        self,
        attempt: AttemptId,
        basis: gate_events.ClearingBasis,
        now: datetime,
        *,
        session_urls: Iterable[str] = (),
        note: str = "",
    ) -> StoredEvent:
        """Rolando's clearing record for ``attempt``'s latest fire, signed by
        ``Approvals.record_clearing`` once it meets ADR 0002 section 6.1:

        - completed / terminated: ``session_urls`` must be exactly the sessions
          on record for the fire (one, or every duplicate), each seen finished
          or stopped.
        - write-access-removed: ``note`` says how it was checked, and GitHub,
          read now, lists none of the worker logins with push access.
        - unresolved-accepted: Rolando's exception (``note``) for a session that
          was searched for (a not-found finding) and never found. Refused while
          any session URL is on record: that one can be checked.
        """
        _aware(now)
        current = self.attempt_status(attempt, now)
        urls = tuple(session_urls)
        if current.latest_run is None or self._launch_outcome(current.latest_run) is None:
            raise RecoveryRefused(
                f"{attempt}'s launch response is not recorded; run recovery first"
            )
        if current.writer.startswith("none"):
            raise RecoveryRefused(f"{attempt} was definitely not launched; nothing to clear")
        if basis in _URL_BASES:
            evidence = self._session_evidence(current, urls, note)
        elif urls:
            raise RecoveryRefused(f"a {basis.value} clearing takes no session URLs")
        elif not note.strip():
            raise RecoveryRefused(f"a {basis.value} clearing needs a note")
        elif basis is gate_events.ClearingBasis.WRITE_ACCESS_REMOVED:
            evidence = self._access_evidence(note, now)
        else:
            if current.session_urls:
                raise RecoveryRefused(
                    "a session is on record for this attempt; check it and record it completed"
                    " or terminated, or remove the bot's push access"
                )
            if not self._searched(current.latest_run):
                raise RecoveryRefused(
                    "record the search first (a not-found finding): the exception covers a"
                    " session that was looked for and never found"
                )
            evidence = note.strip()
        return self._approvals.record_clearing(attempt, basis, evidence, now)

    def _session_evidence(self, current: st.AttemptStatus, urls: tuple[str, ...], note: str) -> str:
        if not current.session_urls:
            raise RecoveryRefused(
                "no session URL is on record for this attempt; record what you found first"
            )
        try:
            given = [ev.session_url(u) for u in urls]
        except ValueError as e:
            raise RecoveryRefused(str(e)) from None
        if len(set(given)) != len(given) or set(given) != set(current.session_urls):
            raise RecoveryRefused(
                "list exactly the sessions on record, each checked: "
                + ", ".join(current.session_urls)
            )
        evidence = " ".join(given)
        return f"{evidence} (note: {note.strip()})" if note.strip() else evidence

    def _access_evidence(self, note: str, now: datetime) -> str:
        if self._github is None:
            raise RecoveryRefused("a write-access-removed clearing needs GitHub to confirm it")
        try:
            still = sorted(w for w in self._workers if self._github.can_push(self._repo, w))
        except GitHubUnreadable as e:
            raise RecoveryRefused(f"GitHub could not confirm the bot lost push access: {e}") from e
        if still:
            raise RecoveryRefused(
                f"GitHub still lists {', '.join(still)} with push access to {self._repo}."
                " Remove the bot as a collaborator first."
            )
        workers = ", ".join(sorted(self._workers))
        return (
            f"{note.strip()} Confirmed by the controller at {now.isoformat()}: GitHub lists"
            f" no push access for {workers} on {self._repo}."
        )

    def close_attempt(
        self, attempt: AttemptId, outcome: st.State, reason: str, now: datetime
    ) -> StoredEvent:
        """Rolando's explicit decision that an attempt failed or was canceled.
        It ends the work, not the session: a session that may still be writing
        still needs a clearing record."""
        _aware(now)
        if outcome not in st.CLOSE_OUTCOMES:
            raise RecoveryRefused("an attempt closes as failed or canceled")
        if not reason.strip():
            raise RecoveryRefused("say why the attempt is closed")
        current = self.attempt_status(attempt, now)
        if current.state is st.State.MERGED:
            raise RecoveryRefused(f"{attempt}'s PR was merged; it cannot be closed as failed")
        if current.state in st.CLOSE_OUTCOMES:
            raise RecoveryRefused(f"{attempt} is already {current.state.value}")
        self._ask(f"Close {attempt} as {outcome.value}: {reason}", str(attempt))
        event = LedgerEvent(
            ledger_kinds.ATTEMPT_ABANDONED,
            now,
            attempt.task,
            attempt,
            data={"reason": reason, "recorded_by": self._identity, "outcome": outcome.value},
        )
        with self._store.writer_lock():
            (stored,) = self._store.append(event)
        return stored

    def record_release(
        self, task: TaskId, status: str, evidence: str, now: datetime
    ) -> StoredEvent:
        """Whether a merged task was released, kept apart from its state."""
        _aware(now)
        if self.status(task, now).state is not st.State.MERGED:
            raise RecoveryRefused(f"{task} has no merged attempt to release")
        try:
            event = ev.release_status(task, status, evidence, self._identity, now)
        except ValueError as e:
            raise RecoveryRefused(str(e)) from None
        with self._store.writer_lock():
            (stored,) = self._store.append(event)
        return stored

    # --- internals ------------------------------------------------------------

    def _launch_outcome(self, run: RunId) -> LaunchOutcome | None:
        view = LedgerView.build(self._store.events(run.attempt.task))
        fire = next((f for f in view.all_fires if f.run == run), None)
        return None if fire is None else fire.outcome

    def _searched(self, run: RunId) -> bool:
        return any(
            s.event.kind == ev.LAUNCH_RECONCILED
            and s.event.run == run
            and s.event.data.get("finding") == ev.Finding.NOT_FOUND.value
            for s in self._store.events(run.attempt.task)
        )

    def _ask(self, summary: str, code: str) -> None:
        if not self._confirm(summary, code):
            raise RecoveryRefused("not confirmed at the terminal; nothing was recorded")


def _observation(pr: PullRequest, problems: tuple[str, ...]) -> dict[str, object]:
    return {
        "number": pr.number,
        "url": pr.url,
        "title": pr.title[:300],
        "head_branch": pr.head_branch,
        "head_sha": pr.head_sha,
        "head_repo": pr.head_repo,
        "author": pr.author,
        "state": pr.state,
        "draft": pr.draft,
        "merged": pr.merged,
        "merge_commit": pr.merge_commit,
        "matches": not problems,
        "problems": list(problems),
    }


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(v) for v in value]
    return value


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("times must be timezone-aware")
