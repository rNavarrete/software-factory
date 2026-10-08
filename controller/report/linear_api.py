"""Linear's GraphQL API, for posting and reading comments (ENG-178).

One POST per call, only to api.linear.app, no redirects, with the factory's
own Linear key. Failures come back as two kinds, because the service treats
them differently:

- ``LinearDown``: Linear can't be reached, answers 5xx, or is rate limiting
  the factory. Nothing more is worth trying this round.
- ``LinearRefused``: Linear answered and said no to this one request (bad
  input, not found, a conflict). ``code`` carries Linear's error code when it
  gives one.

The key never appears in an error. Anything in an error that looks like a
secret is redacted, because errors end up in the ledger.

Standard library only.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from controller.ledger.redact import redact

API = "https://api.linear.app/graphql"
_ERROR_LIMIT = 200

Transport = Callable[[str, Mapping[str, object]], Mapping[str, object]]
"""``(query, variables) -> data``. Raises ``LinearDown`` or ``LinearRefused``."""


class LinearError(Exception):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class LinearDown(LinearError):
    """Linear is unreachable, failing, or rate limiting: wait and retry."""


class LinearRefused(LinearError):
    """Linear refused this request."""


_RATE_LIMIT_CODES = frozenset({"RATELIMITED", "RATE_LIMITED"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _clean(text: object, key: str) -> str:
    s = str(text or "")
    if key:
        s = s.replace(key, "[key]")
    return redact(" ".join(s.split()))[:_ERROR_LIMIT]


def _first_error(body: object) -> tuple[str, str]:
    """(message, code) of the first GraphQL error, if any."""
    if not isinstance(body, Mapping):
        return "", ""
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], Mapping):
        return "", ""
    first = errors[0]
    ext = first.get("extensions")
    code = ""
    if isinstance(ext, Mapping):
        code = str(ext.get("code") or ext.get("type") or "")
    return str(first.get("message") or ""), code.upper()


class HttpTransport:
    """POSTs a GraphQL request with the factory's Linear key."""

    def __init__(
        self,
        key: Callable[[], str],
        opener: Callable[..., object] | None = None,
        *,
        agent: str = "software-factory-reporter",
    ) -> None:
        self._key = key
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._agent = agent

    def __call__(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        key = self._key()
        auth = key if key.startswith("lin_api_") else f"Bearer {key}"
        req = urllib.request.Request(
            API,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": auth,
                "User-Agent": self._agent,
            },
            method="POST",
        )
        try:
            with self._open(req, timeout=60) as resp:  # type: ignore[attr-defined]
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise _http_error(e, key) from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise LinearDown(f"Linear could not be reached ({type(e).__name__})") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise LinearDown("Linear sent an answer that is not JSON") from None
        return _data(body, key)


def _http_error(e: urllib.error.HTTPError, key: str) -> LinearError:
    try:
        body: object = json.loads(e.read().decode("utf-8", "replace"))
    except Exception:
        body = None
    message, code = _first_error(body)
    text = f"Linear answered HTTP {e.code}" + (f": {_clean(message, key)}" if message else "")
    if e.code == 429 or code in _RATE_LIMIT_CODES:
        return LinearDown(text, "RATELIMITED")
    if e.code >= 500:
        return LinearDown(text, code)
    if e.code in (401, 403):
        # The key was revoked or lacks access: every request will fail the same way.
        return LinearDown(text, code or "AUTHENTICATION")
    return LinearRefused(text, code)


def _data(body: object, key: str) -> Mapping[str, object]:
    message, code = _first_error(body)
    if message or code:
        text = f"Linear refused the request: {_clean(message, key)}"
        if code in _RATE_LIMIT_CODES:
            return_error: LinearError = LinearDown(text, "RATELIMITED")
        else:
            return_error = LinearRefused(text, code)
        raise return_error
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), Mapping):
        raise LinearDown("Linear sent an answer without data")
    return body["data"]  # type: ignore[return-value]


__all__ = [
    "API",
    "HttpTransport",
    "LinearDown",
    "LinearError",
    "LinearRefused",
    "Transport",
]
