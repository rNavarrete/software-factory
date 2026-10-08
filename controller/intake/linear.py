"""Rolando's Todo moves, read from Linear's own change history (ENG-174).

Seeing a ticket in Todo proves nothing: a bot, an import, an automation or
another person could have put it there, and its text could have changed
since. So intake reads each ticket's history (Linear's ``IssueHistory``,
over the GraphQL API) and counts a move only when Linear itself records all
of this:

- an entry that changes the state from another state into Todo, made after
  the project was onboarded (``intake_since``);
- made by Rolando's Linear user (``approver_linear_user_id``), who is not an
  app user, with no ``botActor`` (integrations, the GitHub sync, API apps
  acting for him), no ``workflowMetadata`` (Linear automations and agents)
  and no ``issueImport``;
- the ticket is still in Todo, and nothing that defines the work (title,
  description, labels, project, team, parent) changed after the move;
- the move is at least ``settle`` old, so a description edit Linear has not
  yet written to history is caught by the check above or by ``standing``;
- the ticket may be started under the project's onboarding rule (an
  explicit ``issues`` list, and no ``skip_labels``), which is how baseline
  tasks in the same project stay out of the factory.

The move's history entry id is its ``event_id``, so a replayed or
overlapping poll gives the same id and the service ignores it. The exact
ticket text is pinned as ``revision``: the sha256 of the ticket's title,
description, labels, project, team and parent at the time it is read, which
is the text at the time of the move because nothing changed since.

A ticket created straight in Todo, or imported there, has no move and does
not start; intake says so once on the ticket. A move by anyone or anything
else is refused with the reason, once.

What this can't prove: that a change made through an integration acting as
Rolando always carries a ``botActor``. Linear's schema says ``actor`` "may be
empty in the case of integrations or automations" and that ``botActor`` is
"the bot that performed the action"; how a given OAuth app acting for him is
recorded is checked on the live path (``python3 -m controller.intake probe``)
before intake is switched on. Until then it stays off.

Standard library only. All network access goes through ``Transport``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from controller.ledger.redact import redact
from controller.service.seams import Authorization, Control, IntakeBatch, Refusal, Standing

API = "https://api.linear.app/graphql"
TODO = "Todo"
PAUSE_LABEL = "factory-pause"
"""Added by Rolando to a project's status ticket: pause. Removed: resume."""
SETTLE = timedelta(minutes=2)
OVERLAP = timedelta(minutes=15)
"""How far back each poll reaches before its cursor. Overlap is harmless:
the service ignores event ids it has already recorded."""
_DONE_TYPES = frozenset({"completed", "canceled"})
_STANDING_TYPES = frozenset({"unstarted", "started"})
"""States in which an accepted move still stands: Todo itself, or a started
state (the factory or Rolando moved it to In Progress)."""
_NAME_LIMIT = 80


class LinearUnavailable(OSError):
    """Linear could not be read. Nothing is recorded; the next round retries."""


class IntakeBlocked(RuntimeError):
    """Intake can't prove who moved a ticket, so it reads nothing at all."""


# --- What the rules read -----------------------------------------------------------


@dataclass(frozen=True)
class State:
    id: str
    name: str
    type: str


@dataclass(frozen=True)
class Change:
    """One Linear history entry, reduced to what the rules use."""

    id: str
    at: datetime
    actor_id: str | None
    actor_name: str
    actor_is_app: bool
    bot: str | None
    """The ``botActor`` (type and name), if Linear recorded one."""
    automation: bool
    """A Linear workflow, triage rule or auto-close/archive made it."""
    imported: bool
    from_state: State | None
    to_state: State | None
    content: tuple[str, ...]
    """Which parts of the work it changed: title, description, labels,
    project, team, parent."""
    added_labels: tuple[str, ...] = ()
    removed_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ticket:
    id: str
    key: str
    title: str
    description: str
    project_id: str | None
    team_id: str | None
    parent_id: str | None
    state: State
    labels: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    gone: bool
    """Trashed or archived."""
    blockers: tuple[tuple[str, str], ...]
    """(key, state type) of each ticket that blocks this one."""
    changes: tuple[Change, ...]
    """Oldest first."""


def revision(t: Ticket) -> str:
    """The exact text of the work, pinned: what the contract is drafted from."""
    body = {
        "id": t.id,
        "key": t.key,
        "title": t.title,
        "description": t.description,
        "project_id": t.project_id,
        "team_id": t.team_id,
        "parent_id": t.parent_id,
        "labels": sorted(t.labels),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ProjectRule:
    project_id: str
    issues: frozenset[str] | None = None
    """Only these ticket keys may start, if set (the pilot's factory arm)."""
    skip_labels: frozenset[str] = frozenset()
    """A ticket with any of these labels never starts (e.g. ``baseline``)."""
    status_issue_id: str | None = None


@dataclass(frozen=True)
class IntakePolicy:
    approver_id: str
    """Rolando's Linear user id."""
    projects: Mapping[str, ProjectRule]
    since: datetime
    """Moves before this are never read: onboarding is not retroactive."""
    todo: str = TODO
    settle: timedelta = SETTLE

    def status_issues(self) -> set[str]:
        return {p.status_issue_id for p in self.projects.values() if p.status_issue_id}


# --- The rules ---------------------------------------------------------------------


def _is_move_into(c: Change, todo: str) -> bool:
    return (
        c.to_state is not None
        and c.to_state.name == todo
        and c.from_state is not None
        and c.from_state.id != c.to_state.id
        and c.from_state.name != todo  # a team move keeps it in Todo: not a move into it
    )


def attribution_problem(c: Change, approver_id: str) -> str | None:
    """Why this change can't count as Rolando's own action, or None."""
    if c.imported:
        return "it came from an import, not from a move you made"
    if c.bot is not None:
        return f"it was made by an integration or app ({c.bot}), not by you in Linear"
    if c.automation:
        return "it was made by a Linear automation, not by you"
    if c.actor_id is None:
        return "Linear does not record who made it"
    if c.actor_is_app:
        return f"it was made by an app account ({c.actor_name})"
    if c.actor_id != approver_id:
        return f"it was made by {c.actor_name}; only Rolando's own move starts work"
    return None


def changed_after(t: Ticket, at: datetime) -> list[str]:
    parts: list[str] = []
    for c in t.changes:
        if c.at > at:
            parts += [p for p in c.content if p not in parts]
    return parts


def judge(t: Ticket, policy: IntakePolicy, now: datetime) -> Authorization | Refusal | None:
    """The ticket's latest Todo move as an authorization, a refusal, or None
    (nothing to say yet: no new move, it left Todo again, or it is settling)."""
    rule = policy.projects.get(t.project_id or "")
    if rule is None or t.id in policy.status_issues() or t.gone:
        return None
    moves = [c for c in t.changes if _is_move_into(c, policy.todo) and c.at >= policy.since]
    if not moves:
        entered = [c for c in t.changes if c.to_state is not None]
        if (
            t.state.name == policy.todo
            and t.created_at >= policy.since
            and all(c.to_state is None or c.to_state.name == policy.todo for c in entered)
        ):
            return Refusal(
                f"created-{t.id}",
                t.id,
                t.key,
                "it was created or imported straight into Todo, and only a move into Todo"
                " starts work. Move it to Backlog and back to Todo to start it.",
            )
        return None
    move = moves[-1]
    if t.state.name != policy.todo or any(
        c.at > move.at and c.to_state is not None for c in t.changes
    ):
        return None  # it left Todo again; a later move back is a new event
    problem = attribution_problem(move, policy.approver_id)
    if problem is not None:
        return Refusal(move.id, t.id, t.key, f"the move to Todo doesn't count: {problem}.")
    if now - move.at < policy.settle:
        return None
    edited = changed_after(t, move.at)
    if edited:
        return Refusal(
            move.id,
            t.id,
            t.key,
            f"the ticket was changed after it was moved to Todo ({', '.join(edited)}), so the"
            " factory can't tell which text you approved. Move it out of Todo and back to"
            " start from the text as it is now.",
        )
    if rule.issues is not None and t.key not in rule.issues:
        return Refusal(
            move.id,
            t.id,
            t.key,
            "it isn't one of the tickets the factory takes in this project (for example, a"
            " baseline task). Nothing was started.",
        )
    skipped = sorted(set(t.labels) & rule.skip_labels)
    if skipped:
        return Refusal(
            move.id,
            t.id,
            t.key,
            f"it has the label {skipped[0]!r}, which the factory never starts.",
        )
    return Authorization(
        event_id=move.id,
        issue_id=t.id,
        issue_key=t.key,
        project_id=rule.project_id,
        actor=f"{move.actor_name} (Linear user {move.actor_id})",
        moved_at=move.at,
        revision=revision(t),
        evidence=(
            f"Linear history entry {move.id}: {move.from_state.name if move.from_state else '?'}"
            f" to {policy.todo} by user {move.actor_id}, no bot, integration, import or"
            " automation; nothing that defines the work changed after it."
        ),
    )


def standing(t: Ticket | None, a: Authorization, policy: IntakePolicy) -> Standing:
    """Does an accepted move still stand, right now?"""
    if t is None or t.gone:
        return Standing(withdrawn="the ticket was deleted or archived")
    if t.project_id != a.project_id or t.project_id not in policy.projects:
        return Standing(withdrawn="the ticket moved to a project the factory doesn't work on")
    if t.state.type not in _STANDING_TYPES or (
        t.state.type == "unstarted" and t.state.name != policy.todo
    ):
        return Standing(withdrawn=f"the ticket was moved to {t.state.name}")
    changed = None
    edited = changed_after(t, a.moved_at)
    if edited or revision(t) != a.revision:
        what = ", ".join(edited) if edited else "its text"
        changed = f"the ticket changed after it was moved to Todo ({what})"
    waiting = tuple(sorted(k for k, kind in t.blockers if kind not in _DONE_TYPES))
    return Standing(changed=changed, waiting_on=waiting)


def controls(t: Ticket, policy: IntakePolicy) -> list[Control]:
    """Rolando adding or removing the pause label on a status ticket."""
    out = []
    for c in t.changes:
        if c.at < policy.since or attribution_problem(c, policy.approver_id) is not None:
            continue
        if PAUSE_LABEL in c.added_labels:
            action = "pause"
        elif PAUSE_LABEL in c.removed_labels:
            action = "resume"
        else:
            continue
        out.append(
            Control(c.id, action, c.actor_name, c.at, f"label {PAUSE_LABEL} on {t.key}", t.id)
        )
    return out


# --- Reading Linear's answers ------------------------------------------------------

_HISTORY_FIELDS = """
  id createdAt
  actor { id name app }
  botActor { type name }
  issueImport { id }
  workflowMetadata { __typename } triageRuleMetadata { __typename }
  autoClosed autoArchived triageResponsibilityAutoAssigned
  fromState { id name type } toState { id name type }
  updatedDescription fromTitle toTitle
  fromProjectId toProjectId fromTeamId toTeamId fromParentId toParentId
  addedLabels { name } removedLabels { name }
"""

_ISSUE_FIELDS = f"""
  id identifier title description createdAt updatedAt trashed archivedAt
  project {{ id }} team {{ id }} parent {{ id }}
  state {{ id name type }}
  labels(first: 50) {{ nodes {{ name }} }}
  inverseRelations(first: 50) {{ nodes {{ type issue {{ identifier state {{ type }} }} }} }}
  history(first: 50) {{ nodes {{ {_HISTORY_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }}
"""

ISSUES_QUERY = f"""
query FactoryIntake($projects: [ID!], $since: DateTimeOrDuration, $after: String) {{
  issues(first: 25, after: $after, includeArchived: true,
         filter: {{ project: {{ id: {{ in: $projects }} }}, updatedAt: {{ gte: $since }} }}) {{
    nodes {{ {_ISSUE_FIELDS} }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

ISSUE_QUERY = f"""
query FactoryIssue($id: String!) {{ issue(id: $id) {{ {_ISSUE_FIELDS} }} }}
"""

HISTORY_QUERY = f"""
query FactoryHistory($id: String!, $after: String) {{
  issue(id: $id) {{
    history(first: 50, after: $after) {{
      nodes {{ {_HISTORY_FIELDS} }} pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

VIEWER_QUERY = "query FactoryViewer { viewer { id name app } }"


def _when(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r}")
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without a zone: {value!r}")
    return t


def _state(raw: object) -> State | None:
    if not isinstance(raw, Mapping):
        return None
    return State(str(raw["id"]), str(raw["name"]), str(raw["type"]))


def _names(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(x["name"]) for x in raw if isinstance(x, Mapping) and "name" in x)


def _short(text: object) -> str:
    s = " ".join(str(text or "").split())
    return s[:_NAME_LIMIT] or "someone"


def parse_change(raw: Mapping[str, object]) -> Change:
    actor = raw.get("actor") if isinstance(raw.get("actor"), Mapping) else None
    bot = raw.get("botActor") if isinstance(raw.get("botActor"), Mapping) else None
    content = []
    if raw.get("updatedDescription"):
        content.append("description")
    if raw.get("fromTitle") is not None or raw.get("toTitle") is not None:
        content.append("title")
    if raw.get("addedLabels") or raw.get("removedLabels"):
        content.append("labels")
    for part in ("Project", "Team", "Parent"):
        if raw.get(f"from{part}Id") != raw.get(f"to{part}Id"):
            content.append(part.lower())
    return Change(
        id=str(raw["id"]),
        at=_when(raw["createdAt"]),
        actor_id=None if actor is None else str(actor["id"]),
        actor_name=_short(None if actor is None else actor.get("name")),
        actor_is_app=bool(actor is not None and actor.get("app")),
        bot=None if bot is None else _short(f"{bot.get('type')}: {bot.get('name') or ''}"),
        automation=bool(
            raw.get("workflowMetadata")
            or raw.get("triageRuleMetadata")
            or raw.get("autoClosed")
            or raw.get("autoArchived")
            or raw.get("triageResponsibilityAutoAssigned")
        ),
        imported=raw.get("issueImport") is not None,
        from_state=_state(raw.get("fromState")),
        to_state=_state(raw.get("toState")),
        content=tuple(content),
        added_labels=_names(raw.get("addedLabels")),
        removed_labels=_names(raw.get("removedLabels")),
    )


def parse_ticket(raw: Mapping[str, object], history: Iterable[Mapping[str, object]]) -> Ticket:
    def nid(key: str) -> str | None:
        v = raw.get(key)
        return str(v["id"]) if isinstance(v, Mapping) and v.get("id") else None

    labels = raw.get("labels")
    relations = raw.get("inverseRelations")
    blockers = []
    for r in (relations or {}).get("nodes", ()) if isinstance(relations, Mapping) else ():
        if r.get("type") == "blocks" and isinstance(r.get("issue"), Mapping):
            other = r["issue"]
            blockers.append((str(other["identifier"]), str((other.get("state") or {}).get("type"))))
    state = _state(raw.get("state"))
    if state is None:
        raise ValueError("ticket without a state")
    changes = sorted((parse_change(h) for h in history), key=lambda c: (c.at, c.id))
    return Ticket(
        id=str(raw["id"]),
        key=str(raw["identifier"]),
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        project_id=nid("project"),
        team_id=nid("team"),
        parent_id=nid("parent"),
        state=state,
        labels=_names(labels.get("nodes") if isinstance(labels, Mapping) else None),
        created_at=_when(raw["createdAt"]),
        updated_at=_when(raw["updatedAt"]),
        gone=bool(raw.get("trashed") or raw.get("archivedAt")),
        blockers=tuple(blockers),
        changes=tuple(changes),
    )


# --- The transport -----------------------------------------------------------------

Transport = Callable[[str, Mapping[str, object]], Mapping[str, object]]
"""``(query, variables) -> data``. Raises LinearUnavailable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class HttpTransport:
    """POSTs to Linear's GraphQL API with the factory's own Linear key.
    Only api.linear.app, no redirects, and the key never appears in errors."""

    def __init__(self, key: Callable[[], str], opener: Callable[..., object] | None = None):
        self._key = key
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    def __call__(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        key = self._key()
        auth = key if key.startswith("lin_api_") else f"Bearer {key}"
        req = urllib.request.Request(
            API,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": auth,
                "User-Agent": "software-factory-intake",
            },
            method="POST",
        )
        try:
            with self._open(req, timeout=60) as resp:  # type: ignore[attr-defined]
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LinearUnavailable(f"Linear answered HTTP {e.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise LinearUnavailable(f"Linear could not be read ({type(e).__name__})") from None
        if not isinstance(body, Mapping) or body.get("errors") or "data" not in body:
            errors = body.get("errors") if isinstance(body, Mapping) else None
            first = errors[0].get("message") if isinstance(errors, list) and errors else None
            text = _short(first).replace(key, "[key]")
            raise LinearUnavailable(f"Linear refused the query: {redact(text)}")
        return body["data"]


# --- The source the service polls -------------------------------------------------


@dataclass
class LinearSource:
    """``AuthorizationSource`` for the service (see seams.py).

    ``policy`` is read every poll (it comes from the onboarding file).
    Before reading anything, it checks once that the factory's own Linear key
    does not act as Rolando: if it did, the factory's own changes would be
    recorded as his, and a move it made could pass for his.
    """

    transport: Transport
    policy: Callable[[], IntakePolicy]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _viewer_ok: bool = field(default=False, init=False)

    def check_identity(self) -> None:
        if self._viewer_ok:
            return
        data = self.transport(VIEWER_QUERY, {})
        viewer = data.get("viewer")
        if not isinstance(viewer, Mapping) or not viewer.get("id"):
            raise LinearUnavailable("Linear did not say whose key this is")
        if str(viewer["id"]) == self.policy().approver_id:
            raise IntakeBlocked(
                "the factory's Linear key acts as Rolando, so its own changes would look like"
                " his. Intake reads nothing until the factory has its own Linear identity."
            )
        self._viewer_ok = True

    def _history(self, issue: Mapping[str, object]) -> list[Mapping[str, object]]:
        conn = issue.get("history") or {}
        nodes = list(conn.get("nodes") or ())  # type: ignore[union-attr]
        page = conn.get("pageInfo") or {}  # type: ignore[union-attr]
        while page.get("hasNextPage"):
            data = self.transport(HISTORY_QUERY, {"id": issue["id"], "after": page["endCursor"]})
            conn = (data.get("issue") or {}).get("history") or {}  # type: ignore[union-attr]
            nodes += conn.get("nodes") or ()
            page = conn.get("pageInfo") or {}
        return nodes

    def fetch(self, issue_id: str) -> Ticket | None:
        try:
            data = self.transport(ISSUE_QUERY, {"id": issue_id})
        except LinearUnavailable as e:
            if "not found" in str(e).lower():
                return None  # deleted for good: Linear answers with an error
            raise
        raw = data.get("issue")
        if not isinstance(raw, Mapping):
            return None
        return parse_ticket(raw, self._history(raw))

    def poll(self, cursor: str | None) -> IntakeBatch:
        self.check_identity()
        policy = self.policy()
        now = self.now()
        since = policy.since if cursor is None else max(policy.since, _when(cursor) - OVERLAP)
        tickets: list[Ticket] = []
        after = None
        while True:
            data = self.transport(
                ISSUES_QUERY,
                {
                    "projects": sorted(policy.projects),
                    "since": since.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "after": after,
                },
            )
            conn = data.get("issues") or {}
            for raw in conn.get("nodes") or ():  # type: ignore[union-attr]
                tickets.append(parse_ticket(raw, self._history(raw)))
            page = conn.get("pageInfo") or {}  # type: ignore[union-attr]
            if not page.get("hasNextPage"):
                break
            after = page["endCursor"]
        authorizations: list[Authorization] = []
        refusals: list[Refusal] = []
        high = since if cursor is None else _when(cursor)
        hold: datetime | None = None
        for t in tickets:
            high = max(high, t.updated_at)
            verdict = judge(t, policy, now)
            if isinstance(verdict, Authorization):
                authorizations.append(verdict)
            elif isinstance(verdict, Refusal):
                refusals.append(verdict)
            elif _settling(t, policy, now):
                # Read again next poll: keep the cursor at or before it.
                hold = t.updated_at if hold is None else min(hold, t.updated_at)
        found: list[Control] = []
        for status_id in sorted(policy.status_issues()):
            t = self.fetch(status_id)
            if t is not None:
                found += controls(t, policy)
        new_cursor = high if hold is None else min(high, hold)
        return IntakeBatch(
            authorizations=sorted(authorizations, key=lambda a: (a.moved_at, a.event_id)),
            refusals=refusals,
            controls=sorted(found, key=lambda c: (c.at, c.event_id)),
            cursor=new_cursor.astimezone(UTC).isoformat(),
        )

    def standing(self, authorization: Authorization) -> Standing:
        self.check_identity()
        return standing(self.fetch(authorization.issue_id), authorization, self.policy())

    def revalidate(self, authorization: Authorization) -> str | None:
        return self.standing(authorization).reason


def _settling(t: Ticket, policy: IntakePolicy, now: datetime) -> bool:
    moves = [c for c in t.changes if _is_move_into(c, policy.todo) and c.at >= policy.since]
    return bool(moves) and now - moves[-1].at < policy.settle and t.state.name == policy.todo


def policy_from(onboarding: object) -> IntakePolicy:
    """The intake rules from the onboarding file (controller/service/onboarding.py)."""
    approver = getattr(onboarding, "approver_linear_user_id", None)
    since = getattr(onboarding, "intake_since", None)
    if not approver or since is None:
        raise IntakeBlocked(
            "the onboarding file names no approver_linear_user_id or intake_since, so no move"
            " can be attributed"
        )
    rules = {
        pid: ProjectRule(pid, p.issues, p.skip_labels, p.status_issue_id)
        for pid, p in onboarding.projects.items()  # type: ignore[attr-defined]
    }
    return IntakePolicy(approver, rules, since)


__all__: Sequence[str] = [
    "API",
    "OVERLAP",
    "PAUSE_LABEL",
    "SETTLE",
    "TODO",
    "Change",
    "HttpTransport",
    "IntakeBlocked",
    "IntakePolicy",
    "LinearSource",
    "LinearUnavailable",
    "ProjectRule",
    "State",
    "Ticket",
    "Transport",
    "attribution_problem",
    "controls",
    "judge",
    "parse_change",
    "parse_ticket",
    "policy_from",
    "revision",
    "standing",
]
