"""Checking a contract's base commit is on the pilot's protected branch.

A worker branches from the contract's base commit. One that is not on ``main``
(a commit on some other branch, or in a fork, which GitHub also serves under
the parent repo's name) would build on code Rolando never merged, so dispatch
refuses it. Read-only, as Rolando's ``gh`` login.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Protocol, runtime_checkable
from urllib.parse import quote

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
GH_TIMEOUT_SECONDS = 60
_ON_BRANCH = frozenset({"identical", "behind"})


class BaseUnreadable(Exception):
    """GitHub could not answer; dispatch sends nothing."""


@runtime_checkable
class BaseCheck(Protocol):
    def on_branch(self, repo: str, sha: str, branch: str) -> bool:
        """Whether ``sha`` is ``branch``'s head or one of its ancestors."""
        ...


class GhBaseCheck:
    """``BaseCheck`` over ``gh api .../compare/<branch>...<sha>``: the commit is on
    the branch when it is identical to or behind the branch's head."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        gh: str = "gh",
    ) -> None:
        self._run = run
        self._gh = gh

    def on_branch(self, repo: str, sha: str, branch: str) -> bool:
        if not _REPO_RE.fullmatch(repo):
            raise ValueError(f"not an owner/name repository: {repo!r}")
        if not _SHA_RE.fullmatch(sha):
            raise ValueError(f"not a 40-character commit id: {sha!r}")
        if not _BRANCH_RE.fullmatch(branch) or ".." in branch:
            raise ValueError(f"not a branch name: {branch!r}")
        path = f"repos/{repo}/compare/{quote(branch, safe='')}...{sha}"
        try:
            result = self._run(
                [self._gh, "api", "-H", "Accept: application/vnd.github+json", path],
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise BaseUnreadable(f"gh api {path} did not run: {e}") from e
        if result.returncode != 0:
            if "No commit found" in result.stderr or "No common ancestor" in result.stderr:
                return False
            raise BaseUnreadable(f"gh api {path} failed: {result.stderr.strip()[:500]}")
        try:
            answer = json.loads(result.stdout)
        except ValueError as e:
            raise BaseUnreadable(f"gh api {path} returned unreadable JSON") from e
        status = answer.get("status") if isinstance(answer, dict) else None
        if not isinstance(status, str):
            raise BaseUnreadable(f"gh api {path} has no comparison status")
        return status in _ON_BRANCH
