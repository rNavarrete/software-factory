"""``gh api`` without the ``gh`` program, for the host.

``GhCliReader`` and ``GhBaseCheck`` run ``gh api ... <path>`` through an
injectable ``run``. On the host there is no ``gh`` login; this ``run`` answers
the same calls over HTTPS with the service's read-only GitHub token, so the
existing readers (and all their checks on what GitHub returns) are reused
unchanged. Only GET, only api.github.com, no redirects, and the token is
never put in what it returns.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

API = "https://api.github.com/"
_PATH_RE = re.compile(r"^repos/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._/%:?=&-]+$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class HttpGhRunner:
    """A ``run`` for GhCliReader / GhBaseCheck that calls the REST API directly."""

    def __init__(
        self,
        token: Callable[[], str],
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._token = token
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        path = argv[-1]
        bad = not _PATH_RE.fullmatch(path) or ".." in path.split("?")[0].split("/")
        if len(argv) < 3 or argv[1] != "api" or bad:
            return subprocess.CompletedProcess(argv, 2, "", f"refused gh call: {path!r}")
        token = self._token()
        req = urllib.request.Request(
            API + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "software-factory-service",
            },
            method="GET",
        )
        timeout = kwargs.get("timeout", 60)
        try:
            with self._open(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
                body = resp.read().decode("utf-8", "replace")
            return subprocess.CompletedProcess(argv, 0, body, "")
        except urllib.error.HTTPError as e:
            return subprocess.CompletedProcess(argv, 1, "", _message(e, token))
        except (OSError, ValueError, http.client.HTTPException) as e:
            # Same shape as gh failing to run: the reader turns it into "unreadable".
            raise OSError(f"GitHub request failed: {type(e).__name__}") from None


def _message(e: urllib.error.HTTPError, token: str) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
        message = json.loads(raw).get("message", "")
    except Exception:
        message = ""
    text = f"HTTP {e.code}: {message}"
    return text.replace(token, "[redacted]") if token else text


# Where GitHub sends an artifact download (a short-lived signed link). The
# token is never sent there: the link carries its own permission.
_ARTIFACT_HOSTS = re.compile(
    r"^(?:[a-z0-9-]+\.blob\.core\.windows\.net|pipelines\.actions\.githubusercontent\.com"
    r"|[a-z0-9-]+\.actions\.githubusercontent\.com)$"
)
_ARTIFACT_ZIP = re.compile(r"^repos/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/actions/artifacts/\d+/zip$")
MAX_RAW_BYTES = 20 * 1024 * 1024


class HttpGitHubApi:
    """``GitHubApi`` (controller.loop.collect) over HTTPS with the read-only
    token, for the independent review on the host (ENG-156). Only GET, only
    ``repos/...`` paths on api.github.com. The one redirect followed is an
    artifact download's, to GitHub's storage, without the token."""

    def __init__(
        self,
        token: Callable[[], str],
        opener: Callable[..., object] | None = None,
        timeout: float = 60,
    ) -> None:
        self._token = token
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._timeout = timeout

    def json(self, path: str) -> object:
        body = self._get(path, "application/vnd.github+json")
        try:
            return json.loads(body)
        except ValueError as e:
            raise _unreadable(f"{path}: unreadable JSON") from e

    def raw(self, path: str) -> bytes:
        return self._get(path, "application/vnd.github.raw")

    def _get(self, path: str, accept: str) -> bytes:
        from controller.loop.collect import NotFound

        bad = not _PATH_RE.fullmatch(path) or ".." in path.split("?")[0].split("/")
        if bad:
            raise _unreadable(f"refused GitHub path {path!r}")
        token = self._token()
        req = urllib.request.Request(
            API + path,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "software-factory-service",
            },
            method="GET",
        )
        try:
            with self._open(req, timeout=self._timeout) as resp:  # type: ignore[attr-defined]
                return _read(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(f"{path}: not found") from None
            location = e.headers.get("Location", "") if e.code in (301, 302, 307) else ""
            if location and _ARTIFACT_ZIP.fullmatch(path):
                return self._download(location)
            raise _unreadable(f"{path}: {_message(e, token)}") from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise _unreadable(f"{path}: request failed: {type(e).__name__}") from None

    def _download(self, url: str) -> bytes:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not _ARTIFACT_HOSTS.fullmatch(parts.hostname or ""):
            raise _unreadable("artifact download redirected somewhere unexpected")
        req = urllib.request.Request(url, headers={"User-Agent": "software-factory-service"})
        try:
            with self._open(req, timeout=self._timeout) as resp:  # type: ignore[attr-defined]
                return _read(resp)
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise _unreadable(f"artifact download failed: {type(e).__name__}") from None


def _read(resp: object) -> bytes:
    data = resp.read(MAX_RAW_BYTES + 1)  # type: ignore[attr-defined]
    if len(data) > MAX_RAW_BYTES:
        raise _unreadable("GitHub's answer is too large")
    return data


def _unreadable(text: str) -> Exception:
    from controller.loop.collect import GitHubUnreadable

    return GitHubUnreadable(text)


__all__ = ["HttpGhRunner", "HttpGitHubApi"]
