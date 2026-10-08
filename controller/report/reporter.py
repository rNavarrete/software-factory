"""The service's ``Reporter``, posting on Linear tickets (ENG-178).

Each message is posted at most once, whatever happens. The service hands
every message a unique ``key`` and retries when it can't tell a failed post
from a lost answer, so the comment's id is derived from the key: the same
key always names the same comment. Before creating it, the reporter asks
Linear whether that comment already exists; if it does, the earlier try
worked and nothing more is posted. A restart, a crash between posting and
recording, or a timeout therefore never shows the same message twice.

What it refuses:

- To post as Rolando. If the factory's Linear key acts as his user, every
  post fails and says why. The factory has its own Linear identity, and a
  comment that looked like his could pass for his decision.
- To treat a comment it didn't make as its own: an existing comment under
  the derived id must be on the same ticket and by the factory's user.
- To pass markers through. Only the last line of a question or readiness
  message may carry a ``factory-question:`` or ``factory-candidate:``
  marker, and every comment ends with a ``factory-ref:`` line naming its
  key. ``replies.py`` trusts a marker only in a comment whose id matches
  that key, so outside text quoted in a message can't forge one.

Every body is redacted for secrets and capped in length before it leaves.
When Linear is down or rate limiting the factory, ``post`` raises
``ReportFailed(hold_all=True)`` and the service stops posting for the round.

Standard library only.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from controller.ledger.redact import redact
from controller.report.linear_api import LinearDown, LinearRefused, Transport
from controller.report.messages import CANDIDATE_MARK, QUESTION_MARK, REF_MARK, defuse
from controller.service.seams import ReportFailed

BODY_LIMIT = 12_000
"""Characters. Well under Linear's own limit; a longer message is shortened."""

QUESTION_PREFIX = "question:"
"""Keys of messages that ask Rolando a product question."""
CANDIDATE_PREFIXES = ("ready:", "observe:")
"""Keys of messages that name an exact candidate commit."""

VIEWER_QUERY = "query { viewer { id name app } }"
COMMENT_QUERY = """
query($id: String!) {
  comment(id: $id) { id issue { id } user { id } }
}
"""
ISSUE_QUERY = """
query($id: String!) {
  issue(id: $id) { id }
}
"""
CREATE_MUTATION = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}
"""
_KEY_SPACE_RE = re.compile(r"\s+")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def not_found(e: BaseException) -> bool:
    """Linear's answer for an id that names nothing."""
    return "not found" in str(e).lower()


def comment_id(issue_id: str, key: str) -> str:
    """The comment id for a message: the same issue and key always give the
    same id. Shaped as a version 4 UUID, which is what Linear accepts."""
    digest = hashlib.sha256(f"factory-comment\n{issue_id}\n{key}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def clean_key(key: str) -> str:
    return _KEY_SPACE_RE.sub("_", key.strip())[:200]


def render(text: str, key: str) -> str:
    """The comment body for ``text`` posted under ``key``."""
    key = clean_key(key)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").rstrip().split("\n")
    allowed = _allowed_mark(key)
    trailer = ""
    if allowed and lines and lines[-1].startswith(allowed):
        trailer = lines.pop()
    body = defuse(redact("\n".join(lines))).rstrip()
    ref = f"{REF_MARK} {key}"
    cut = "\n\n(The rest of this message was cut.)"
    room = BODY_LIMIT - len(trailer) - len(ref) - len(cut) - 8  # 8: the joins
    if len(body) > room:
        body = body[: max(room, 0)].rstrip() + cut
    parts = [body] + ([trailer] if trailer else []) + [ref]
    return "\n\n".join(p for p in parts if p)


def _allowed_mark(key: str) -> str:
    if key.startswith(QUESTION_PREFIX):
        return QUESTION_MARK
    if key.startswith(CANDIDATE_PREFIXES):
        return CANDIDATE_MARK
    return ""


@dataclass
class LinearReporter:
    """``Reporter`` for the service (see seams.py)."""

    transport: Transport
    approver_id: str
    """Rolando's Linear user id. The factory never posts as this user."""
    factory_user_id: str | None = None
    """If set, the key must act as exactly this user."""
    _viewer: str | None = None
    _issues: dict[str, str] = field(default_factory=dict)

    def issue_uuid(self, issue_id: str) -> str:
        """Linear's UUID for a ticket named by UUID or by key (``ENG-178``).
        Comment ids are derived from the UUID, so both names give the same
        comment, and the existence check compares like with like."""
        if _UUID_RE.fullmatch(issue_id):
            return issue_id
        if issue_id not in self._issues:
            issue = self._call(ISSUE_QUERY, {"id": issue_id}).get("issue")
            uid = str(issue.get("id") or "") if isinstance(issue, Mapping) else ""
            if not _UUID_RE.fullmatch(uid):
                raise ReportFailed(f"Linear did not return ticket {issue_id!r}")
            self._issues[issue_id] = uid
        return self._issues[issue_id]

    def viewer(self) -> str:
        """The factory's own Linear user id, checked once."""
        if self._viewer is not None:
            return self._viewer
        data = self._call(VIEWER_QUERY, {})
        viewer = data.get("viewer")
        vid = str(viewer.get("id") or "") if isinstance(viewer, Mapping) else ""
        if not vid:
            raise ReportFailed("Linear did not say whose key this is", hold_all=True)
        if vid == self.approver_id:
            raise ReportFailed(
                "the factory's Linear key acts as Rolando, so its comments would look like his."
                " Nothing is posted until the factory has its own Linear identity.",
                hold_all=True,
            )
        if self.factory_user_id and vid != self.factory_user_id:
            raise ReportFailed(
                "the factory's Linear key acts as a different user than the one configured",
                hold_all=True,
            )
        self._viewer = vid
        return vid

    def post(self, issue_id: str, key: str, text: str) -> None:
        me = self.viewer()
        issue_id = self.issue_uuid(issue_id)
        key = clean_key(key)
        cid = comment_id(issue_id, key)
        if self._exists(cid, issue_id, me):
            return
        body = render(text, key)
        try:
            data = self._call(
                CREATE_MUTATION, {"input": {"id": cid, "issueId": issue_id, "body": body}}
            )
        except ReportFailed as e:
            if e.hold_all:
                raise
            # A conflict means the comment was made after all (a lost answer
            # to an earlier try). Anything else is a real failure.
            if self._exists(cid, issue_id, me):
                return
            raise
        result = data.get("commentCreate")
        if not isinstance(result, Mapping) or not result.get("success"):
            if self._exists(cid, issue_id, me):
                return
            raise ReportFailed("Linear did not confirm the comment")

    def _exists(self, cid: str, issue_id: str, me: str) -> bool:
        try:
            data = self._call(COMMENT_QUERY, {"id": cid})
        except ReportFailed as e:
            if e.hold_all or not not_found(e):
                raise
            return False  # Linear answers "not found" as an error
        c = data.get("comment")
        if not isinstance(c, Mapping):
            return False
        on = (c.get("issue") or {}).get("id")  # type: ignore[union-attr]
        by = (c.get("user") or {}).get("id")  # type: ignore[union-attr]
        if on != issue_id or by != me:
            raise ReportFailed(
                "a comment with this message's id exists but is not the factory's own on this"
                " ticket; nothing was posted"
            )
        return True

    def _call(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        try:
            return self.transport(query, variables)
        except LinearDown as e:
            raise ReportFailed(str(e), hold_all=True) from None
        except LinearRefused as e:
            raise ReportFailed(str(e)) from None


__all__ = [
    "BODY_LIMIT",
    "CANDIDATE_PREFIXES",
    "QUESTION_PREFIX",
    "LinearReporter",
    "clean_key",
    "comment_id",
    "not_found",
    "render",
]
