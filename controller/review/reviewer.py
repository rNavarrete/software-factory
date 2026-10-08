"""The automatic independent review of a worker PR (ENG-156).

When the service sees an attempt's PR it calls ``AutoReviewer.start``; after
that, ``check`` is called each round. Every call reads the PR again from
GitHub, so nothing is judged on an old picture:

1. Collect the PR through ``controller.loop.collect`` (destination branch,
   state, markers, trusted CI run, changed files) for the approved contract.
   A PR that can't be judged (closed, retargeted, wrong markers) is reported
   to Rolando at once and no review job is started.
2. The exact revision (contract digest, head, base and merge base, plus the
   PR and this policy's version) gives the request key. The same key always
   means the same review: it is launched at most once, and a verdict already
   recorded for it is reused.
3. While CI runs, wait. When CI or a gate (scope, base, markers, checks) has
   already failed, the verdict is "failed" right away, with findings routed to
   the repair worker, and no review job is spent on it.
4. Otherwise start one review job for the key, within the allowances: one
   full pass and one verification pass per review cycle (a task's approved
   contract) unless the policy allows more, and the factory's shared weekly
   fire allowance, usage reading and holds. The claim is written to the
   ledger before the launch, so a crash or a second reviewer never launches
   it twice; a launch whose answer was lost is never repeated.
5. The job posts its mapping, failure proofs and findings as one comment from
   the reviewer's own account. Only that account counts: not the worker, not
   an edited comment, not a comment for another revision (``verify.review``).
   The two verifiers then decide: passed, failed (findings for the repair
   worker), or needs Rolando (a behavior only he can check, a protected
   control, a product or security question).

The verdict names the exact commit it covers. A new push or a moved base is a
new key: the old verdict stays in the ledger for that commit and says nothing
about the new one. A pass is evidence for Rolando, never an approval: the
reviewer can't approve, merge or release, and nothing here writes a decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from controller.adapter.routine import LaunchInterrupted
from controller.attempts import events as aev
from controller.attempts.events import ATTEMPT_RESERVED
from controller.attempts.gate import _EXHAUSTED_RE, _MAX_RETRY_AFTER
from controller.attempts.limits import PILOT_LIMITS, Limits
from controller.attempts.policy import LedgerView, Notice, _check_snapshot, _check_window
from controller.interfaces import (
    LAUNCH_TIMEOUT_SECONDS,
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    LedgerLocked,
    LedgerStore,
)
from controller.loop.check import Assessment, assess
from controller.loop.collect import PILOT_REPO, Collected, GitHubApi, collect
from controller.review import events as ev
from controller.review.runtime import ReviewJob, ReviewRuntime, ReviewTooLarge, review_text
from controller.service.seams import PullRequestRef
from verify.criteria import DEFAULT_POLICY, TrustPolicy, _login_key
from verify.findings import (
    Finding,
    Route,
    adopt_ids,
    blocking,
    carry_forward,
    finding_from_data,
    for_route,
    from_reports,
)

POLICY_VERSION = "1"


class ReviewState(Enum):
    WAITING_CI = "waiting-for-ci"
    RUNNING = "running"
    """A review job is out; its result isn't posted yet."""
    PASSED = "passed"
    FAILED = "failed"
    """Routine technical findings: the repair worker's to fix."""
    NEEDS_ROLANDO = "needs-rolando"
    """Only Rolando's judgment can settle what is left."""
    BLOCKED = "blocked"
    """No review job may start now (allowance used, hold, usage reading)."""
    UNKNOWN = "unknown"
    """The review can't say: its launch was lost, or it never reported."""
    NOT_REVIEWABLE = "not-reviewable"
    """The PR can't be judged as it is (closed, retargeted, wrong markers)."""


FINAL = frozenset({ReviewState.PASSED, ReviewState.FAILED, ReviewState.NEEDS_ROLANDO})


@dataclass(frozen=True)
class ReviewPolicy:
    reviewers: frozenset[str] = frozenset()
    """The GitHub accounts review jobs post as. Only their comments count.
    Empty means no reviewer is set up: nothing posted can count, and no job is
    launched. Never the worker's account, and never one that isn't a real
    GitHub login."""
    passes_per_cycle: int = 2
    """One full review and one verification pass per review cycle."""
    extra_passes: int = 0
    """More passes, only by an explicit allowance in the project or task policy
    or a recorded decision; still within every other allowance."""
    launches_per_job: int = 2
    """A job is launched again only after a definite not-launched answer."""
    review_timeout: timedelta = timedelta(hours=2)
    """After this, a launched job that hasn't posted is reported as unknown."""
    trust: TrustPolicy = DEFAULT_POLICY

    def __post_init__(self) -> None:
        for login in self.reviewers:
            if _login_key(login) is None or login.lower().endswith("[bot]"):
                raise ValueError(f"reviewer {login!r} is not a GitHub user login")
            if self.trust.is_worker(login):
                raise ValueError(f"reviewer {login!r} is a worker account")
            if self.trust.is_reviewer(login):
                # Rolando's own account: a review posted as him would read as his.
                raise ValueError(f"reviewer {login!r} is Rolando's account, not an independent one")
        if self.passes_per_cycle < 1 or self.extra_passes < 0 or self.launches_per_job < 1:
            raise ValueError("review allowances must be positive")

    @property
    def max_passes(self) -> int:
        return self.passes_per_cycle + self.extra_passes

    @property
    def reviewer(self) -> str:
        """The login the next job is told to post as."""
        return sorted(self.reviewers)[0] if self.reviewers else ""


@dataclass(frozen=True)
class Revision:
    repository: str
    pr: int
    digest: str
    head: str
    base: str
    merge_base: str

    def key(self, version: str = POLICY_VERSION) -> str:
        raw = json.dumps(
            [version, self.repository, self.pr, self.digest, self.head, self.base, self.merge_base]
        )
        return "rv-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ReviewStatus:
    state: ReviewState
    attempt: AttemptId
    pr: int
    note: str
    """One or two plain sentences for Rolando and the ledger."""
    key: str | None = None
    revision: Revision | None = None
    findings: tuple[Finding, ...] = ()
    review_url: str | None = None
    reviewer: str | None = None
    pass_kind: str | None = None
    blocks: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.state is ReviewState.PASSED

    @property
    def reviewed_commit(self) -> str:
        """The exact commit a final verdict covers; empty without one."""
        if self.state in FINAL and self.revision is not None:
            return self.revision.head
        return ""

    def for_repair(self) -> tuple[Finding, ...]:
        """What the repair worker (ENG-160) gets: routine technical findings."""
        return for_route(self.findings, Route.REPAIR)

    def for_rolando(self) -> tuple[Finding, ...]:
        """What only Rolando can settle, for Linear (ENG-178)."""
        return for_route(self.findings, Route.ROLANDO)

    def unresolved(self) -> tuple[str, ...]:
        return tuple(f.summary for f in blocking(self.findings))


DEFAULT_REVIEW_POLICY = ReviewPolicy()

ContractSource = Callable[[AttemptId], tuple[Mapping[str, object], ContractDigest]]
DecisionSource = Callable[[AttemptId, ContractDigest, object], tuple[Sequence, Sequence]]


def ledger_contracts(
    store: LedgerStore, load: Callable[[ContractDigest], Mapping]
) -> ContractSource:
    """The attempt's contract as the attempt gate reserved it, never the PR's claim."""

    def source(attempt: AttemptId) -> tuple[Mapping[str, object], ContractDigest]:
        for s in reversed(store.events(attempt.task)):
            e = s.event
            if e.kind == ATTEMPT_RESERVED and e.attempt == attempt:
                digest = ContractDigest(str(e.data["digest"]))
                return load(digest), digest
        raise LookupError(f"no reserved attempt {attempt} in the ledger")

    return source


def _no_decisions(attempt, digest, candidate) -> tuple[Sequence, Sequence]:
    return (), ()


class AutoReviewer:
    """The service's ``ReviewStarter`` (controller/service/seams.py), plus
    ``check`` for each later round. All state is in the ledger."""

    def __init__(
        self,
        store: LedgerStore,
        api: GitHubApi,
        runtime: ReviewRuntime,
        contracts: ContractSource,
        *,
        policy: ReviewPolicy = DEFAULT_REVIEW_POLICY,
        decisions: DecisionSource = _no_decisions,
        limits: Limits = PILOT_LIMITS,
        repo: str = PILOT_REPO,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._api = api
        self._runtime = runtime
        self._contracts = contracts
        self._policy = policy
        self._decisions = decisions
        self._limits = limits
        self._repo = repo
        self._now = clock
        self._unwritten: dict[str, list] = {}
        """Launch answers not yet in the ledger because another process held
        the writer lock; written on the next call."""

    # --- the service seam -----------------------------------------------------------

    def start(self, pr: PullRequestRef, key: str) -> str:
        """Record that the review of this attempt's PR was asked for, then run
        the first check. Idempotent per ``key``: a repeat finds the watch
        already recorded and starts nothing new. Raises LedgerLocked if the
        watch can't be written yet (the service tries again next round)."""
        now = self._now()
        with self._store.writer_lock():
            view = self._view()
            if pr.attempt not in view.watches:
                self._store.append(ev.watch(pr.attempt, pr.number, key, now))
        try:
            return self.check(pr.attempt).note
        except Exception as e:  # the watch stands; the next round checks again
            return f"The independent review of PR #{pr.number} is queued ({_clip(e)})."

    def evidence(self, attempt: AttemptId) -> ReviewStatus | None:
        """The latest recorded verdict for the attempt, from the ledger only (no
        GitHub reads): what a merge record should cite."""
        view = self._view()
        records = [v for v in view.verdicts.values() if v.attempt == attempt]
        if not records:
            return None
        latest = max(records, key=lambda v: v.seq)
        return self._from_record(latest, view)

    def merged_head(self, attempt: AttemptId) -> str:
        """The last commit of the attempt's PR, read from GitHub, once it is
        merged; empty otherwise or when GitHub can't be read. This is the
        commit a merge record compares the review's commit with (never the
        merge or squash commit, which no review saw)."""
        watch = self._view().watches.get(attempt)
        if watch is None:
            return ""
        try:
            pr = self._api.json(f"repos/{self._repo}/pulls/{watch[0]}")
        except Exception:
            return ""
        if not isinstance(pr, dict) or pr.get("merged") is not True:
            return ""
        sha = (pr.get("head") or {}).get("sha")
        return sha if isinstance(sha, str) else ""

    # --- one round ------------------------------------------------------------------

    def check(self, attempt: AttemptId) -> ReviewStatus:
        """Where the review of the attempt's PR stands now. Raises
        GitHubUnreadable when GitHub can't be read (try again next round)."""
        self._flush_unwritten()
        view = self._view()
        if attempt not in view.watches:
            raise LookupError(f"no review was asked for {attempt}")
        number, _ = view.watches[attempt]
        contract, digest = self._contracts(attempt)
        collected = collect(
            self._api,
            contract,
            digest,
            attempt,
            number,
            repo=self._repo,
            policy=self._policy.trust,
        )
        cand = collected.candidate
        cycle = ev.cycle_of(attempt.task, digest.value)
        if cand is None:
            rev = Revision(self._repo, number, digest.value, "", "", "")
        else:
            rev = Revision(
                self._repo,
                number,
                digest.value,
                cand.head_commit,
                cand.base_commit,
                cand.merge_base,
            )
        key = rev.key()
        previous = self._previous_findings(view, cycle, key)
        if cand is None or collected.problems:
            found = from_reports(None, None, commit=rev.head, problems=collected.problems)
            if cand is None or any(f.route is not Route.REPAIR for f in found):
                return self._not_reviewable(attempt, cycle, rev, key, collected, view, previous)
            # Only what a repair push fixes (failed CI, a marker): fail now, no job.
            return self._record(
                attempt,
                cycle,
                rev,
                key,
                ReviewState.FAILED,
                carry_forward(previous, found, evaluated=False),
                view,
                "PR #{n} fails before any review is needed: {why}",
            )
        if collected.pending:
            # Recorded, so an earlier verdict for another revision is no
            # longer the latest evidence for this attempt.
            return self._record(
                attempt,
                cycle,
                rev,
                key,
                ReviewState.WAITING_CI,
                carry_forward(previous, (), evaluated=False),
                view,
                "Waiting for CI on `{head}` before reviewing PR #{n}.",
            )
        observations, clearances = self._decisions(attempt, digest, cand)
        a = assess(
            contract,
            digest,
            collected,
            observations=observations,
            clearances=clearances,
            mappers=self._policy.reviewers,
        )
        findings = self._findings(a, rev.head, previous)
        if a.definite_failure:
            repair = any(f.route is Route.REPAIR for f in blocking(findings))
            return self._record(
                attempt,
                cycle,
                rev,
                key,
                ReviewState.FAILED if repair else ReviewState.NEEDS_ROLANDO,
                findings,
                view,
                "PR #{n} fails before any review is needed: {why}"
                if repair
                else "PR #{n} can't go further without you: {why}",
                review_url=a.review.url,
                author=a.review.author,
                blockers=a.blockers,
            )
        if a.review.url is None:
            return self._without_review(attempt, cycle, rev, key, contract, findings, view, a)
        return self._decide(attempt, cycle, rev, key, findings, a, view)

    # --- deciding -----------------------------------------------------------------

    def _findings(self, a: Assessment, head: str, previous: Sequence[Finding]) -> tuple:
        reviewed = a.review.url is not None
        current = from_reports(
            a.criteria,
            a.assertions,
            commit=head,
            observable=a.observable,
            mapped=reviewed,
        )
        mine = adopt_ids(a.review.findings, previous)
        return carry_forward(previous, current + mine, evaluated=reviewed)

    def _decide(self, attempt, cycle, rev, key, findings, a: Assessment, view) -> ReviewStatus:
        open_ = blocking(findings)
        if any(f.route is Route.REPAIR for f in open_):
            state = ReviewState.FAILED
            text = "The independent review of PR #{n} found problems for the repair worker: {why}"
        elif open_:
            state = ReviewState.NEEDS_ROLANDO
            text = "The independent review of PR #{n} needs your judgment: {why}"
        elif a.ready:
            state = ReviewState.PASSED
            text = "The independent review of PR #{n} passed on `{head}`."
        else:
            # Ready is false but nothing blocking was found: say so rather than pass.
            state = ReviewState.UNKNOWN
            text = "The independent review of PR #{n} couldn't decide: {why}"
        return self._record(
            attempt,
            cycle,
            rev,
            key,
            state,
            findings,
            view,
            text,
            review_url=a.review.url,
            author=a.review.author,
            blockers=a.blockers,
        )

    def _record(
        self,
        attempt,
        cycle,
        rev,
        key,
        state,
        findings,
        view,
        text,
        *,
        review_url: str | None = None,
        author: str | None = None,
        blockers: Sequence[str] = (),
    ) -> ReviewStatus:
        why = "; ".join(f.summary for f in blocking(findings)[:3]) or "; ".join(blockers[:3])
        note = text.format(n=rev.pr, head=rev.head[:12], why=why or "no detail")
        latest = view.verdicts.get(key)
        data = [f.as_data() for f in findings]
        same = (
            latest is not None
            and latest.verdict == state.value
            and latest.review_url == review_url
            and [(f["id"], f["resolved"]) for f in latest.findings]
            == [(f["id"], f["resolved"]) for f in data]
        )
        if not same:
            event = ev.verdict(
                attempt,
                key=key,
                cycle=cycle,
                value=state.value,
                head=rev.head,
                base=rev.base,
                merge_base=rev.merge_base,
                digest=rev.digest,
                pr=rev.pr,
                review_url=review_url,
                findings=data,
                now=self._now(),
            )
            try:
                with self._store.writer_lock():
                    self._store.append(event)
            except LedgerLocked:
                pass  # recomputed and written on a later round
        job = view.jobs.get(key)
        return ReviewStatus(
            state,
            attempt,
            rev.pr,
            note,
            key,
            rev,
            tuple(findings),
            review_url,
            author,
            job.pass_kind if job else None,
        )

    # --- no review posted yet -------------------------------------------------------

    def _without_review(
        self, attempt, cycle, rev, key, contract, findings, view, a: Assessment
    ) -> ReviewStatus:
        n = rev.pr
        common = dict(key=key, revision=rev, findings=tuple(findings))
        if not self._policy.reviewers:
            return ReviewStatus(
                ReviewState.BLOCKED,
                attempt,
                n,
                "No reviewer account is set up, so the factory can't start an independent"
                " review (docs/review.md).",
                blocks=("no reviewer account",),
                **common,
            )
        job = view.jobs.get(key)
        now = self._now()
        if any(v.key == key and v.review_url for v in view.by_cycle.get(cycle, [])):
            # The comment a verdict rested on is gone or no longer counts.
            self._note_unknown(attempt, cycle, rev, key, findings, view)
            return ReviewStatus(
                ReviewState.UNKNOWN,
                attempt,
                n,
                f"The review comment on PR #{n} that the last verdict rested on can no longer"
                " be read or trusted, so that verdict no longer stands.",
                **common,
            )
        if job is not None:
            if job.unanswered:
                stale = now - job.claims[-1] > timedelta(seconds=LAUNCH_TIMEOUT_SECONDS + 120)
                if not stale:
                    return ReviewStatus(
                        ReviewState.RUNNING,
                        attempt,
                        n,
                        f"A review job for PR #{n} is being started.",
                        pass_kind=job.pass_kind,
                        **common,
                    )
                self._write_launch(
                    attempt,
                    key,
                    _Answer(
                        LaunchOutcome.OUTCOME_UNKNOWN,
                        detail="the reviewer stopped before recording the launch's answer",
                    ),
                )
                return self._lost(attempt, n, job, common)
            if job.outcome == LaunchOutcome.OUTCOME_UNKNOWN.value:
                return self._lost(attempt, n, job, common)
            if job.outcome == LaunchOutcome.LAUNCHED.value:
                started = job.launched_at or job.claims[-1]
                if now - started > self._policy.review_timeout:
                    timeout = ReviewStatus(
                        ReviewState.UNKNOWN,
                        attempt,
                        n,
                        f"The review job for PR #{n} hasn't posted a result in"
                        f" {_hours(self._policy.review_timeout)}. The factory won't start it"
                        " again by itself; it will still read a result if one is posted.",
                        pass_kind=job.pass_kind,
                        **common,
                    )
                    self._note_unknown(attempt, cycle, rev, key, findings, view)
                    return timeout
                return ReviewStatus(
                    ReviewState.RUNNING,
                    attempt,
                    n,
                    f"The independent review of PR #{n} is running ({job.pass_kind} pass).",
                    pass_kind=job.pass_kind,
                    **common,
                )
            # Definitely not launched: it may be launched again, within its allowance.
        return self._launch(attempt, cycle, rev, key, contract, findings, common)

    def _lost(self, attempt, n, job, common) -> ReviewStatus:
        return ReviewStatus(
            ReviewState.UNKNOWN,
            attempt,
            n,
            f"It is unclear whether the review job for PR #{n} started. The factory won't"
            " start it again by itself; it will still read a result if one is posted.",
            pass_kind=job.pass_kind,
            **common,
        )

    def _note_unknown(self, attempt, cycle, rev, key, findings, view) -> None:
        latest = view.verdicts.get(key)
        if latest is not None and latest.verdict == ReviewState.UNKNOWN.value:
            return
        try:
            with self._store.writer_lock():
                self._store.append(
                    ev.verdict(
                        attempt,
                        key=key,
                        cycle=cycle,
                        value=ReviewState.UNKNOWN.value,
                        head=rev.head,
                        base=rev.base,
                        merge_base=rev.merge_base,
                        digest=rev.digest,
                        pr=rev.pr,
                        review_url=None,
                        findings=[f.as_data() for f in findings],
                        now=self._now(),
                    )
                )
        except LedgerLocked:
            pass

    def _launch(self, attempt, cycle, rev, key, contract, findings, common) -> ReviewStatus:
        n = rev.pr
        with self._store.writer_lock():
            stored = self._store.events()
            view = ev.ReviewView.build(stored)
            job = view.jobs.get(key)
            if job is not None and (job.unanswered or job.outcome != "not-launched"):
                # Someone else claimed it between our read and the lock.
                return ReviewStatus(
                    ReviewState.RUNNING,
                    attempt,
                    n,
                    f"A review job for PR #{n} was already started.",
                    pass_kind=job.pass_kind,
                    **common,
                )
            blocks = self._blocks(stored, view, cycle, job)
            if blocks:
                return ReviewStatus(
                    ReviewState.BLOCKED,
                    attempt,
                    n,
                    f"The independent review of PR #{n} can't start now: {'; '.join(blocks)}.",
                    blocks=tuple(blocks),
                    **common,
                )
            jobs = view.cycle_jobs(cycle)
            number = job.number if job else len(jobs) + 1
            previous = self._previous_findings(view, cycle, key)
            # A verification pass checks an earlier review's findings on the new revision.
            reviewed = any(v.review_url for v in view.by_cycle.get(cycle, []))
            kind = ev.PASS_VERIFY if number > 1 or reviewed else ev.PASS_FULL
            try:
                text = review_text(
                    ReviewJob(
                        key=key,
                        pass_kind=kind,
                        repository=rev.repository,
                        pr=n,
                        pr_url=f"https://github.com/{rev.repository}/pull/{n}",
                        contract_digest=rev.digest,
                        head=rev.head,
                        base=rev.base,
                        merge_base=rev.merge_base,
                        reviewer=self._policy.reviewer,
                        contract=contract,
                        previous_findings=[f.as_data() for f in previous if not f.resolved],
                    )
                )
            except ReviewTooLarge as e:
                return ReviewStatus(
                    ReviewState.BLOCKED,
                    attempt,
                    n,
                    f"The review job for PR #{n} is too large to send: {e}.",
                    blocks=("review job too large",),
                    **common,
                )
            self._store.append(
                ev.intent(
                    attempt,
                    key=key,
                    cycle=cycle,
                    pass_kind=kind,
                    number=number,
                    pr=n,
                    head=rev.head,
                    base=rev.base,
                    merge_base=rev.merge_base,
                    digest=rev.digest,
                    now=self._now(),
                )
            )
        try:
            result = self._fire(text)
        except LaunchInterrupted as e:  # Ctrl-C mid-launch: record its answer, then stop
            r = e.result
            self._write_launch(attempt, key, _Answer(r.outcome, r.http_status, r.session_url))
            raise
        self._write_launch(attempt, key, result)
        if result.outcome is LaunchOutcome.LAUNCHED:
            note = f"The independent review of PR #{n} has started ({kind} pass)."
            state = ReviewState.RUNNING
        elif result.outcome is LaunchOutcome.NOT_LAUNCHED:
            note = f"The review job for PR #{n} did not start ({result.detail}); it will be"
            note += " tried again within its allowance."
            state = ReviewState.BLOCKED
        else:
            return self._lost(
                attempt, n, ev.ReviewView.build(self._store.events()).jobs[key], common
            )
        return ReviewStatus(state, attempt, n, note, pass_kind=kind, **common)

    def _fire(self, text: str) -> _Answer:
        try:
            r: LaunchResult = self._runtime.launch(text)
        except (LookupError, ValueError) as e:  # refused before anything was sent
            return _Answer(LaunchOutcome.NOT_LAUNCHED, detail=_clip(e))
        except Exception as e:
            return _Answer(LaunchOutcome.OUTCOME_UNKNOWN, detail=_clip(e))
        return _Answer(
            r.outcome,
            r.http_status,
            r.session_url,
            r.detail,
            r.retry_after_seconds,
            r.response_body,
        )

    def _write_launch(self, attempt, key, result: _Answer) -> None:
        now = self._now()
        events = [
            ev.launched(
                attempt,
                key,
                result.outcome,
                now,
                http_status=result.http_status,
                session_url=result.session_url,
                detail=result.detail,
            )
        ]
        if result.http_status == 429:
            # The same wait the attempt gate records, so workers wait too.
            if result.retry_after_seconds is not None:
                wait = timedelta(seconds=min(max(result.retry_after_seconds, 0), _MAX_RETRY_AFTER))
            else:
                wait = self._limits.rate_limit_default_wait
            events.append(
                LedgerEvent(
                    aev.RATE_LIMIT_WAIT,
                    now,
                    data={"not_before": (now + wait).isoformat(), "review_job": key},
                )
            )
        body = result.response_body or ""
        if result.http_status != 200 and _EXHAUSTED_RE.search(body):
            # Usage is used up: hold the whole factory, as a worker launch would.
            events.append(
                LedgerEvent(
                    aev.HOLD_SET,
                    now,
                    data={
                        "reason": aev.SUBSCRIPTION_EXHAUSTED,
                        "note": f"Set automatically from the response to review job {key}.",
                        "automatic": True,
                    },
                )
            )
        try:
            with self._store.writer_lock():
                self._store.append(*events)
        except LedgerLocked:
            self._unwritten.setdefault(key, []).extend(events)

    def _flush_unwritten(self) -> None:
        if not self._unwritten:
            return
        try:
            with self._store.writer_lock():
                for events in self._unwritten.values():
                    self._store.append(*events)
                self._unwritten.clear()
        except LedgerLocked:
            pass

    def _blocks(self, stored, view: ev.ReviewView, cycle: str, job) -> list[str]:
        """Why no review job may start now. Shares every factory allowance with
        the workers: holds, the 429 wait, the usage reading and the weekly fire
        count (review launches count there too)."""
        gate = LedgerView.build(stored)
        now = self._now()
        notices: list[Notice] = []
        if gate.holds:
            notices.append(Notice("hold", f"the factory is on hold ({', '.join(gate.holds)})"))
        if gate.retry_not_before is not None and now < gate.retry_not_before:
            notices.append(Notice("rate-limited", "the runtime asked the factory to wait"))
        _check_snapshot(gate, now, self._limits, notices, [])
        _check_window(gate, now, self._limits, notices, [])
        out = [n.detail for n in notices]
        tries = len(job.claims) - job.rate_limited if job is not None else 0
        if job is not None and tries >= self._policy.launches_per_job:
            out.append(f"this review job was already tried {tries} times")
        if job is None and len(view.cycle_jobs(cycle)) >= self._policy.max_passes:
            out.append(
                f"this task's {self._policy.max_passes} review passes are used; more need an"
                " explicit allowance"
            )
        return out

    # --- helpers --------------------------------------------------------------------

    def _not_reviewable(
        self, attempt, cycle, rev, key, collected: Collected, view, previous
    ) -> ReviewStatus:
        number = rev.pr
        if (
            rev.head
            and collected.problems == (f"PR #{number} is closed.",)
            and self._merged(number)
        ):
            # A merged PR, otherwise unchanged: the last verdict stands if it
            # was for exactly this revision.
            records = [v for v in view.verdicts.values() if v.attempt == attempt]
            latest = max(records, key=lambda v: v.seq, default=None)
            if latest is not None and latest.key == key:
                return self._from_record(latest, view)
        found = from_reports(None, None, commit=rev.head, problems=collected.problems)
        if not found:
            found = from_reports(
                None, None, commit=rev.head, problems=("The PR could not be read as a candidate.",)
            )
        # Recorded, so no earlier verdict stays the latest evidence.
        return self._record(
            attempt,
            cycle,
            rev,
            key,
            ReviewState.NOT_REVIEWABLE,
            carry_forward(previous, found, evaluated=False),
            view,
            "The factory can't review PR #{n} as it is: {why}",
        )

    def _merged(self, number: int) -> bool:
        try:
            pr = self._api.json(f"repos/{self._repo}/pulls/{number}")
        except Exception:
            return False
        return isinstance(pr, dict) and pr.get("merged") is True

    def _previous_findings(self, view: ev.ReviewView, cycle: str, key: str) -> tuple[Finding, ...]:
        """The open findings as the latest record of another revision in this
        cycle left them (every record carries the open ones forward)."""
        records = [v for v in view.by_cycle.get(cycle, []) if v.key != key]
        if not records:
            return ()
        return tuple(_finding(f) for f in records[-1].findings if not f.get("resolved"))

    def _from_record(self, record: ev.VerdictRecord, view) -> ReviewStatus:
        rev = Revision(
            self._repo, record.pr, record.digest, record.head, record.base, record.merge_base
        )
        state = ReviewState(record.verdict)
        job = view.jobs.get(record.key)
        return ReviewStatus(
            state,
            record.attempt,
            record.pr,
            f"The latest recorded review of PR #{record.pr} is {state.value} on"
            f" `{record.head[:12]}`.",
            record.key,
            rev,
            tuple(_finding(f) for f in record.findings),
            record.review_url,
            None,
            job.pass_kind if job else None,
        )

    def _view(self) -> ev.ReviewView:
        return ev.ReviewView.build(self._store.events())


@dataclass(frozen=True)
class _Answer:
    """What a launch came to. Unlike LaunchResult it can say "not launched"
    without an HTTP status: the runtime refused before sending anything."""

    outcome: LaunchOutcome
    http_status: int | None = None
    session_url: str | None = None
    detail: str = ""
    retry_after_seconds: int | None = None
    response_body: str | None = None


def _finding(d: Mapping[str, object]) -> Finding:
    return finding_from_data(d)


def _clip(e: BaseException) -> str:
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[redacted]", f"{type(e).__name__}: {e}")[:300]


def _hours(t: timedelta) -> str:
    h = t.total_seconds() / 3600
    return f"{h:g} hours" if h != 1 else "an hour"


__all__ = [
    "FINAL",
    "POLICY_VERSION",
    "AutoReviewer",
    "ReviewPolicy",
    "ReviewState",
    "ReviewStatus",
    "Revision",
    "ledger_contracts",
]
