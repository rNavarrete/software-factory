"""One task from approved contract to a PR ready for Rolando's review (ENG-145).

``Loop.run(contract)`` is what ``python3 -m controller.loop run <contract.json>``
does. Run it again at any point and it picks up where the ledger and GitHub
say the task is; nothing it does is repeated:

1. Dispatch, through the one fire path (``Dispatcher.dispatch``): it asks for
   the approval code if this exact contract isn't approved, fires one worker,
   and on a repeat run fires nothing and returns the recorded attempt.
2. Wait for the worker's PR, reading GitHub through recovery's ``reconcile``
   (which records the PR once, however often it runs).
3. When the PR's trusted CI run has finished, run the independent check
   (``check.assess``), record it for that exact commit, and write the full
   report to ``~/.software-factory/reports``.
4. When the independent mapper has posted its review, ask Rolando for what
   only he can give: what he saw for each observable criterion, and whether
   he has looked at each flag. Each answer is a signed record for that exact
   commit. A new push makes every one of them stale.
5. Say "ready for your review" with the PR link, or exactly what blocks it.
   Merging is Rolando's, on GitHub. Once the PR is merged, the next run
   records that the worker's session is finished (his code again), which
   frees the single lane for the next task.

Controller interruption (Ctrl-C, a closed laptop) loses nothing: every step is
read back from the ledger and GitHub on the next run, and recovery's rules
(``Recovery.recover``) decide what an interrupted launch means. The loop never
fires a second worker for an attempt, never pushes, approves, merges or
releases, and asks for a repair only by telling Rolando the command.

Every prompt it shows Rolando is timed into the ledger as his active minutes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from controller import contract as contracts
from controller.attempts.events import ClearingBasis
from controller.attempts.policy import LedgerView
from controller.dispatch import Dispatcher
from controller.interfaces import AttemptId, ContractDigest, LedgerStore, TaskId
from controller.loop.check import CHECK_NAME, Assessment, assess, render
from controller.loop.collect import GitHubApi, GitHubUnreadable, collect
from controller.loop.decisions import ReviewDecisions, Timer
from controller.recovery import FINISHED, PILOT_REPO, Recovery, RecoveryRefused, State
from verify.criteria import Verdict

POLL_SECONDS = 60
DEFAULT_WAIT_MINUTES = 90

EXIT_READY, EXIT_STOPPED, EXIT_WAITING = 0, 1, 3
MAX_REVIEW_MINUTES = 8 * 60.0
MIN_NOTE = 8
_NOT_A_NOTE = frozenset({"y", "yes", "n", "no", "ok", "okay", "skip", "fine", "sure", "lgtm"})


class Asker:
    """Asks Rolando at the terminal. ``None`` means nobody is there to answer."""

    def __init__(self, stdin: TextIO, out: TextIO) -> None:
        self._in, self._out = stdin, out

    def ask(self, prompt: str) -> str | None:
        try:
            if not self._in.isatty():
                return None
        except (AttributeError, ValueError):
            return None
        self._out.write(prompt)
        self._out.flush()
        line = self._in.readline()
        return None if line == "" else line.strip()


@dataclass
class Loop:
    store: LedgerStore
    recovery: Recovery
    dispatcher: Dispatcher
    api: GitHubApi
    decisions: ReviewDecisions
    timer: Timer
    asker: Asker
    reports: Path
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    say: Callable[[str], None] = print
    poll_seconds: float = POLL_SECONDS
    repo: str = PILOT_REPO

    def run(
        self, contract: Mapping[str, object], wait_minutes: float = DEFAULT_WAIT_MINUTES
    ) -> int:
        digest = contracts.digest(contract)
        task = TaskId(str(contract["task_id"]))
        self.timer.task = task
        try:
            result = self.dispatcher.dispatch(contract)
        except KeyboardInterrupt:
            self.say(
                "\nStopped. Whatever was sent is on record: run the same command again and it"
                " checks before anything is sent twice."
            )
            return EXIT_WAITING
        if result.outcome not in ("launched", "already-dispatched"):
            self.say(result.message)
            return EXIT_STOPPED
        attempt = result.attempt
        if result.outcome == "launched":
            self.say(result.message)
        recorded = result.status.digest if result.status is not None else None
        if recorded and recorded != digest.value:
            # The attempt on record was approved for another version of this
            # contract. Its PR is judged against what was approved for it, so
            # judging it against this file would only report false mismatches.
            self.say(result.message)
            self.say(
                f"{attempt} was started from another version of this contract"
                f" ({recorded[:12]}), not this file ({digest.short}). Finish or close that"
                " attempt first; a changed contract needs its own approval and attempt."
            )
            return EXIT_STOPPED
        deadline = self.now().timestamp() + wait_minutes * 60
        last_said = None
        try:
            while True:
                outcome, said = self._step(contract, digest, attempt)
                if outcome is not None:
                    return outcome
                if said != last_said:
                    self.say(said)
                    last_said = said
                if self.now().timestamp() >= deadline:
                    self.say(
                        "Still waiting. Nothing is lost: run the same command again later"
                        " and it picks up from here."
                    )
                    return EXIT_WAITING
                self.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self.say("\nStopped waiting. Nothing is lost: run the same command again to pick up.")
            return EXIT_WAITING

    # --- one pass ---------------------------------------------------------------

    def _step(
        self, contract: Mapping[str, object], digest: ContractDigest, attempt: AttemptId
    ) -> tuple[int | None, str]:
        """Look once. Returns (exit code, None) when done, or (None, status line)."""
        now = self.now()
        try:
            found = self.recovery.reconcile(attempt, now)
        except RecoveryRefused as e:
            return None, f"Couldn't read GitHub just now ({e}); will try again."
        status = found.status
        for w in found.warnings:
            self.say(f"Warning: {w}")
        if status.state is State.MERGED:
            return self._finish(attempt, status), ""
        if status.state in FINISHED:
            self.say(f"{attempt} is {status.state.value}: {status.detail}")
            self.say(_repair_hint(contract))
            return EXIT_STOPPED, ""
        if status.state in (State.UNKNOWN, State.DISPATCHING) or not status.pull_requests:
            if status.state is State.RUNNING:
                return None, "The worker is running; no PR yet."
            self.say(f"{attempt}: {status.state.value}. {status.detail}")
            self.say(f"Next: {status.next_step}")
            return EXIT_STOPPED, ""

        try:
            open_prs = self._open_prs(status.pull_requests)
        except GitHubUnreadable as e:
            return None, f"Couldn't read the attempt's PRs just now ({e}); will try again."
        if len(open_prs) != 1:
            self.say(f"{attempt}: {status.detail}")
            self.say(f"Next: {status.next_step}")
            return EXIT_STOPPED, ""
        number = open_prs[0]
        try:
            collected = collect(self.api, contract, digest, attempt, number, repo=self.repo)
        except GitHubUnreadable as e:
            return None, f"Couldn't read PR #{number} just now ({e}); will try again."
        if collected.pending:
            return None, f"PR #{number} is open; waiting for its CI run to finish."
        assessment = self._assess(contract, digest, attempt, collected)
        if assessment.waiting_on_review:
            self._record(assessment, status.latest_run, now)
            return None, (
                f"PR #{number} passed collection; waiting for the independent review comment"
                f" on {collected.pr_url}."
            )
        if assessment.conclusion() == "action_required":
            # Only Rolando's inputs are missing; anything else wrong needs a repair first.
            assessment = self._ask_rolando(contract, digest, attempt, assessment)
        self._record(assessment, status.latest_run, now)
        path = self._write_report(attempt, assessment)
        head = collected.candidate.head_commit if collected.candidate else "?"
        if assessment.ready:
            self.say(f"Ready for your review: {collected.pr_url}")
            self.say(f"Checked commit: {head}")
            self.say(f"Full report: {path}")
            self.say(
                "Before you merge, check the PR's latest commit is still the one above: a"
                " later push hasn't been checked. Merge on GitHub if you're happy, then run"
                " this same command again to close the task out."
            )
            return EXIT_READY, ""
        self.say(f"PR #{number} (commit {head[:12]}) is not ready for review:")
        for b in assessment.blockers:
            self.say(f"  - {b}")
        self.say(f"Full report: {path}")
        if assessment.conclusion() == "action_required":
            self.say("Only your answers are missing: run this same command again to give them.")
        else:
            self.say(_repair_hint(contract))
        return EXIT_STOPPED, ""

    def _open_prs(self, numbers) -> list[int]:
        """Which of ``numbers`` are open now. An unreadable one raises: guessing
        could pick the wrong PR when two carry the attempt's markers."""
        out = []
        for n in numbers:
            pr = self.api.json(f"repos/{self.repo}/pulls/{n}")
            if not isinstance(pr, Mapping) or pr.get("state") not in ("open", "closed"):
                raise GitHubUnreadable(f"PR #{n} has no readable state")
            if pr["state"] == "open":
                out.append(n)
        return out

    def _assess(self, contract, digest, attempt, collected) -> Assessment:
        cand = collected.candidate
        if cand is None:
            return assess(contract, digest, collected)
        return assess(
            contract,
            digest,
            collected,
            observations=self.decisions.observations(attempt, digest, cand),
            clearances=self.decisions.clearances(attempt, digest, cand),
        )

    def _ask_rolando(self, contract, digest, attempt, a: Assessment) -> Assessment:
        cand = a.collected.candidate
        assert cand is not None
        asked = False
        owed = a.owed_observations(contract)
        if owed:
            self.say(
                f"\nTo look at it yourself, in your pilot checkout run:\n"
                f"git fetch origin && git checkout {cand.head_commit} && npm ci && npm run dev\n"
            )
        for c in owed:
            ev = c["evidence"]
            steps = ev.get("steps") or ev.get("what") or ""
            expected = ev.get("expected") or ""
            answer = self.timer.timed(
                lambda c=c, steps=steps, expected=expected: self.asker.ask(
                    f"{c['id']}: {c['statement']}\n  Do: {steps}\n  Expect: {expected}\n"
                    "Did you see that? y / n / Enter to skip: "
                ),
                f"observed {c['id']} on PR #{a.collected.pr_number}",
            )
            if answer is None or answer.lower() not in ("y", "n"):
                continue
            seen = self.asker.ask("What did you see? (Enter: exactly what was expected) ") or ""
            verdict = Verdict.PASS if answer.lower() == "y" else Verdict.FAIL
            if not seen:
                seen = f"Saw what was expected: {expected}" if verdict is Verdict.PASS else ""
            if not seen:
                self.say("Skipped: a 'no' needs a word on what you saw instead.")
                continue
            self.decisions.observe(
                attempt,
                digest,
                cand,
                c["id"],
                verdict,
                seen,
                "Checked once by hand in one browser at this commit.",
                self.now(),
            )
            asked = True
        for f in a.open_flags():
            answer = self.timer.timed(
                lambda f=f: self.asker.ask(
                    f"\nNeeds your eyes ({f.key}):\n  {f.detail}\n"
                    "If you've looked and it's fine, type a few words on what you checked"
                    " (Enter to leave it open): "
                ),
                f"looked at flag {f.key} on PR #{a.collected.pr_number}",
            )
            if not answer:
                continue
            if answer.lower().strip(" .!") in _NOT_A_NOTE or len(answer) < MIN_NOTE:
                self.say("Left open: say in a few words what you checked to clear it.")
                continue
            self.decisions.clear(attempt, digest, cand, f.key, answer, self.now())
            asked = True
        if not asked:
            return a
        return self._assess(contract, digest, attempt, a.collected)

    def _record(self, a: Assessment, run, now) -> None:
        if run is None:
            return
        event = a.checks_event(run, now)
        if event is None:
            return
        # Record only a change, so polling doesn't grow the ledger. Checked
        # under the writer lock so two runs at once can't both append it.
        with self.store.writer_lock():
            last = None
            for s in self.store.events(run.attempt.task):
                e = s.event
                if e.kind == event.kind and e.data.get("revision") == event.data["revision"]:
                    last = e.data.get("results")
            if last is not None and _same(last, event.data["results"]):
                return
            self.store.append(event)

    def _write_report(self, attempt: AttemptId, a: Assessment) -> Path:
        cand = a.collected.candidate
        name = f"{attempt}-{cand.head_commit[:12] if cand else 'unread'}.md"
        self.reports.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.reports / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render(a))
        return path

    def _finish(self, attempt: AttemptId, status) -> int:
        self.say(f"{attempt}: {status.detail}")
        if status.writer_cleared:
            self.say("This task is already closed out. The lane is free.")
            return EXIT_READY
        if not status.writer_cleared:
            if not status.session_urls:
                self.say(f"Next: {status.next_step}")
                return EXIT_STOPPED
            self.say(
                "Last step: confirm the worker's session has finished, so the next task"
                f" can start. Its session: {', '.join(status.session_urls)}"
            )
            try:
                self.recovery.clear(
                    attempt,
                    ClearingBasis.COMPLETED,
                    self.now(),
                    session_urls=status.session_urls,
                )
            except RecoveryRefused as e:
                self.say(f"Not recorded: {e}")
                return EXIT_STOPPED
        for _ in range(2):
            minutes = self.asker.ask(
                "Roughly how many minutes did you spend reviewing it on GitHub? (Enter to skip) "
            )
            if not minutes:
                break
            try:
                value = float(minutes)
            except ValueError:
                value = -1.0
            if not (0 < value <= MAX_REVIEW_MINUTES):  # also refuses nan and inf
                self.say(
                    f"A number of minutes above 0 and at most {MAX_REVIEW_MINUTES:.0f}, please."
                )
                continue
            self.timer.record(value, f"reviewed and merged {attempt}'s PR", entered_by="Rolando")
            break
        self.say("Done. The lane is free for the next task.")
        return EXIT_READY


def _same(a, b) -> bool:
    def norm(rs):
        return sorted(tuple(sorted((str(k), str(v)) for k, v in r.items())) for r in rs)

    return norm(a) == norm(b)


def _repair_hint(contract: Mapping[str, object]) -> str:
    return (
        "If you want the worker to try again, say what to fix with:"
        f' python3 -m controller repair <contract.json> "<what failed>" (task'
        f" {contract['task_id']}), then run this command again. To give up on it:"
        f' python3 -m controller close {contract["task_id"]} failed "<why>".'
    )


def latest_attempt(store: LedgerStore, task: TaskId) -> AttemptId | None:
    attempts = LedgerView.build(store.events(task)).task_attempts(task)
    return attempts[-1].attempt if attempts else None


__all__ = ["CHECK_NAME", "Asker", "Loop", "latest_attempt"]
