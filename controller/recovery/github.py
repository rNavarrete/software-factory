"""Reading an attempt's work from GitHub, the only machine-readable evidence of
what a worker did (ADR 0002 sections 3 and 4).

Everything read here is untrusted: titles, bodies and branch names come from
whoever can open a PR. Recovery only compares them against the markers the
controller expects; nothing in them is ever read as a decision.

``GhCliReader`` runs ``gh api`` under Rolando's own GitHub login, read-only.
Tests use any object with the ``GitHubReader`` methods.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PUSH_PERMISSIONS = frozenset({"admin", "maintain", "write"})
GH_TIMEOUT_SECONDS = 60


class GitHubUnreadable(Exception):
    """GitHub could not be read, or answered with something unexpected.
    Recovery records nothing from a read that failed."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    title: str
    body: str
    head_branch: str
    head_sha: str
    head_repo: str | None
    """``owner/name`` the head branch lives in; None if that repo was deleted."""
    author: str
    state: str
    """"open" or "closed"."""
    draft: bool
    merged: bool
    merge_commit: str | None
    """The merge commit, only when merged."""


@runtime_checkable
class GitHubReader(Protocol):
    def branch_head(self, repo: str, branch: str) -> str | None:
        """The branch's head commit, or None if there is no such branch."""
        ...

    def pulls_for_branch(self, repo: str, branch: str) -> list[PullRequest]:
        """Every PR, open or closed, whose head is ``branch`` in ``repo``."""
        ...

    def recent_pulls(self, repo: str) -> list[PullRequest]:
        """The 100 most recently updated PRs in ``repo``, open or closed."""
        ...

    def can_push(self, repo: str, login: str) -> bool:
        """Whether GitHub lists ``login`` with push access to ``repo``."""
        ...


def _check_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")
    return repo


class GhCliReader:
    """``GitHubReader`` over the ``gh`` command, as Rolando's login."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        gh: str = "gh",
    ) -> None:
        self._run = run
        self._gh = gh

    def branch_head(self, repo: str, branch: str) -> str | None:
        refs = self._api(f"repos/{_check_repo(repo)}/git/matching-refs/heads/{quote(branch)}")
        if not isinstance(refs, list):
            raise GitHubUnreadable("matching-refs did not return a list")
        # matching-refs is a prefix match: claude/x-a1 also matches claude/x-a10.
        exact = [
            r for r in refs if isinstance(r, Mapping) and r.get("ref") == f"refs/heads/{branch}"
        ]
        if not exact:
            return None
        sha = _get(exact[0], "object", "sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise GitHubUnreadable(f"branch {branch} has no readable head commit")
        return sha

    def pulls_for_branch(self, repo: str, branch: str) -> list[PullRequest]:
        owner = _check_repo(repo).split("/")[0]
        head = quote(f"{owner}:{branch}", safe="")
        return self._pulls(f"repos/{repo}/pulls?state=all&per_page=100&head={head}")

    def recent_pulls(self, repo: str) -> list[PullRequest]:
        return self._pulls(
            f"repos/{_check_repo(repo)}/pulls?state=all&sort=updated&direction=desc&per_page=100"
        )

    def can_push(self, repo: str, login: str) -> bool:
        if not _LOGIN_RE.fullmatch(login):
            raise ValueError(f"not a GitHub login: {login!r}")
        answer = self._api(f"repos/{_check_repo(repo)}/collaborators/{login}/permission")
        permission = _get(answer, "permission")
        role = _get(answer, "role_name")
        if not isinstance(permission, str):
            raise GitHubUnreadable("the permission answer has no permission field")
        return permission in _PUSH_PERMISSIONS or role in _PUSH_PERMISSIONS

    def _pulls(self, path: str) -> list[PullRequest]:
        items = self._api(path)
        if not isinstance(items, list):
            raise GitHubUnreadable("the pulls answer is not a list")
        return [_pull(item) for item in items]

    def _api(self, path: str) -> Any:
        try:
            result = self._run(
                [self._gh, "api", "-H", "Accept: application/vnd.github+json", path],
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise GitHubUnreadable(f"gh api {path} did not run: {e}") from e
        if result.returncode != 0:
            raise GitHubUnreadable(f"gh api {path} failed: {result.stderr.strip()[:500]}")
        try:
            return json.loads(result.stdout)
        except ValueError as e:
            raise GitHubUnreadable(f"gh api {path} returned unreadable JSON") from e


def _get(value: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _pull(item: object) -> PullRequest:
    """One PR from the REST answer. Raises GitHubUnreadable on a missing or
    mistyped field, so a half-read PR is never recorded."""
    number, url = _get(item, "number"), _get(item, "html_url")
    title, body = _get(item, "title"), _get(item, "body")
    ref, sha = _get(item, "head", "ref"), _get(item, "head", "sha")
    head_repo = _get(item, "head", "repo", "full_name")
    author, state = _get(item, "user", "login"), _get(item, "state")
    draft, merged_at = _get(item, "draft"), _get(item, "merged_at")
    merge_commit = _get(item, "merge_commit_sha")
    ok = (
        isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and isinstance(url, str)
        and url.startswith("https://")
        and isinstance(title, str)
        and (body is None or isinstance(body, str))
        and isinstance(ref, str)
        and isinstance(sha, str)
        and _SHA_RE.fullmatch(sha) is not None
        and (head_repo is None or isinstance(head_repo, str))
        and isinstance(author, str)
        and state in ("open", "closed")
        and isinstance(draft, bool)
        and (merged_at is None or isinstance(merged_at, str))
    )
    if not ok:
        raise GitHubUnreadable(f"a pull request in the answer is malformed: #{number!r}")
    merged = merged_at is not None
    if merged and not (isinstance(merge_commit, str) and _SHA_RE.fullmatch(merge_commit)):
        raise GitHubUnreadable(f"merged PR #{number} has no merge commit")
    return PullRequest(
        number=number,
        url=url,
        title=title,
        body=body or "",
        head_branch=ref,
        head_sha=sha,
        head_repo=head_repo,
        author=author,
        state=state,
        draft=draft,
        merged=merged,
        merge_commit=merge_commit if merged else None,
    )
