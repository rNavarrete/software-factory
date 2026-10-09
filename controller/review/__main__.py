"""``python3 -m controller.review dry-run <contract.json> <pr-number>``

(``check-token``, run on the host, checks the review token may start the
review workflow without starting it; see ``check_start_permission``.)

Runs the automatic review's checks on a real pilot PR without starting
anything: GitHub is only read, the ledger is a throwaway one in a temporary
folder, and the review job is never sent (a stand-in records what would have
been sent). It runs the same check twice, to show that the same revision
gives the same request key and one job, not two.

``--as-open`` reads a merged PR as if it were still open, to replay a past
sample. ``--reviewer`` names the account whose comment would count (none by
default, as in production until one is set up). Reads use ``gh api`` as you,
like the delivery loop.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from controller import contract as contracts
from controller.attempts import events as aev
from controller.interfaces import (
    AttemptId,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    TaskId,
)
from controller.ledger import SqliteLedgerStore
from controller.loop.collect import PILOT_REPO, GhApi, GitHubApi, NotFound
from controller.review import events as ev
from controller.review.reviewer import AutoReviewer, ReviewPolicy
from controller.service.seams import PullRequestRef


class DryRunRuntime:
    """Sends nothing. Answers "launched" so the dedupe can be shown; the
    throwaway ledger it is recorded in is deleted at the end."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def launch(self, text: str) -> LaunchResult:
        self.texts.append(text)
        return LaunchResult(LaunchOutcome.LAUNCHED, 200, "dry-run", "(dry run: nothing sent)")


class AsOpen:
    """A merged PR read as if still open, for replaying a past sample.

    Two things change at merge that the review relies on: the PR's state, and
    the CI run's link to the PR (GitHub empties a run's ``pull_requests`` once
    its PR is merged). The replay restores both from the PR itself; everything
    else is read as GitHub returns it. The restored link uses the PR's base as
    GitHub shows it now, so a replay can miss CI that was stale at the time.
    """

    def __init__(self, api: GitHubApi, number: int, repo: str) -> None:
        self._api = api
        self._number = number
        self._pr = f"repos/{repo}/pulls/{number}"
        self._runs = f"repos/{repo}/actions/workflows/"

    def json(self, path: str) -> Any:
        data = self._api.json(path)
        if path == self._pr and isinstance(data, dict):
            data = {**data, "state": "open"}
        elif path.startswith(self._runs) and isinstance(data, dict):
            pr = self._api.json(self._pr)
            link = [{"number": self._number, "base": {"sha": pr["base"]["sha"]}}]
            data = {
                **data,
                "workflow_runs": [
                    {**r, "pull_requests": r.get("pull_requests") or link}
                    for r in data.get("workflow_runs", [])
                    if isinstance(r, dict)
                ],
            }
        return data

    def raw(self, path: str) -> bytes:
        return self._api.raw(path)


def dry_run(
    api: GitHubApi,
    contract_text: str,
    number: int,
    *,
    attempt_number: int = 1,
    reviewer: str | None = None,
    repo: str = PILOT_REPO,
    now: datetime | None = None,
) -> dict[str, object]:
    contract = contracts.loads(contract_text)
    digest = contracts.digest(contract)
    attempt = AttemptId(TaskId(str(contract["task_id"])), attempt_number)
    now = now or datetime.now(UTC)
    policy = ReviewPolicy(reviewers=frozenset({reviewer}) if reviewer else frozenset())
    runtime = DryRunRuntime()
    with tempfile.TemporaryDirectory() as tmp:
        with SqliteLedgerStore(Path(tmp) / "dry-run.db") as store:
            with store.writer_lock():
                store.append(aev.attempt_reserved(attempt, digest, now))
                # A stand-in reading so the allowance checks run; nothing is fired.
                store.append(
                    LedgerEvent(
                        aev.USAGE_SNAPSHOT,
                        now,
                        data={
                            "taken_at": now.isoformat(),
                            "session_pct": 0,
                            "weekly_pct": 0,
                            "credits_spent": 0,
                        },
                    )
                )
            r = AutoReviewer(
                store,
                api,
                runtime,
                lambda a: (contract, digest),
                policy=policy,
                repo=repo,
                clock=lambda: now,
            )
            r.start(PullRequestRef(attempt, number), f"dry-run-{number}")
            first = r.check(attempt)
            second = r.check(attempt)
            intents = [s for s in store.events() if s.event.kind == ev.REVIEW_JOB_INTENT]
    job = json.loads(runtime.texts[0]) if runtime.texts else None
    rev = first.revision
    return {
        "pr": number,
        "task": str(attempt.task),
        "contract_digest": digest.value,
        "head": rev.head if rev else None,
        "base": rev.base if rev else None,
        "merge_base": rev.merge_base if rev else None,
        "request_key": first.key,
        "same_key_on_second_check": first.key == second.key,
        "state": first.state.value,
        "note": first.note,
        "reviewed_commit": first.reviewed_commit or None,
        "review_jobs_claimed": len(intents),
        "review_jobs_sent": 0,
        "job_pass": job["pass"] if job else None,
        "job_chars": len(runtime.texts[0]) if runtime.texts else 0,
        "findings": [
            {
                "id": f.id,
                "route": f.route.value,
                "severity": f.severity.value,
                "category": f.category,
                "summary": f.summary,
            }
            for f in first.findings
        ],
        "blocks": list(first.blocks),
    }


def qualify_identity(api: GitHubApi, login: str, repo: str = PILOT_REPO) -> list[str]:
    """Why ``login`` can't be the reviewer account; empty when it can.

    It must be a valid reviewer for the policy (a plain user login, not the
    worker, not Rolando), a real GitHub user, and have no write access to the
    repository: it can comment on the public repo, and nothing more.
    """
    try:
        ReviewPolicy(reviewers=frozenset({login}))
    except ValueError as e:
        return [str(e)]
    problems = []
    try:
        user = api.json(f"users/{login}")
    except NotFound:
        return [f"there is no GitHub account {login!r}"]
    if not isinstance(user, dict) or user.get("type") != "User":
        problems.append(f"{login!r} is not a GitHub user account")
    elif str(user.get("login", "")).lower() != login.lower():
        problems.append(f"GitHub knows {login!r} as {user.get('login')!r}")
    try:
        perm = api.json(f"repos/{repo}/collaborators/{login}/permission")
    except NotFound:
        perm = {"permission": "none"}
    level = perm.get("permission") if isinstance(perm, dict) else None
    if level not in ("none", "read"):
        problems.append(
            f"{login!r} has {level!r} access to {repo}; the reviewer must have none, so it"
            " can't push, approve or merge"
        )
    return problems


def check_token() -> int:
    """Prints ``review token: ok`` or ``review token: not ok: <why>``; always
    exits 0 so a caller over SSH reads the line instead of retrying."""
    import os

    from controller.review.workflow import check_start_permission
    from controller.service.secrets import FileSecrets, SecretMissing

    try:
        token = FileSecrets(os.environ.get("FACTORY_SECRETS_DIR", "")).get("review-token")
    except SecretMissing as e:
        print(f"review token: not ok: {e}")
        return 0
    problem = check_start_permission(token)
    print("review token: ok" if problem is None else f"review token: not ok: {problem}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m controller.review")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dry-run", help="check one PR without starting a review job")
    d.add_argument("contract", type=Path)
    d.add_argument("pr", type=int)
    d.add_argument("--attempt", type=int, default=1)
    d.add_argument("--reviewer", default=None)
    d.add_argument("--as-open", action="store_true")
    q = sub.add_parser("qualify-identity", help="check an account can be the reviewer")
    q.add_argument("login")
    sub.add_parser(
        "check-token", help="on the host: check the review token can start reviews (starts none)"
    )
    args = p.parse_args(argv)
    if args.command == "check-token":
        return check_token()
    api: GitHubApi = GhApi()
    if args.command == "qualify-identity":
        try:
            problems = qualify_identity(api, args.login)
        except Exception as e:
            print(f"check failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        for line in problems:
            print(f"not qualified: {line}")
        if not problems:
            print(f"{args.login} can be the reviewer: a user with no write access to {PILOT_REPO}.")
        return 1 if problems else 0
    if args.as_open:
        api = AsOpen(api, args.pr, PILOT_REPO)
    try:
        out = dry_run(
            api,
            args.contract.read_text(),
            args.pr,
            attempt_number=args.attempt,
            reviewer=args.reviewer,
        )
    except Exception as e:  # a plain message, not a traceback
        print(f"dry run failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
