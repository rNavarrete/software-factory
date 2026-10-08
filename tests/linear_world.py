"""An in-memory Linear for the ENG-178 tests: a ``Transport`` that keeps comments.

``FakeLinear`` answers the four GraphQL requests the reporter and the reply
reader make (viewer, one comment by id, comment create, an issue's comments,
paged) and can be told to misbehave the ways the real one does: be down,
rate limit, refuse, lose the answer after creating a comment, refuse a
duplicate id, hide a comment from one lookup. Nothing here touches the network.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from controller.report.linear_api import LinearDown, LinearRefused

FACTORY = "user-factory"
ROLANDO = "user-rolando"
OTHER = "user-other"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def issue_uuid(name: str) -> str:
    """The fake's UUID for a ticket named by key (or anything else)."""
    if _UUID_RE.fullmatch(name):
        return name
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"linear-issue:{name}"))


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class FakeComment:
    id: str
    issue_id: str
    body: str
    user_id: str | None
    created_at: datetime
    parent_id: str | None = None
    edited_at: datetime | None = None
    user_name: str = ""
    user_app: bool = False
    bot: Mapping[str, str] | None = None
    external: bool = False

    def node(self) -> dict[str, object]:
        return {
            "id": self.id,
            "body": self.body,
            "createdAt": iso(self.created_at),
            "editedAt": iso(self.edited_at) if self.edited_at else None,
            "user": (
                None
                if self.user_id is None
                else {"id": self.user_id, "name": self.user_name, "app": self.user_app}
            ),
            "botActor": dict(self.bot) if self.bot else None,
            "externalUser": {"id": "ext-1"} if self.external else None,
            "parent": {"id": self.parent_id} if self.parent_id else None,
        }


@dataclass
class FakeLinear:
    viewer_id: str = FACTORY
    page_size: int = 100
    comments: dict[str, FakeComment] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    down: LinearDown | None = None
    """Raised on every call while set."""
    fail_next: list[Exception] = field(default_factory=list)
    """Raised, one per call, before anything else."""
    lose_next_create: int = 0
    """Creates that succeed and then raise LinearDown, as a lost answer would."""
    hide_next_lookup: int = 0
    """Lookups by id that answer "not found" although the comment exists."""
    missing_as_null: bool = False
    """Answer a lookup of a missing comment with null instead of an error."""
    refuse_create: dict[str, LinearRefused] = field(default_factory=dict)
    """issue_id -> refusal for every create on that issue."""
    unconfirmed_create: int = 0
    """Creates that make the comment but answer success: false."""
    extra_nodes: list[object] = field(default_factory=list)
    """Raw nodes appended to the first page of every comments read."""
    missing_issues: set[str] = field(default_factory=set)
    """Ticket names Linear answers "not found" for."""
    refuse_lookup: LinearRefused | None = None
    """Raised by every comment lookup by id while set."""
    _clock: int = 0

    # --- the transport -----------------------------------------------------------

    def __call__(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((query, dict(variables)))
        if self.fail_next:
            raise self.fail_next.pop(0)
        if self.down is not None:
            raise self.down
        if "viewer" in query:
            return {"viewer": {"id": self.viewer_id, "name": "Factory", "app": False}}
        if "commentCreate" in query:
            return self._create(variables["input"])  # type: ignore[arg-type]
        if "comments(first" in query:
            return self._page(str(variables["id"]), variables.get("after"))
        if "comment(id" in query:
            if self.refuse_lookup is not None:
                raise self.refuse_lookup
            return self._lookup(str(variables["id"]))
        if "issue(id" in query:
            return {"issue": {"id": self.resolve(str(variables["id"]))}}
        raise AssertionError(f"unexpected query: {query}")

    def _lookup(self, cid: str) -> Mapping[str, object]:
        c = self.comments.get(cid)
        if c is not None and self.hide_next_lookup:
            self.hide_next_lookup -= 1
            c = None
        if c is None:
            if self.missing_as_null:
                return {"comment": None}
            raise LinearRefused("Entity not found: Comment", "INVALID_INPUT")
        return {
            "comment": {
                "id": c.id,
                "issue": {"id": c.issue_id},
                "user": {"id": c.user_id} if c.user_id else None,
            }
        }

    def resolve(self, name: str) -> str:
        if name in self.missing_issues:
            raise LinearRefused("Entity not found: Issue", "INVALID_INPUT")
        return issue_uuid(name)

    def _create(self, data: Mapping[str, object]) -> Mapping[str, object]:
        issue = self.resolve(str(data["issueId"]))
        if issue in self.refuse_create:
            raise self.refuse_create[issue]
        cid = str(data["id"])
        if cid in self.comments:
            # Linear refuses a second comment with an id it already has.
            raise LinearRefused("Comment with this id already exists", "CONFLICT")
        self.add(issue, str(data["body"]), self.viewer_id, cid=cid)
        if self.lose_next_create:
            self.lose_next_create -= 1
            raise LinearDown("Linear could not be reached (TimeoutError)")
        if self.unconfirmed_create:
            self.unconfirmed_create -= 1
            return {"commentCreate": {"success": False, "comment": None}}
        return {"commentCreate": {"success": True, "comment": {"id": cid}}}

    def _page(self, name: str, after: object) -> Mapping[str, object]:
        issue = self.resolve(name)
        nodes: list[object] = [
            c.node()
            for c in sorted(self.comments.values(), key=lambda c: c.created_at)
            if c.issue_id == issue
        ]
        start = int(str(after)) if after else 0
        page = nodes[start : start + self.page_size]
        if start == 0:
            page = page + list(self.extra_nodes)
        end = start + self.page_size
        return {
            "issue": {
                "id": issue,
                "comments": {
                    "nodes": page,
                    "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
                },
            }
        }

    # --- what tests do to it ------------------------------------------------------

    def add(
        self,
        issue_id: str,
        body: str,
        user_id: str | None = ROLANDO,
        *,
        parent: str | None = None,
        cid: str | None = None,
        **extra: object,
    ) -> FakeComment:
        self._clock += 1
        cid = cid or f"c-{self._clock:04d}"
        c = FakeComment(
            cid,
            issue_uuid(issue_id),
            body,
            user_id,
            T0 + timedelta(seconds=self._clock),
            parent_id=parent,
            **extra,  # type: ignore[arg-type]
        )
        self.comments[cid] = c
        return c

    def edit(self, cid: str, body: str | None = None) -> None:
        c = self.comments[cid]
        if body is not None:
            c.body = body
        c.edited_at = c.created_at + timedelta(minutes=1)

    def delete(self, cid: str) -> None:
        del self.comments[cid]

    def on(self, issue_id: str) -> list[FakeComment]:
        return [c for c in self.comments.values() if c.issue_id == issue_uuid(issue_id)]

    def creates(self) -> int:
        return sum(1 for q, _ in self.calls if "commentCreate" in q)


# --- a fake opener for HttpTransport ---------------------------------------------


class Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class Opener:
    """Records requests; answers with a body or raises what it is given."""

    def __init__(self, answer: object, *, first: Sequence[object] = ()) -> None:
        self.answer = answer
        self.first = list(first)
        """Answers given, in order, before ``answer`` (e.g. the viewer check)."""
        self.requests: list[object] = []
        self.timeouts: list[object] = []

    def __call__(self, req: object, timeout: object = None) -> Resp:
        self.requests.append(req)
        self.timeouts.append(timeout)
        answer = self.first.pop(0) if self.first else self.answer
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return Resp(answer)
        return Resp(json.dumps(answer).encode())


def http_error(code: int, body: object = None) -> urllib.error.HTTPError:
    raw = b"" if body is None else json.dumps(body).encode()
    return urllib.error.HTTPError(
        "https://api.linear.app/graphql", code, "error", {}, io.BytesIO(raw)
    )  # type: ignore[arg-type]
