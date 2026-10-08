"""Choosing the commit a task starts from, right before its contract is frozen.

The policy (``BasePolicy``): the newest commit on the project's branch whose
own push run of the trusted CI workflow passed the required job, looking back
at most ``lookback`` commits. Every commit on the pilot's main came through a
reviewed PR (the ruleset requires Rolando's code-owner approval), so "green
on main" is "reviewed and checked". A newer commit that is still running or
red is skipped, and the summary says so.

Nothing from an earlier contract or a sample is reused: each draft asks
GitHub again. GitHub answers are checked the same way the loop checks them
(exact head, exact workflow path, this repository, push event).

Standard library only. All I/O is through the injected ``GitHubApi``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from controller.loop.collect import GitHubApi, GitHubUnreadable
from controller.prepare.policy import BasePolicy

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ACTIONS_APP = "github-actions"
"""The app behind a trusted run's check suite, as the loop requires."""


class NoEligibleBase(Exception):
    """No commit in the window passed CI. The service retries next round."""


@dataclass(frozen=True)
class Base:
    commit: str
    run_url: str
    skipped: tuple[str, ...]
    """Newer commits passed over (not green yet, or red), newest first."""


def _same(a: object, b: str) -> bool:
    return isinstance(a, str) and a.lower() == b.lower()


def select(api: GitHubApi, repository: str, policy: BasePolicy) -> Base:
    """Raises GitHubUnreadable (GitHub down or odd) or NoEligibleBase."""
    commits = api.json(
        f"repos/{repository}/commits?sha={quote(policy.branch, safe='')}&per_page={policy.lookback}"
    )
    if not isinstance(commits, list) or not commits:
        raise GitHubUnreadable(f"no commits listed for {policy.branch}")
    workflow = policy.workflow_path.rsplit("/", 1)[-1]
    skipped: list[str] = []
    for c in commits[: policy.lookback]:
        sha = c.get("sha") if isinstance(c, Mapping) else None
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise GitHubUnreadable("a listed commit has no commit id")
        url = _green(api, repository, sha, workflow, policy)
        if url is not None:
            return Base(sha, url, tuple(skipped))
        skipped.append(sha)
    raise NoEligibleBase(
        f"none of the newest {len(skipped)} commits on {policy.branch} passed"
        f" {policy.workflow_path} ({policy.job})"
    )


def _green(api: GitHubApi, repo: str, sha: str, workflow: str, policy: BasePolicy) -> str | None:
    answer = api.json(
        f"repos/{repo}/actions/workflows/{quote(workflow)}/runs"
        f"?head_sha={sha}&event=push&per_page=20"
    )
    runs = answer.get("workflow_runs") if isinstance(answer, Mapping) else None
    if not isinstance(runs, list):
        raise GitHubUnreadable("the workflow runs answer has no list of runs")
    trusted = [
        r
        for r in runs
        if isinstance(r, Mapping)
        and r.get("head_sha") == sha
        and r.get("path") == policy.workflow_path
        and r.get("event") == "push"
        and r.get("head_branch") == policy.branch
        and _same((r.get("repository") or {}).get("full_name"), repo)
        and _same((r.get("head_repository") or {}).get("full_name"), repo)
        and isinstance(r.get("id"), int)
        and not isinstance(r.get("id"), bool)
    ]
    if not trusted:
        return None
    run = max(trusted, key=lambda r: (str(r.get("created_at") or ""), r["id"]))
    if run.get("status") != "completed":
        return None
    suite = run.get("check_suite_id")
    if isinstance(suite, bool) or not isinstance(suite, int):
        return None
    app = api.json(f"repos/{repo}/check-suites/{suite}")
    if not isinstance(app, Mapping) or (app.get("app") or {}).get("slug") != ACTIONS_APP:
        return None
    jobs = api.json(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
    jobs = jobs.get("jobs") if isinstance(jobs, Mapping) else None
    if not isinstance(jobs, list):
        raise GitHubUnreadable(f"run {run['id']}'s jobs answer has no list of jobs")
    named = [j for j in jobs if isinstance(j, Mapping) and j.get("name") == policy.job]
    if len(named) != 1 or named[0].get("conclusion") != "success":
        return None
    url = run.get("html_url")
    return url if isinstance(url, str) and url.startswith("https://") else f"run {run['id']}"


__all__ = ["Base", "NoEligibleBase", "select"]
