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

import hashlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping

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


VIEWER_QUERY = "query { viewer { id name app } }"


class HttpTransport:
    """POSTs a GraphQL request with the factory's Linear key.

    The key is read again for every request, so a replaced secret takes
    effect at once. Whose key it is gets checked against that same key
    value: before the first request made with a key it hasn't seen, the
    transport asks Linear who the key acts as, and refuses every request
    made with a key that acts as ``forbidden_user`` (Rolando) or as anyone
    but ``expected_user``. A key swapped in later can't skip the check.

    ``forbidden_user`` may be a callable returning one id or several. It is
    asked again on every request, so a change in who is forbidden (the
    onboarding file names a new approver) applies to keys already checked."""

    def __init__(
        self,
        key: Callable[[], str],
        opener: Callable[..., object] | None = None,
        *,
        agent: str = "software-factory-reporter",
        forbidden_user: str | Callable[[], str | Iterable[str] | None] | None = None,
        expected_user: str | None = None,
    ) -> None:
        self._key = key
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._agent = agent
        self._forbidden = forbidden_user
        self._expected = expected_user
        self._viewers: dict[str, str] = {}
        """sha256 of a key -> the user it acts as, once checked."""

    def checked_viewer(self) -> str:
        """The Linear user the current key acts as, checked."""
        return self._viewer(self._key())

    def __call__(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        key = self._key()
        if self._forbidden or self._expected:
            self._viewer(key)  # with this same key value, before it is used
        return self._post(key, query, variables)

    def _viewer(self, key: str) -> str:
        fp = hashlib.sha256(key.encode()).hexdigest()
        vid = self._viewers.get(fp) or ""
        if not vid:
            viewer = self._post(key, VIEWER_QUERY, {}).get("viewer")
            vid = str(viewer.get("id") or "") if isinstance(viewer, Mapping) else ""
            if not vid:
                raise LinearDown("Linear did not say whose key this is", "IDENTITY")
        if vid in self._forbidden_now():
            raise LinearDown(
                "the factory's Linear key acts as Rolando, so anything it posted would look like"
                " his. Nothing is sent until the factory has its own Linear identity.",
                "IDENTITY",
            )
        if self._expected and vid != self._expected:
            raise LinearDown(
                "the factory's Linear key acts as a different user than the one configured",
                "IDENTITY",
            )
        self._viewers[fp] = vid
        return vid

    def _forbidden_now(self) -> frozenset[str]:
        f = self._forbidden() if callable(self._forbidden) else self._forbidden
        if not f:
            return frozenset()
        return frozenset([f] if isinstance(f, str) else (x for x in f if x))

    def _post(self, key: str, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
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
