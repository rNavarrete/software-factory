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
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from controller import contract as contracts
from controller.approval import ApprovalRefused
from controller.attempts.events import ClearingBasis
from controller.attempts.policy import LedgerView
from controller.dispatch import Dispatcher
from controller.interfaces import AttemptId, ContractDigest, LedgerStore, TaskId
from controller.ledger import kinds, records
from controller.loop.check import CHECK_NAME, Assessment, assess, render
from controller.loop.collect import GitHubApi, GitHubUnreadable, collect
from controller.loop.decisions import ReviewDecisions, Timer, flag_label, flag_names, names_flag
from controller.recovery import FINISHED, PILOT_REPO, Recovery, RecoveryRefused, State
from verify.criteria import Verdict, _permitted

POLL_SECONDS = 60
DEFAULT_WAIT_MINUTES = 90

EXIT_READY, EXIT_STOPPED, EXIT_WAITING = 0, 1, 3
MAX_REVIEW_MINUTES = 8 * 60.0
COMPARE_FILE_LIMIT = 300
"""GitHub's compare lists at most this many files."""
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
    backup: Callable[[datetime], object] | None = None
    """Copies the ledger to the backups folder. Run whenever a run of the loop
    wrote anything (checks, Rolando's answers, the clearing, his minutes)."""
    poll_seconds: float = POLL_SECONDS
    repo: str = PILOT_REPO

    def run(
        self, contract: Mapping[str, object], wait_minutes: float = DEFAULT_WAIT_MINUTES
    ) -> int:
        task = TaskId(str(contract["task_id"]))
        mark = self._last_seq(task)
        try:
            return self._run(contract, task, wait_minutes)
        finally:
            if self._last_seq(task) != mark:
                self._backup()

    def _last_seq(self, task: TaskId) -> int:
        return max((s.seq for s in self.store.events(task)), default=0)

    def _backup(self) -> None:
        if self.backup is None:
            return
        try:
            self.backup(self.now())
        except Exception as e:  # a failed backup must not lose the run's result
            self.say(f"Warning: the ledger backup failed ({type(e).__name__}: {e}).")

    def _run(self, contract: Mapping[str, object], task: TaskId, wait_minutes: float) -> int:
        digest = contracts.digest(contract)
        self.timer.task = task
        if latest_attempt(self.store, task) is None:
            try:
                stop = self._stale_base(contract)
            except KeyboardInterrupt:
                stop = "\nNot started. Nothing was sent."
            if stop is not None:
                self.say(stop)
                return EXIT_STOPPED
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
            # Through the dispatcher, which backs the ledger up whenever this records.
            found = self.dispatcher.reconcile(attempt)
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
            said = (
                f"PR #{number} passed collection; waiting for the independent review comment"
                f" on {collected.pr_url}."
            )
            # A review that no longer counts (main moved, a new push, an edit)
            # is said out loud, never dropped in silence.
            for why in assessment.review.ignored:
                said += f"\n  Not used: {_plain_ignored(why)}"
            return None, said
        if assessment.conclusion() == "action_required":
            # Only Rolando's inputs are missing; anything else wrong needs a repair first.
            assessment = self._ask_rolando(contract, digest, attempt, assessment)
            now = self.now()  # his answers can take minutes; record when they were done
        self._record(assessment, status.latest_run, now)
        path = self._write_report(attempt, assessment)
        head = collected.candidate.head_commit if collected.candidate else "?"
        if assessment.ready and not self._still_open(number):
            self.say(
                f"PR #{number} is no longer open (merged or closed while this ran). Run this"
                " same command again to close the task out."
            )
            return EXIT_STOPPED, ""
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
        for why in assessment.review.ignored:
            self.say(f"  Review comment not used: {_plain_ignored(why)}")
        self.say(f"Full report: {path}")
        if any(_base_moved(why) for why in assessment.review.ignored):
            self.say(
                "Main moved after this PR was checked, so its review (and any check bound to"
                " main) is for the old main. Those need redoing for the new main: the review"
                " posted again, and CI run again on the new main. That part is not the"
                " worker's fault."
            )
        if assessment.conclusion() == "action_required":
            self.say("Only your answers are missing: run this same command again to give them.")
        else:
            self.say(_repair_hint(contract))
        return EXIT_STOPPED, ""

    def _still_open(self, number: int) -> bool:
        try:
            return self._open_prs([number]) == [number]
        except GitHubUnreadable:
            return False

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
            self.say(_look_hint(self.repo, cand))
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
            if seen and verdict is Verdict.PASS and not _about(seen, c):
                seen = (
                    self.asker.ask(
                        "That doesn't mention anything from the step or the expected result."
                        " Say what you saw, or press Enter to record exactly what was"
                        " expected: "
                    )
                    or ""
                )
                if seen and not _about(seen, c):
                    self.say(f"Skipped {c['id']}: what you saw needs to be about this check.")
                    continue
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
        flags = a.open_flags()
        for flag, note in self.decisions.unnamed_clearances(attempt, digest, cand):
            self.say(
                f'Your earlier note for {flag} ("{note[:80]}") doesn\'t name'
                f" {flag_label(flag)}, so it no longer counts."
            )
        if flags:
            self.say(
                "\n---- Flags: each one is about the code or tests in the PR, not the"
                " behavior above ----"
            )
        for f in flags:
            name = flag_label(f.key)
            answer = self.timer.timed(
                lambda f=f, name=name: self.asker.ask(
                    f"\nNeeds your eyes ({f.key}):\n  {f.detail}\n"
                    f"If you've looked and it's fine, say what you checked in {name}"
                    " (Enter to leave it open): "
                ),
                f"looked at flag {f.key} on PR #{a.collected.pr_number}",
            )
            if not answer:
                continue
            if answer.lower().strip(" .!") in _NOT_A_NOTE or len(answer) < MIN_NOTE:
                self.say("Left open: say in a few words what you checked to clear it.")
                continue
            if not names_flag(f.key, answer):
                self.say(
                    "Left open: a note clears only the thing it names, and this one names"
                    f" none of: {', '.join(flag_names(f.key))}."
                )
                continue
            try:
                self.decisions.clear(attempt, digest, cand, f.key, answer, self.now())
            except ApprovalRefused as e:
                self.say(f"Left open: {e}")
                continue
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
        self._merged_before_ready(attempt, status)
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

    def _merged_before_ready(self, attempt: AttemptId, status) -> None:
        """Say so, every time, when the merged commit never got the loop's ready.

        Nothing on GitHub stops a merge before the loop's verdict: merging
        stays Rolando's. So close-out checks the merged PR's head against the
        loop's own records and, the first time it finds no ready verdict for
        that exact commit, records it in the ledger, where the pilot's numbers
        count it.
        """
        heads = []
        for n in status.pull_requests:
            try:
                pr = self.api.json(f"repos/{self.repo}/pulls/{n}")
            except GitHubUnreadable as e:
                self.say(f"Warning: couldn't read PR #{n} to check it was ready when merged ({e}).")
                return
            if isinstance(pr, Mapping) and pr.get("merged_at"):
                sha = (pr.get("head") or {}).get("sha")
                merged_at = _when(pr.get("merged_at"))
                if isinstance(sha, str) and merged_at is not None:
                    heads.append((n, sha, merged_at))
        if not heads:
            return
        for n, sha, merged_at in heads:
            last = _verdict_before(self.store.events(attempt.task), attempt, sha, merged_at)
            if last == "success":
                continue
            why = (
                f"its last check before the merge said {last}"
                if last
                else "the loop never checked that commit before the merge"
            )
            self.say(
                f"Note: PR #{n} was merged at commit {sha[:12]} before this loop said it was"
                f" ready ({why}). That is recorded so the pilot's results count it."
            )
            stage = "merged-before-ready"
            with self.store.writer_lock():
                if any(
                    s.event.kind == kinds.FAILURE
                    and s.event.attempt == attempt
                    and s.event.data.get("stage") == stage
                    and s.event.data.get("revision") == sha
                    for s in self.store.events(attempt.task)
                ):
                    continue
                event = records.failure(
                    stage,
                    f"PR #{n} merged at {sha} with no ready verdict from the loop for that"
                    f" commit ({why})",
                    self.now(),
                    task=attempt.task,
                    attempt=attempt,
                    run=status.latest_run,
                )
                event = replace(event, data={**event.data, "revision": sha, "pr": n})
                self.store.append(event)

    def _stale_base(self, contract: Mapping[str, object]) -> str | None:
        """Before a task's first attempt: has main changed, since the contract's
        base, any file the task may change? Then the worker's PR would start
        from old code there and likely conflict (the second sample in the first
        live run had to be re-based). Advisory: it never decides anything
        alone, and an unreadable GitHub only warns."""
        base = str(contract.get("base_commit", ""))
        try:
            cmp = self.api.json(f"repos/{self.repo}/compare/{base}...main")
        except GitHubUnreadable as e:
            self.say(f"Warning: couldn't check whether main moved since the contract's base ({e}).")
            return None
        files = cmp.get("files") if isinstance(cmp, Mapping) else None
        if not isinstance(files, list):
            self.say("Warning: couldn't read what changed on main since the contract's base.")
            return None
        permitted = [str(p) for p in contract.get("permitted_paths", ())]
        touched = sorted(
            {
                name
                for f in files
                if isinstance(f, Mapping)
                for name in (f.get("filename"), f.get("previous_filename"))
                if isinstance(name, str) and _permitted(name, permitted)
            }
        )
        if len(files) >= COMPARE_FILE_LIMIT:
            self.say(
                f"Main has changed {len(files)} or more files since this contract's base"
                f" ({base[:12]}), more than GitHub lists, so whether it changed files this task"
                " may change can't be told. A contract on a newer base needs its own approval."
            )
        elif not touched:
            return None
        else:
            self.say(
                f"Main has changed since this contract's base ({base[:12]}) in files this task"
                f" may change: {', '.join(touched)}. The worker would start from the old"
                " version of them, and its PR would likely conflict. A contract on a newer"
                " base needs its own approval."
            )
        answer = self.timer.timed(
            lambda: self.asker.ask("Start it on the old base anyway? y / Enter to stop: "),
            f"decided whether to start {contract.get('task_id')} on an old base",
        )
        if answer is not None and answer.lower() == "y":
            return None
        return "Not started. Nothing was sent; only your time on this question was recorded."


def _when(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _verdict_before(events, attempt: AttemptId, sha: str, merged_at: datetime) -> str | None:
    """The loop's latest verdict on ``sha`` recorded before the merge, if any.
    A ready that a later check withdrew, or one recorded after the merge,
    doesn't count as ready when merged."""
    last = None
    for s in events:
        e = s.event
        if e.kind != kinds.CHECKS or e.attempt != attempt or e.data.get("revision") != sha:
            continue
        if e.at > merged_at:
            continue
        for r in e.data.get("results", ()):
            if r.get("name") == CHECK_NAME:
                last = str(r.get("conclusion"))
    return last


_STOP = frozenset(
    "the and was for but are you has had its that this with from then them they their"
    " there when what have only into each were your will just like once see saw new"
    " code chang work fine good looks look okay".split()
)


def _words(text: str) -> set[str]:
    """Word stems (first 5 letters) of 3 letters or more, minus filler."""
    stems = {w[:5] for w in re.findall(r"[^\W_]{3,}", text.lower())}
    return {w for w in stems if w not in _STOP}


def _about(seen: str, criterion: Mapping[str, object]) -> bool:
    """Whether what Rolando typed shares a real word with the check he followed.

    In the first live run "the new code changes" counted as having watched a
    button work. This only catches a note about something else; whether he
    really saw it is his word, as it always was."""
    ev = criterion.get("evidence") or {}
    source = " ".join(
        str(x)
        for x in (
            criterion.get("statement", ""),
            ev.get("steps", "") if isinstance(ev, Mapping) else "",
            ev.get("expected", "") if isinstance(ev, Mapping) else "",
            ev.get("what", "") if isinstance(ev, Mapping) else "",
        )
    )
    return bool(_words(seen) & _words(source))


_APP_PATHS = ("src/", "index.html", "public/")


def _look_hint(repo: str, cand) -> str:
    """How to look at the candidate: on GitHub always; run the app only when
    the change touches it (a README-only change needs no dev server)."""
    lines = [
        "\nTo look at it yourself, the files at this exact commit are at:",
        f"https://github.com/{repo}/tree/{cand.head_commit}",
    ]
    if any(p.startswith(_APP_PATHS) or p in _APP_PATHS for p in cand.changed_paths):
        lines += [
            "To try the app, in a checkout of the pilot repo run:",
            f"git fetch origin && git checkout {cand.head_commit} && npm ci && npm run dev",
        ]
    return "\n".join(lines) + "\n"


def _base_moved(why: str) -> bool:
    return why.startswith("stale:") and "its base_commit is" in why


def _plain_ignored(why: str) -> str:
    """A review comment the check didn't use, in plain words."""
    if _base_moved(why):
        return (
            why.removeprefix("stale: ")
            + " (main moved after the review was posted; the review needs a new comment"
            " for the current main)"
        )
    return why


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
