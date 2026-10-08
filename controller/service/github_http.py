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


def as_bytes(
    run: Callable[..., subprocess.CompletedProcess],
) -> Callable[..., subprocess.CompletedProcess]:
    """``run`` for readers that expect ``gh``'s raw bytes (``GhApi``), where
    ``HttpGhRunner`` answers with text like ``gh --jq`` would."""

    def call(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        r = run(argv, **kwargs)
        out, err = r.stdout, r.stderr
        return subprocess.CompletedProcess(
            r.args,
            r.returncode,
            out.encode() if isinstance(out, str) else out,
            err.encode() if isinstance(err, str) else err,
        )

    return call


def _message(e: urllib.error.HTTPError, token: str) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
        message = json.loads(raw).get("message", "")
    except Exception:
        message = ""
    text = f"HTTP {e.code}: {message}"
    return text.replace(token, "[redacted]") if token else text


__all__ = ["HttpGhRunner", "as_bytes"]
