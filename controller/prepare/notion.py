"""Read selected Notion pages through the markdown API; never follow document URLs.

The allowlist and credential supplier are configured by the controller, never by
an untrusted ticket. This reader is not enabled in service wiring yet. It returns
raw bytes for context.capture to validate and retain, not an implementation brief.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable

from controller.prepare.context import MAX_SOURCE_BYTES, Content, Source, SourceUnavailable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NotionReader:
    def __init__(self, key: Callable[[], str], allowed_pages: frozenset[str], *, opener=None):
        self._key = key
        self._pages = frozenset(str(uuid.UUID(p)) for p in allowed_pages)
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    def __call__(self, source: Source) -> Content:
        try:
            page = str(uuid.UUID(source.source_id))
        except (ValueError, AttributeError):
            raise SourceUnavailable("not-authorized") from None
        if source.kind != "notion" or page not in self._pages:
            raise SourceUnavailable("not-authorized")
        key = self._key()
        if not key:
            raise SourceUnavailable("permission-denied")
        request = urllib.request.Request(
            f"https://api.notion.com/v1/pages/{page}/markdown",
            headers={"Authorization": f"Bearer {key}", "Notion-Version": "2026-03-11"},
            method="GET",
        )
        try:
            with self._open(request, timeout=30) as response:
                data = response.read(MAX_SOURCE_BYTES + 1)
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
            reason = (
                "permission-denied"
                if status in (401, 403)
                else "missing-or-inaccessible"
                if status == 404
                else "redirect-refused"
                if 300 <= status < 400
                else "temporarily-unavailable"
                if status == 429 or status >= 500
                else "invalid-response"
            )
            raise SourceUnavailable(reason) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise SourceUnavailable("temporarily-unavailable") from None
        if len(data) > MAX_SOURCE_BYTES:
            raise SourceUnavailable("too-large")
        if media_type != "application/json":
            raise SourceUnavailable("invalid-response")
        return Content(data, media_type)
