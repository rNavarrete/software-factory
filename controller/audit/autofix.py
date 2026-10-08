"""Detective check that auto-fix stayed off on every worker PR (governance map G-G5).

Auto-fix has no repo-wide switch: it is kept off by never turning it on for a
PR. What it would leave behind is a worker push after the PR was opened (in
answer to CI or a comment). A worker PR in this factory is one commit pushed
before the PR exists, so this reads every PR a worker opened on the pilot
repo and reports any sign of a later push:

- a commit on the PR committed after the PR was opened, or
- CI runs for the PR at more than one head commit (each push starts one).

Read-only, through the collector's ``GitHubApi`` (``gh`` as Rolando's login).
It finds a violation after the fact; it can't prevent one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from controller.loop.collect import GitHubApi, GitHubUnreadable

MAX_PAGES = 20


@dataclass(frozen=True)
class AutofixReport:
    checked: tuple[int, ...]
    """Worker PR numbers read."""
    findings: tuple[str, ...]
    """Signs of a push after a PR was opened, or PRs that couldn't be read."""

    @property
    def clean(self) -> bool:
        return bool(self.checked) and not self.findings


def check_autofix(api: GitHubApi, repo: str, worker_logins: Iterable[str]) -> AutofixReport:
    workers = {w.lower().removesuffix("[bot]") for w in worker_logins}
    checked: list[int] = []
    findings: list[str] = []
    for pr in _pages(api, f"repos/{repo}/pulls?state=all&per_page=100"):
        login = str((pr.get("user") or {}).get("login", "")).lower().removesuffix("[bot]")
        if login not in workers:
            continue
        n = pr.get("number")
        opened = pr.get("created_at")
        ref = (pr.get("head") or {}).get("ref")
        if not isinstance(n, int) or not isinstance(opened, str) or not isinstance(ref, str):
            findings.append(f"a worker PR has no readable number, open time or branch: {n!r}")
            continue
        checked.append(n)
        try:
            commits = list(_pages(api, f"repos/{repo}/pulls/{n}/commits?per_page=100"))
            runs = api.json(f"repos/{repo}/actions/runs?event=pull_request&branch={ref}")
        except GitHubUnreadable as e:
            findings.append(f"PR #{n} could not be read ({e})")
            continue
        for c in commits:
            when = ((c.get("commit") or {}).get("committer") or {}).get("date")
            if not isinstance(when, str):
                findings.append(f"PR #{n}: a commit has no readable date")
            elif when > opened:  # both ISO-8601 UTC ("...Z"), so text order is time order
                findings.append(
                    f"PR #{n}: commit {str(c.get('sha'))[:12]} at {when},"
                    f" after it opened at {opened}"
                )
        heads = {
            r.get("head_sha")
            for r in (runs.get("workflow_runs", []) if isinstance(runs, Mapping) else [])
            if isinstance(r, Mapping)
        }
        if len(heads) > 1:
            findings.append(f"PR #{n}: CI ran at {len(heads)} different head commits")
    if not checked:
        findings.append("no worker PRs found, so nothing was checked")
    return AutofixReport(tuple(checked), tuple(findings))


def _pages(api: GitHubApi, path: str):
    for page in range(1, MAX_PAGES + 1):
        items = api.json(f"{path}&page={page}")
        if not isinstance(items, list):
            raise GitHubUnreadable(f"{path} did not return a list")
        yield from (i for i in items if isinstance(i, Mapping))
        if len(items) < 100:
            return
    raise GitHubUnreadable(f"{path} has more than {MAX_PAGES} pages")
