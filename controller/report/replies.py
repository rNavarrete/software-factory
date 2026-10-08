"""Rolando's answers, observations and time entries, read from Linear (ENG-178).

A comment is untrusted text, like the rest of a ticket. What it can do is
narrow: answer one of the factory's product questions, record a product
observation on one exact commit, or log time. It can never approve a
contract, a repair, a clearing or a release. Those stay signed records, and
nothing here writes one.

A reply counts only when Linear records all of this:

- Rolando's own Linear user wrote it (``approver_id``), not an app user,
  with no bot (``botActor``: integrations, sync, apps acting for him) and
  no external user (Slack or email sync).
- It was never edited (``editedAt`` is empty). An edited reply is ignored,
  and the reason is returned, so what was read is what he wrote.
- An answer is a reply in the thread of the factory's own question
  comment. An observation is a reply, starting with ``Observation:``, in
  the thread of a factory comment that names one exact commit.

The factory's own comments are recognised by their markers
(``messages.py``) only when the comment was written by the factory's user,
was never edited, and its id matches the key on its ``factory-ref:`` line
(``reporter.comment_id``). Outside text quoted inside one of its comments
therefore can't pass for a question or a candidate.

A recommendation in a question is never taken as an answer, and silence is
never an answer either: no reply means no answer.

Standard library only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from controller.report.linear_api import LinearDown, LinearRefused, Transport
from controller.report.messages import CANDIDATE_MARK, QUESTION_MARK, REF_MARK
from controller.report.reporter import (
    CANDIDATE_PREFIXES,
    QUESTION_PREFIX,
    VIEWER_QUERY,
    comment_id,
)
from controller.service.seams import Answer

COMMENTS_QUERY = """
query($id: String!, $after: String) {
  issue(id: $id) {
    id
    comments(first: 100, after: $after) {
      nodes {
        id body createdAt editedAt
        user { id name app }
        botActor { type name }
        externalUser { id }
        parent { id }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
MAX_PAGES = 20
"""2,000 comments. A ticket with more is not read past that."""
MAX_ENTRY_MINUTES = 12 * 60
_QUESTION_RE = re.compile(
    rf"^{re.escape(QUESTION_MARK)} (?P<key>[A-Za-z0-9_-]+) options=(?P<ids>-|[A-Za-z0-9,]+)$"
)
_CANDIDATE_RE = re.compile(rf"^{re.escape(CANDIDATE_MARK)} (?P<sha>[0-9a-f]{{40}})$")
_REF_RE = re.compile(rf"^{re.escape(REF_MARK)} (?P<key>\S+)$")
_OPTION_PICK_RE = re.compile(
    r"^[*_`\s]*(?:option\s*:?\s+)?(?P<id>[A-Za-z0-9]{1,12})[*_`]*\s*(?:$|[.,:;!)\n]|\s[-\u2013\u2014]\s)",
    re.I,
)
"""The option id alone, or followed by punctuation or a dash: "B", "B.",
"Option B: ...", "**B** - because". "A good point" names nothing."""
_OBSERVATION_RE = re.compile(r"^\s*observation\s*:\s*(?P<text>.+)", re.I | re.S)
_TIME_RE = re.compile(
    r"^\s*time\s*:\s*(?P<n>\d{1,4}(?:\.\d{1,2})?)\s*"
    r"(?P<unit>m|min|mins|minutes?|h|hr|hrs|hours?)\s*$",
    re.I,
)


class ReadFailed(OSError):
    """Linear could not be read; nothing was concluded."""


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    created_at: datetime
    edited: bool
    user_id: str | None
    user_name: str
    user_is_app: bool
    bot: str | None
    external: bool
    parent_id: str | None


@dataclass(frozen=True)
class Observation:
    commit: str
    """The exact commit the comment it replied to names."""
    text: str
    comment_id: str
    at: datetime
    body_sha256: str


@dataclass(frozen=True)
class TimeEntry:
    minutes: int
    comment_id: str
    at: datetime


@dataclass(frozen=True)
class Ignored:
    comment_id: str
    reason: str


@dataclass(frozen=True)
class Reading:
    answers: Sequence[Answer] = ()
    observations: Sequence[Observation] = ()
    time_entries: Sequence[TimeEntry] = ()
    ignored: Sequence[Ignored] = field(default_factory=tuple)

    @property
    def entered_minutes(self) -> int | None:
        if not self.time_entries:
            return None
        return sum(t.minutes for t in self.time_entries)


# --- Parsing -----------------------------------------------------------------------


def _when(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing time")
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("time without a zone")
    return t


def _sub_id(raw: object) -> str | None:
    if isinstance(raw, Mapping) and raw.get("id"):
        return str(raw["id"])
    return None


def parse_comment(raw: Mapping[str, object]) -> Comment:
    user = raw.get("user")
    bot = raw.get("botActor")
    bot_name = None
    if isinstance(bot, Mapping):
        bot_name = " ".join(str(bot.get(k) or "") for k in ("type", "name")).strip() or "a bot"
    return Comment(
        id=str(raw["id"]),
        body=str(raw.get("body") or ""),
        created_at=_when(raw.get("createdAt")),
        edited=bool(raw.get("editedAt")),
        user_id=_sub_id(user),
        user_name=str(user.get("name") or "") if isinstance(user, Mapping) else "",
        user_is_app=bool(user.get("app")) if isinstance(user, Mapping) else False,
        bot=bot_name,
        external=isinstance(raw.get("externalUser"), Mapping),
        parent_id=_sub_id(raw.get("parent")),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Own:
    """One of the factory's own comments, with what its markers say."""

    question_key: str | None = None
    options: tuple[str, ...] = ()
    candidate: str | None = None


def own_markers(c: Comment, issue_id: str, factory_id: str) -> _Own | None:
    """The markers of a factory comment, or None if it isn't one that can
    be trusted (another author, edited, or an id that doesn't match its key)."""
    if c.user_id != factory_id or c.edited or c.bot is not None:
        return None
    # Linear may escape markdown characters when it stores a body; markers
    # never contain a backslash, so dropping them is safe.
    lines = [x.strip().replace("\\", "") for x in c.body.strip().split("\n") if x.strip()]
    if len(lines) < 2:
        return None
    ref = _REF_RE.fullmatch(lines[-1])
    if ref is None or comment_id(issue_id, ref["key"]) != c.id:
        return None
    key, mark = ref["key"], lines[-2]
    if key.startswith(QUESTION_PREFIX):
        m = _QUESTION_RE.fullmatch(mark)
        if m:
            ids = () if m["ids"] == "-" else tuple(i for i in m["ids"].split(",") if i)
            return _Own(question_key=m["key"], options=ids)
    if key.startswith(CANDIDATE_PREFIXES):
        m = _CANDIDATE_RE.fullmatch(mark)
        if m:
            return _Own(candidate=m["sha"])
    return _Own()


def author_problem(c: Comment, approver_id: str) -> str | None:
    """Why this comment can't count as Rolando's own words, or None."""
    if c.bot is not None:
        return f"it was posted by an integration or app ({c.bot}), not by Rolando in Linear"
    if c.external:
        return "it came through a synced integration (such as Slack or email), not from Linear"
    if c.user_id is None:
        return "Linear does not record who wrote it"
    if c.user_is_app:
        return "it was written by an app account"
    if c.user_id != approver_id:
        return "it was written by someone other than Rolando"
    if c.edited:
        return "it was edited after it was posted; post a new reply instead"
    return None


def picked_option(text: str, options: Sequence[str]) -> str | None:
    """The option a reply names, if it opens with exactly one of them."""
    if not options:
        return None
    m = _OPTION_PICK_RE.match(text.strip())
    if m is None:
        return None
    by_lower = {o.lower(): o for o in options}
    return by_lower.get(m["id"].lower())


def time_minutes(text: str) -> int | None:
    first = text.strip().split("\n", 1)[0]
    m = _TIME_RE.fullmatch(first)
    if m is None:
        return None
    n = float(m["n"])
    minutes = n * 60 if m["unit"].lower().startswith("h") else n
    return round(minutes)


def judge(comments: Sequence[Comment], issue_id: str, approver_id: str, factory_id: str) -> Reading:
    """Everything the comments on one ticket validly say. Pure."""
    ordered = sorted(comments, key=lambda c: (c.created_at, c.id))
    own: dict[str, _Own] = {}
    for c in ordered:
        mark = own_markers(c, issue_id, factory_id)
        if mark is not None:
            own[c.id] = mark
    answers: list[Answer] = []
    observations: list[Observation] = []
    entries: list[TimeEntry] = []
    ignored: list[Ignored] = []
    for c in ordered:
        if c.id in own or c.user_id == factory_id:
            continue
        parent = own.get(c.parent_id or "")
        minutes = time_minutes(c.body)
        observed = _OBSERVATION_RE.match(c.body)
        relevant = (
            (parent is not None and (parent.question_key or parent.candidate))
            or minutes is not None
            or observed is not None
        )
        if not relevant:
            continue  # ordinary discussion: neither read nor reported
        problem = author_problem(c, approver_id)
        if problem is not None:
            ignored.append(Ignored(c.id, f"The factory ignored a reply: {problem}."))
            continue
        if minutes is not None:
            if 0 < minutes <= MAX_ENTRY_MINUTES:
                entries.append(TimeEntry(minutes, c.id, c.created_at))
            else:
                ignored.append(
                    Ignored(c.id, f"The factory ignored a time entry of {minutes} minutes.")
                )
            continue
        if parent is not None and parent.question_key:
            answers.append(
                Answer(
                    question_key=parent.question_key,
                    option=picked_option(c.body, parent.options),
                    text=c.body,
                    comment_id=c.id,
                    at=c.created_at,
                    body_sha256=_sha(c.body),
                )
            )
        elif parent is not None and parent.candidate and observed is not None:
            observations.append(
                Observation(
                    commit=parent.candidate,
                    text=observed["text"].strip(),
                    comment_id=c.id,
                    at=c.created_at,
                    body_sha256=_sha(c.body),
                )
            )
        elif observed is not None:
            ignored.append(
                Ignored(
                    c.id,
                    "The factory did not record an observation: reply in the thread of the"
                    " factory comment that names the commit it is about.",
                )
            )
    return Reading(tuple(answers), tuple(observations), tuple(entries), tuple(ignored))


# --- Reading Linear ----------------------------------------------------------------


@dataclass
class LinearDecisions:
    """``DecisionReader`` for the service, plus observations and time."""

    transport: Transport
    approver_id: str
    factory_user_id: str | None = None
    _viewer: str | None = None

    def factory_id(self) -> str:
        bound = getattr(self.transport, "checked_viewer", None)
        if bound is not None:
            try:
                vid = str(bound())  # checked per key value (see HttpTransport)
            except (LinearDown, LinearRefused) as e:
                raise ReadFailed(str(e)) from None
        elif self._viewer is not None:
            return self._viewer
        else:
            viewer = self._call(VIEWER_QUERY, {}).get("viewer")
            vid = str(viewer.get("id") or "") if isinstance(viewer, Mapping) else ""
        if not vid:
            raise ReadFailed("Linear did not say whose key this is")
        if vid == self.approver_id:
            # Its own comments would then be indistinguishable from his.
            raise ReadFailed("the factory's Linear key acts as Rolando; nothing is read")
        if self.factory_user_id and vid != self.factory_user_id:
            raise ReadFailed("the factory's Linear key acts as a different user than configured")
        self._viewer = vid
        return vid

    def comments(self, issue_id: str) -> list[Comment]:
        return self._comments(issue_id)[1]

    def _comments(self, issue_id: str) -> tuple[str, list[Comment]]:
        """(the ticket's UUID, its comments). The ticket may be named by key."""
        uid = issue_id
        out: list[Comment] = []
        seen: set[str] = set()
        after: str | None = None
        for _ in range(MAX_PAGES):
            data = self._call(COMMENTS_QUERY, {"id": issue_id, "after": after})
            issue = data.get("issue")
            if not isinstance(issue, Mapping):
                raise ReadFailed("Linear did not return the ticket")
            uid = str(issue.get("id") or uid)
            conn = issue.get("comments") or {}
            for raw in conn.get("nodes") or ():  # type: ignore[union-attr]
                if not isinstance(raw, Mapping):
                    continue  # malformed: never counts
                try:
                    c = parse_comment(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                if c.id not in seen:  # a page repeated by Linear counts once
                    seen.add(c.id)
                    out.append(c)
            page = conn.get("pageInfo") or {}  # type: ignore[union-attr]
            if not page.get("hasNextPage"):
                break
            after = str(page.get("endCursor") or "")
            if not after:
                break
        return uid, out

    def read(self, issue_id: str) -> Reading:
        me = self.factory_id()
        uid, comments = self._comments(issue_id)
        return judge(comments, uid, self.approver_id, me)

    def answers(self, issue_id: str) -> Sequence[Answer]:
        return self.read(issue_id).answers

    def _call(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        try:
            return self.transport(query, variables)
        except (LinearDown, LinearRefused) as e:
            raise ReadFailed(str(e)) from None


__all__ = [
    "Comment",
    "Ignored",
    "LinearDecisions",
    "Observation",
    "ReadFailed",
    "Reading",
    "TimeEntry",
    "author_problem",
    "judge",
    "own_markers",
    "parse_comment",
    "picked_option",
    "time_minutes",
]
