"""Todo intake from Linear's own history (ENG-174): ``controller/intake/linear.py``.

Fixtures are in the shape Linear's GraphQL API answers with (issue nodes,
``IssueHistory`` entries, connections with ``pageInfo``). ``FakeLinear`` is a
``Transport`` that answers the four queries from a small mutable world and
records every call. Nothing here touches the network.
"""

import copy
import http.client
import json
import random
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from io import BytesIO

from controller.intake import linear
from controller.intake.linear import (
    HISTORY_QUERY,
    ISSUE_QUERY,
    ISSUES_QUERY,
    SETTLE,
    VIEWER_QUERY,
    HttpTransport,
    IntakeBlocked,
    IntakePolicy,
    LinearSource,
    LinearUnavailable,
    ProjectRule,
    judge,
    parse_ticket,
    policy_from,
    revision,
    standing,
)
from controller.service.seams import Authorization, Refusal

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=3)
APPROVER = "user-rolando"
FACTORY_USER = "user-factory"
PROJECT = "proj-pilot"
OTHER_PROJECT = "proj-other"
TEAM = "team-eng"
STATUS_KEY = "ENG-100"
STATUS_ID = "iss-eng-100"

BACKLOG = {"id": "st-backlog", "name": "Backlog", "type": "backlog"}
TODO = {"id": "st-todo", "name": "Todo", "type": "unstarted"}
IN_PROGRESS = {"id": "st-progress", "name": "In Progress", "type": "started"}
IN_REVIEW = {"id": "st-review", "name": "In Review", "type": "started"}
DONE = {"id": "st-done", "name": "Done", "type": "completed"}
CANCELED = {"id": "st-canceled", "name": "Canceled", "type": "canceled"}

ROLANDO = {"id": APPROVER, "name": "Rolando Navarrete", "app": False}
MARIA = {"id": "user-maria", "name": "Maria", "app": False}
ZAPIER = {"id": "user-zapier", "name": "Zapier", "app": True}
CLAUDE_BOT = {"type": "oauthClient", "name": "Claude"}
GITHUB_BOT = {"type": "github"}


def iso(t):
    """A time as Linear writes it: ``2026-10-09T12:00:00.000Z``."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def entry(eid, at, *, actor=ROLANDO, **fields):
    """One IssueHistory node, with every field the intake query selects."""
    e = {
        "id": eid,
        "createdAt": iso(at),
        "actor": actor,
        "botActor": None,
        "issueImport": None,
        "workflowMetadata": None,
        "autoClosed": False,
        "autoArchived": False,
        "triageResponsibilityAutoAssigned": False,
        "fromState": None,
        "toState": None,
        "updatedDescription": None,
        "fromTitle": None,
        "toTitle": None,
        "fromProjectId": None,
        "toProjectId": None,
        "fromTeamId": None,
        "toTeamId": None,
        "fromParentId": None,
        "toParentId": None,
        "addedLabels": None,
        "removedLabels": None,
    }
    e.update(fields)
    return e


def issue_node(
    key,
    *,
    state=BACKLOG,
    created=SINCE + timedelta(minutes=10),
    updated=None,
    history=(),
    labels=(),
    project=PROJECT,
    parent=None,
    title=None,
    description=None,
    blockers=(),
    trashed=False,
    archived_at=None,
    has_next=False,
    end_cursor=None,
):
    """One Issue node as the intake queries select it."""
    times = [created] + [linear._when(h["createdAt"]) for h in history]
    return {
        "id": f"iss-{key.lower()}",
        "identifier": key,
        "title": title if title is not None else f"Work for {key}",
        "description": description if description is not None else f"Do {key}.",
        "createdAt": iso(created),
        "updatedAt": iso(updated or max(times)),
        "trashed": trashed,
        "archivedAt": archived_at,
        "project": {"id": project} if project else None,
        "team": {"id": TEAM},
        "parent": {"id": parent} if parent else None,
        "state": state,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"identifier": k, "state": {"type": t}}}
                for k, t in blockers
            ]
        },
        "history": {
            "nodes": list(history),
            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        },
    }


_QUERY_NAMES = {
    ISSUES_QUERY: "issues",
    ISSUE_QUERY: "issue",
    HISTORY_QUERY: "history",
    VIEWER_QUERY: "viewer",
}


class FakeLinear:
    """A ``Transport`` over a small Linear workspace that tests change in place.

    It answers like Linear: ``issues`` filtered by project and ``updatedAt``,
    paged ``page_size`` at a time; ``issue`` by id or key (null if absent);
    ``history`` pages from ``history_pages``; ``viewer`` is the factory's key.
    """

    def __init__(self, viewer_id=FACTORY_USER):
        self.nodes = {}
        self.history_pages = {}
        """(issue id, after) -> (nodes, pageInfo) for HISTORY_QUERY."""
        self.viewer = {"id": viewer_id, "name": "Software Factory", "app": True}
        self.page_size = 25
        self.calls = []
        self.fail = None
        self._n = 0

    # --- the transport ---

    def __call__(self, query, variables):
        name = _QUERY_NAMES.get(query)
        if name is None:
            raise AssertionError(f"unexpected query: {query[:60]!r}")
        self.calls.append((name, dict(variables)))
        if self.fail is not None:
            raise self.fail
        if name == "viewer":
            return {"viewer": dict(self.viewer)}
        if name == "issue":
            node = self.find(variables["id"])
            return {"issue": copy.deepcopy(node)}
        if name == "history":
            nodes, page = self.history_pages[(variables["id"], variables["after"])]
            return {"issue": {"history": {"nodes": copy.deepcopy(nodes), "pageInfo": page}}}
        since = linear._when(variables["since"])
        projects = set(variables["projects"])
        matching = [
            n
            for n in self.nodes.values()
            if n["project"] is not None
            and n["project"]["id"] in projects
            and linear._when(n["updatedAt"]) >= since
        ]
        start = int(variables.get("after") or 0)
        page = matching[start : start + self.page_size]
        more = start + self.page_size < len(matching)
        return {
            "issues": {
                "nodes": copy.deepcopy(page),
                "pageInfo": {
                    "hasNextPage": more,
                    "endCursor": str(start + self.page_size) if more else None,
                },
            }
        }

    def names(self):
        return [n for n, _ in self.calls]

    def variables(self, name):
        return [v for n, v in self.calls if n == name]

    # --- changing the workspace ---

    def find(self, id_or_key):
        for n in self.nodes.values():
            if id_or_key in (n["id"], n["identifier"]):
                return n
        return None

    def add(self, key, **kw):
        node = issue_node(key, **kw)
        self.nodes[node["id"]] = node
        return node

    def record(self, key, at, *, actor=ROLANDO, **fields):
        """Append a history entry; returns its id."""
        self._n += 1
        eid = f"hist-{self._n:04d}"
        node = self.find(key)
        node["history"]["nodes"].append(entry(eid, at, actor=actor, **fields))
        if linear._when(node["updatedAt"]) < at:
            node["updatedAt"] = iso(at)
        return eid

    def move(self, key, to, at, *, actor=ROLANDO, **fields):
        node = self.find(key)
        eid = self.record(key, at, actor=actor, fromState=node["state"], toState=to, **fields)
        node["state"] = to
        return eid

    def edit(self, key, at, *, actor=ROLANDO, description=None, title=None):
        node = self.find(key)
        fields = {}
        if description is not None:
            node["description"] = description
            fields["updatedDescription"] = True
        if title is not None:
            fields.update(fromTitle=node["title"], toTitle=title)
            node["title"] = title
        return self.record(key, at, actor=actor, **fields)

    def label(self, key, at, *, add=(), remove=(), actor=ROLANDO, **fields):
        node = self.find(key)
        names = [x["name"] for x in node["labels"]["nodes"]]
        names = [n for n in names if n not in remove] + [n for n in add if n not in names]
        node["labels"]["nodes"] = [{"name": n} for n in names]
        return self.record(
            key,
            at,
            actor=actor,
            addedLabels=[{"name": n} for n in add] or None,
            removedLabels=[{"name": n} for n in remove] or None,
            **fields,
        )


def policy(**rule):
    return IntakePolicy(
        APPROVER, {PROJECT: ProjectRule(PROJECT, status_issue_id=STATUS_ID, **rule)}, SINCE
    )


def ticket(node):
    return parse_ticket(node, node["history"]["nodes"])


class LinearCase(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.world = FakeLinear()
        self.policy = policy()
        self.source = LinearSource(self.world, lambda: self.policy, now=lambda: self.now)

    def judge(self, key="ENG-187"):
        return judge(ticket(self.world.find(key)), self.policy, self.now)

    def rolando_moves(self, key="ENG-187", ago=timedelta(minutes=5), **add):
        """A ticket in Backlog that Rolando moves to Todo ``ago`` before now."""
        if self.world.find(key) is None:
            self.world.add(key, **add)
        return self.world.move(key, TODO, self.now - ago)


class JudgeTests(LinearCase):
    # --- Rolando's own move ---

    def test_rolandos_own_move_is_an_authorization(self):
        eid = self.rolando_moves()
        a = self.judge()
        self.assertIsInstance(a, Authorization)
        t = ticket(self.world.find("ENG-187"))
        self.assertEqual(a.event_id, eid)
        self.assertEqual(a.revision, revision(t))
        self.assertEqual(a.project_id, PROJECT)
        self.assertEqual(a.issue_id, "iss-eng-187")
        self.assertEqual(a.issue_key, "ENG-187")
        self.assertEqual(a.moved_at, NOW - timedelta(minutes=5))
        self.assertIn(APPROVER, a.actor)
        self.assertIn(eid, a.evidence)

    def test_revision_pins_the_text(self):
        self.world.add("ENG-187")
        base = revision(ticket(self.world.find("ENG-187")))
        changes = {
            "title": lambda n: n.update(title="Other"),
            "description": lambda n: n.update(description="Other"),
            "labels": lambda n: n["labels"]["nodes"].append({"name": "x"}),
            "project": lambda n: n.update(project={"id": OTHER_PROJECT}),
            "team": lambda n: n.update(team={"id": "team-other"}),
            "parent": lambda n: n.update(parent={"id": "iss-eng-1"}),
        }
        for name, change in changes.items():
            with self.subTest(name):
                node = copy.deepcopy(self.world.find("ENG-187"))
                change(node)
                self.assertNotEqual(revision(ticket(node)), base)
        # Label order and the state are not part of the text.
        node = copy.deepcopy(self.world.find("ENG-187"))
        node["labels"]["nodes"] = [{"name": "b"}, {"name": "a"}]
        other = copy.deepcopy(node)
        other["labels"]["nodes"].reverse()
        other["state"] = TODO
        self.assertEqual(revision(ticket(node)), revision(ticket(other)))

    # --- moves that don't count ---

    def test_moves_by_anyone_or_anything_else_are_refused(self):
        cases = {
            "another user": (dict(actor=MARIA), "made by Maria"),
            "an app user": (dict(actor=ZAPIER), "app account"),
            "an app acting for Rolando": (
                dict(actor=ROLANDO, botActor=CLAUDE_BOT),
                "integration or app (oauthClient: Claude)",
            ),
            "the GitHub sync": (dict(actor=None, botActor=GITHUB_BOT), "integration or app"),
            "a workflow": (
                dict(workflowMetadata={"__typename": "IssueHistoryWorkflowMetadata"}),
                "automation",
            ),
            "a triage rule": (dict(triageResponsibilityAutoAssigned=True), "automation"),
            "an import": (dict(issueImport={"id": "imp-1"}), "import"),
            "nobody recorded": (dict(actor=None), "does not record who"),
        }
        for i, (name, (fields, why)) in enumerate(cases.items()):
            with self.subTest(name):
                key = f"ENG-{300 + i}"
                self.world.add(key)
                eid = self.world.move(key, TODO, NOW - timedelta(minutes=5), **fields)
                r = self.judge(key)
                self.assertIsInstance(r, Refusal)
                self.assertEqual(r.event_id, eid)
                self.assertEqual(r.issue_key, key)
                self.assertIn(why, r.reason)

    def test_created_straight_into_todo_is_refused_once(self):
        node = self.world.add("ENG-187", state=TODO)
        r = self.judge()
        self.assertIsInstance(r, Refusal)
        self.assertEqual(r.event_id, f"created-{node['id']}")
        self.assertIn("Backlog and back", r.reason)
        # The same refusal (same id) on every poll, so the service says it once.
        first = self.source.poll(None)
        again = self.source.poll(first.cursor)
        self.assertEqual([x.event_id for x in first.refusals], [r.event_id])
        self.assertEqual([x.event_id for x in again.refusals], [r.event_id])

    def test_moving_to_another_teams_todo_is_not_a_move_into_todo(self):
        self.world.add("ENG-187", state=TODO)
        other_todo = {"id": "state-todo-other-team", "name": "Todo", "type": "unstarted"}
        self.world.move("ENG-187", other_todo, self.now - timedelta(minutes=5))
        self.assertNotIsInstance(self.judge(), Authorization)

    def test_imported_into_todo_is_refused(self):
        self.world.add("ENG-187", state=TODO)
        self.world.record(
            "ENG-187",
            SINCE + timedelta(minutes=10),
            actor=None,
            issueImport={"id": "imp-1"},
            toState=TODO,
        )
        r = self.judge()
        self.assertIsInstance(r, Refusal)
        self.assertEqual(r.event_id, "created-iss-eng-187")

    def test_in_todo_before_onboarding_says_nothing(self):
        self.world.add("ENG-187", state=TODO, created=SINCE - timedelta(days=2))
        self.assertIsNone(self.judge())
        self.world.add("ENG-188", created=SINCE - timedelta(days=2))
        self.world.move("ENG-188", TODO, SINCE - timedelta(days=1))
        self.assertIsNone(self.judge("ENG-188"))

    def test_moves_before_intake_since_are_ignored(self):
        self.world.add("ENG-187", created=SINCE - timedelta(days=1))
        self.world.move("ENG-187", TODO, SINCE - timedelta(minutes=1))
        self.assertIsNone(self.judge())
        self.assertEqual(self.source.poll(None).authorizations, [])
        # A bot's move before onboarding isn't refused either: it's never read.
        self.world.add("ENG-188", created=SINCE - timedelta(days=1))
        self.world.move("ENG-188", TODO, SINCE - timedelta(minutes=1), botActor=CLAUDE_BOT)
        self.assertIsNone(self.judge("ENG-188"))

    # --- the settle window ---

    def test_a_move_younger_than_settle_waits(self):
        self.rolando_moves(ago=SETTLE - timedelta(seconds=1))
        self.assertIsNone(self.judge())
        self.now += timedelta(seconds=1)
        self.assertIsInstance(self.judge(), Authorization)

    def test_refusals_do_not_wait_for_settle(self):
        self.world.add("ENG-187")
        self.world.move("ENG-187", TODO, NOW - timedelta(seconds=5), botActor=CLAUDE_BOT)
        self.assertIsInstance(self.judge(), Refusal)

    # --- edits after the move ---

    def test_edit_after_the_move_is_refused_naming_it(self):
        edits = {
            "description": dict(updatedDescription=True),
            "title": dict(fromTitle="Old", toTitle="New"),
            "labels": dict(addedLabels=[{"name": "ux"}]),
            "project": dict(fromProjectId=OTHER_PROJECT, toProjectId=PROJECT),
            "team": dict(fromTeamId="team-other", toTeamId=TEAM),
            "parent": dict(fromParentId=None, toParentId="iss-eng-1"),
        }
        for i, (part, fields) in enumerate(edits.items()):
            with self.subTest(part):
                key = f"ENG-{400 + i}"
                eid = self.rolando_moves(key, ago=timedelta(minutes=10))
                self.world.record(key, NOW - timedelta(minutes=5), **fields)
                r = self.judge(key)
                self.assertIsInstance(r, Refusal)
                self.assertEqual(r.event_id, eid)
                self.assertIn(part, r.reason)
                self.assertIn("out of Todo and back", r.reason)

    def test_edit_by_someone_else_after_the_move_is_refused_too(self):
        self.rolando_moves(ago=timedelta(minutes=10))
        self.world.edit("ENG-187", NOW - timedelta(minutes=5), actor=MARIA, description="x")
        self.assertIsInstance(self.judge(), Refusal)

    def test_edit_before_the_move_is_fine(self):
        self.world.add("ENG-187")
        self.world.edit("ENG-187", NOW - timedelta(minutes=20), description="Better", title="T")
        self.world.label("ENG-187", NOW - timedelta(minutes=15), add=["ux"])
        self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        a = self.judge()
        self.assertIsInstance(a, Authorization)
        self.assertEqual(a.revision, revision(ticket(self.world.find("ENG-187"))))

    def test_non_content_changes_after_the_move_are_fine(self):
        self.rolando_moves(ago=timedelta(minutes=10))
        # An assignee or priority change: none of the fields that define the work.
        self.world.record("ENG-187", NOW - timedelta(minutes=5))
        self.assertIsInstance(self.judge(), Authorization)

    # --- leaving Todo ---

    def test_moved_out_of_todo_again_says_nothing(self):
        for i, where in enumerate((BACKLOG, IN_PROGRESS, DONE, CANCELED)):
            with self.subTest(where["name"]):
                key = f"ENG-{500 + i}"
                self.rolando_moves(key, ago=timedelta(minutes=10))
                self.world.move(key, where, NOW - timedelta(minutes=5))
                self.assertIsNone(self.judge(key))

    def test_out_and_back_in_gives_the_newer_move(self):
        first = self.rolando_moves(ago=timedelta(minutes=20))
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15))
        second = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        a = self.judge()
        self.assertIsInstance(a, Authorization)
        self.assertEqual(a.event_id, second)
        self.assertNotEqual(first, second)
        self.assertEqual(a.moved_at, NOW - timedelta(minutes=10))

    def test_bot_move_then_rolandos_own_move_counts(self):
        self.world.add("ENG-187")
        self.world.move("ENG-187", TODO, NOW - timedelta(minutes=20), botActor=CLAUDE_BOT)
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15))
        mine = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        self.assertEqual(self.judge().event_id, mine)

    def test_rolandos_move_then_bot_out_and_in_is_refused(self):
        self.rolando_moves(ago=timedelta(minutes=20))
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15), botActor=CLAUDE_BOT)
        bot = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10), botActor=CLAUDE_BOT)
        r = self.judge()
        self.assertIsInstance(r, Refusal)
        self.assertEqual(r.event_id, bot)

    # --- onboarding rule ---

    def test_ticket_not_in_the_issues_list_is_refused(self):
        self.policy = policy(issues=frozenset({"ENG-188"}))
        eid = self.rolando_moves("ENG-186")
        r = self.judge("ENG-186")
        self.assertIsInstance(r, Refusal)
        self.assertEqual(r.event_id, eid)
        self.assertIn("baseline", r.reason)
        self.rolando_moves("ENG-188")
        self.assertIsInstance(self.judge("ENG-188"), Authorization)

    def test_ticket_with_a_skip_label_is_refused(self):
        self.policy = policy(skip_labels=frozenset({"baseline"}))
        self.rolando_moves(labels=("baseline", "ux"))
        r = self.judge()
        self.assertIsInstance(r, Refusal)
        self.assertIn("'baseline'", r.reason)

    def test_other_projects_and_deleted_tickets_say_nothing(self):
        self.rolando_moves(project=OTHER_PROJECT)
        self.assertIsNone(self.judge())
        self.rolando_moves("ENG-188", trashed=True)
        self.assertIsNone(self.judge("ENG-188"))
        self.rolando_moves("ENG-189", archived_at=iso(NOW))
        self.assertIsNone(self.judge("ENG-189"))

    # --- replays and order ---

    def test_shuffled_history_gives_the_same_verdict(self):
        self.world.add("ENG-187")
        self.world.edit("ENG-187", NOW - timedelta(minutes=40), description="v2")
        self.world.move("ENG-187", TODO, NOW - timedelta(minutes=30), botActor=CLAUDE_BOT)
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=25))
        self.world.move("ENG-187", TODO, NOW - timedelta(minutes=20))
        self.world.record("ENG-187", NOW - timedelta(minutes=15))
        node = self.world.find("ENG-187")
        expected = self.judge()
        self.assertIsInstance(expected, Authorization)
        rng = random.Random(174)
        for _ in range(10):
            shuffled = copy.deepcopy(node)
            rng.shuffle(shuffled["history"]["nodes"])
            self.assertEqual(judge(ticket(shuffled), self.policy, NOW), expected)
        # And a refusal stays the same refusal.
        self.world.edit("ENG-187", NOW - timedelta(minutes=5), title="v3")
        expected = self.judge()
        self.assertIsInstance(expected, Refusal)
        for _ in range(10):
            shuffled = copy.deepcopy(self.world.find("ENG-187"))
            rng.shuffle(shuffled["history"]["nodes"])
            self.assertEqual(judge(ticket(shuffled), self.policy, NOW), expected)


class StatusTicketTests(LinearCase):
    def setUp(self):
        super().setUp()
        self.world.add(STATUS_KEY, created=SINCE - timedelta(days=1))

    def controls(self):
        return self.source.poll(None).controls

    def test_status_ticket_moved_to_todo_is_never_an_authorization(self):
        self.world.move(STATUS_KEY, TODO, NOW - timedelta(minutes=10))
        self.assertIsNone(self.judge(STATUS_KEY))
        batch = self.source.poll(None)
        self.assertEqual(batch.authorizations, [])
        self.assertEqual(batch.refusals, [])

    def test_pause_and_resume_by_rolando(self):
        pause = self.world.label(STATUS_KEY, NOW - timedelta(minutes=10), add=["factory-pause"])
        resume = self.world.label(STATUS_KEY, NOW - timedelta(minutes=5), remove=["factory-pause"])
        found = self.controls()
        self.assertEqual(
            [(c.event_id, c.action) for c in found], [(pause, "pause"), (resume, "resume")]
        )
        self.assertTrue(all(c.issue_id == STATUS_ID for c in found))
        # Fetched by id, not through the issues filter.
        self.assertIn({"id": STATUS_ID}, self.world.variables("issue"))

    def test_pause_by_anyone_or_anything_else_is_not_a_control(self):
        cases = {
            "a bot": dict(botActor=CLAUDE_BOT),
            "another user": dict(actor=MARIA),
            "an automation": dict(workflowMetadata={"__typename": "X"}),
            "nobody": dict(actor=None),
        }
        for name, fields in cases.items():
            with self.subTest(name):
                self.world.find(STATUS_KEY)["history"]["nodes"].clear()
                self.world.label(
                    STATUS_KEY, NOW - timedelta(minutes=5), add=["factory-pause"], **fields
                )
                self.assertEqual(self.controls(), [])

    def test_pause_before_onboarding_is_not_a_control(self):
        self.world.label(STATUS_KEY, SINCE - timedelta(minutes=5), add=["factory-pause"])
        self.assertEqual(self.controls(), [])

    def test_other_labels_are_not_controls(self):
        self.world.label(STATUS_KEY, NOW - timedelta(minutes=5), add=["urgent"])
        self.assertEqual(self.controls(), [])


class PollTests(LinearCase):
    def test_poll_reads_only_onboarded_projects_since_onboarding(self):
        self.rolando_moves()
        self.source.poll(None)
        (v,) = self.world.variables("issues")
        self.assertEqual(v["projects"], [PROJECT])
        self.assertEqual(linear._when(v["since"]), SINCE)
        self.assertIsNone(v["after"])

    def test_wrong_project_is_never_seen(self):
        self.rolando_moves(project=OTHER_PROJECT)
        batch = self.source.poll(None)
        self.assertEqual((batch.authorizations, batch.refusals), ([], []))

    def test_replayed_and_overlapping_polls_give_the_same_event_ids(self):
        eid = self.rolando_moves()
        first = self.source.poll(None)
        self.assertEqual([a.event_id for a in first.authorizations], [eid])
        for cursor in (None, first.cursor, iso(NOW - timedelta(hours=1))):
            with self.subTest(cursor=cursor):
                again = self.source.poll(cursor)
                self.assertEqual(again.authorizations, first.authorizations)

    def test_cursor_overlaps_the_previous_poll(self):
        self.rolando_moves()
        cursor = self.source.poll(None).cursor
        self.source.poll(cursor)
        since = linear._when(self.world.variables("issues")[-1]["since"])
        self.assertEqual(since, linear._when(cursor) - linear.OVERLAP)

    def test_settling_move_holds_the_cursor_and_is_read_again(self):
        held = self.rolando_moves(ago=timedelta(seconds=30))
        # Another ticket changed later; the cursor must not pass the settling one.
        self.world.add("ENG-188")
        self.world.edit("ENG-188", NOW - timedelta(seconds=10), description="x")
        batch = self.source.poll(None)
        self.assertEqual(batch.authorizations, [])
        moved = linear._when(self.world.find("ENG-187")["updatedAt"])
        self.assertLessEqual(datetime.fromisoformat(batch.cursor), moved)
        self.now += SETTLE
        later = self.source.poll(batch.cursor)
        self.assertEqual([a.event_id for a in later.authorizations], [held])
        self.assertGreater(datetime.fromisoformat(later.cursor), moved)

    def test_settled_poll_advances_the_cursor(self):
        self.rolando_moves()
        batch = self.source.poll(None)
        self.assertEqual(
            datetime.fromisoformat(batch.cursor),
            linear._when(self.world.find("ENG-187")["updatedAt"]),
        )

    def test_history_is_read_page_by_page(self):
        node = self.world.add("ENG-187", has_next=True, end_cursor="h1")
        node["history"]["nodes"] = [entry("hist-a", NOW - timedelta(minutes=30))]
        self.world.history_pages[(node["id"], "h1")] = (
            [entry("hist-b", NOW - timedelta(minutes=20), fromState=BACKLOG, toState=IN_PROGRESS)],
            {"hasNextPage": True, "endCursor": "h2"},
        )
        self.world.history_pages[(node["id"], "h2")] = (
            [entry("hist-c", NOW - timedelta(minutes=10), fromState=IN_PROGRESS, toState=TODO)],
            {"hasNextPage": False, "endCursor": None},
        )
        node["state"] = TODO
        node["updatedAt"] = iso(NOW - timedelta(minutes=10))
        batch = self.source.poll(None)
        self.assertEqual([a.event_id for a in batch.authorizations], ["hist-c"])
        self.assertEqual([v["after"] for v in self.world.variables("history")], ["h1", "h2"])
        self.assertTrue(all(v["id"] == node["id"] for v in self.world.variables("history")))

    def test_an_edit_on_a_later_history_page_is_seen(self):
        node = self.world.add("ENG-187", has_next=True, end_cursor="h1")
        eid = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=20))
        self.world.history_pages[(node["id"], "h1")] = (
            [entry("hist-z", NOW - timedelta(minutes=5), updatedDescription=True)],
            {"hasNextPage": False, "endCursor": None},
        )
        node["updatedAt"] = iso(NOW - timedelta(minutes=5))
        batch = self.source.poll(None)
        self.assertEqual(batch.authorizations, [])
        self.assertEqual([r.event_id for r in batch.refusals], [eid])

    def test_issues_are_read_page_by_page(self):
        self.world.page_size = 2
        ids = [
            self.rolando_moves(f"ENG-{n}", ago=timedelta(minutes=n - 180))
            for n in (187, 188, 189, 191, 194)
        ]
        batch = self.source.poll(None)
        self.assertEqual(sorted(a.event_id for a in batch.authorizations), sorted(ids))
        self.assertEqual([v["after"] for v in self.world.variables("issues")], [None, "2", "4"])
        # Oldest move first.
        moved = [a.moved_at for a in batch.authorizations]
        self.assertEqual(moved, sorted(moved))

    def test_unreachable_linear_raises_and_reads_nothing_more(self):
        self.rolando_moves()
        self.source.check_identity()
        self.world.fail = LinearUnavailable("Linear answered HTTP 503")
        with self.assertRaises(LinearUnavailable):
            self.source.poll(None)


class IdentityTests(LinearCase):
    def test_factory_key_acting_as_rolando_blocks_everything(self):
        self.world.viewer["id"] = APPROVER
        self.rolando_moves()
        with self.assertRaises(IntakeBlocked):
            self.source.poll(None)
        a = Authorization("hist-1", "iss-eng-187", "ENG-187", PROJECT, "R", NOW, "0" * 64, "e")
        with self.assertRaises(IntakeBlocked):
            self.source.standing(a)
        self.assertEqual(set(self.world.names()), {"viewer"})

    def test_own_identity_is_checked_once(self):
        self.rolando_moves()
        self.source.poll(None)
        self.source.poll(None)
        self.source.fetch("iss-eng-187")
        self.assertEqual(self.world.names().count("viewer"), 1)
        self.assertEqual(self.world.names()[0], "viewer")

    def test_unknown_viewer_is_unavailable(self):
        self.world.viewer = {}
        with self.assertRaises(LinearUnavailable):
            self.source.poll(None)
        self.assertEqual(self.world.names(), ["viewer"])


class StandingTests(LinearCase):
    def setUp(self):
        super().setUp()
        self.rolando_moves(ago=timedelta(minutes=10))
        self.a = self.judge()
        self.assertIsInstance(self.a, Authorization)

    def now_standing(self):
        return self.source.standing(self.a)

    def test_still_in_todo_stands(self):
        s = self.now_standing()
        self.assertIsNone(s.reason)
        self.assertEqual(tuple(s.waiting_on), ())
        self.assertIsNone(self.source.revalidate(self.a))
        self.assertIn({"id": "iss-eng-187"}, self.world.variables("issue"))

    def test_started_states_stand(self):
        for where in (IN_PROGRESS, IN_REVIEW):
            with self.subTest(where["name"]):
                self.world.move("ENG-187", where, NOW - timedelta(minutes=1), botActor=GITHUB_BOT)
                self.assertIsNone(self.now_standing().reason)

    def test_moved_out_is_withdrawn(self):
        for where in (BACKLOG, DONE, CANCELED):
            with self.subTest(where["name"]):
                self.world.move("ENG-187", where, NOW - timedelta(minutes=1))
                s = self.now_standing()
                self.assertIn(where["name"], s.withdrawn or "")
                self.assertEqual(self.source.revalidate(self.a), s.withdrawn)

    def test_other_unstarted_state_is_withdrawn(self):
        self.world.move(
            "ENG-187",
            {"id": "st-triage-ish", "name": "Ready", "type": "unstarted"},
            NOW - timedelta(minutes=1),
        )
        self.assertIsNotNone(self.now_standing().withdrawn)

    def test_deleted_or_archived_is_withdrawn(self):
        cases = {
            "gone": lambda n: self.world.nodes.pop(n["id"]),
            "trashed": lambda n: n.update(trashed=True),
            "archived": lambda n: n.update(archivedAt=iso(NOW)),
        }
        for name, change in cases.items():
            with self.subTest(name):
                saved = copy.deepcopy(self.world.find("ENG-187"))
                change(self.world.find("ENG-187"))
                self.assertIsNotNone(self.now_standing().withdrawn)
                self.world.nodes[saved["id"]] = saved

    def test_project_changed_is_withdrawn(self):
        node = self.world.find("ENG-187")
        node["project"] = {"id": OTHER_PROJECT}
        self.world.record(
            "ENG-187",
            NOW - timedelta(minutes=1),
            fromProjectId=PROJECT,
            toProjectId=OTHER_PROJECT,
        )
        self.assertIn("project", self.now_standing().withdrawn or "")

    def test_project_removed_from_onboarding_is_withdrawn(self):
        self.policy = IntakePolicy(APPROVER, {}, SINCE)
        self.assertIsNotNone(self.now_standing().withdrawn)

    def test_text_edited_after_the_move_is_changed(self):
        self.world.edit("ENG-187", NOW - timedelta(minutes=1), description="New")
        s = self.now_standing()
        self.assertIsNone(s.withdrawn)
        self.assertIn("description", s.changed or "")
        self.assertIsNotNone(s.reason)

    def test_edit_linear_did_not_record_is_still_changed(self):
        self.world.find("ENG-187")["title"] = "Quietly different"
        s = self.now_standing()
        self.assertIn("its text", s.changed or "")

    def test_open_blocker_waits_and_done_blocker_does_not(self):
        node = self.world.find("ENG-187")
        cases = {
            "started": ("ENG-190",),
            "unstarted": ("ENG-190",),
            "backlog": ("ENG-190",),
            "completed": (),
            "canceled": (),
        }
        for kind, waiting in cases.items():
            with self.subTest(kind):
                node["inverseRelations"]["nodes"] = [
                    {"type": "blocks", "issue": {"identifier": "ENG-190", "state": {"type": kind}}}
                ]
                s = self.now_standing()
                self.assertEqual(tuple(s.waiting_on), waiting)
                self.assertIsNone(s.reason)

    def test_related_or_duplicate_tickets_do_not_block(self):
        self.world.find("ENG-187")["inverseRelations"]["nodes"] = [
            {"type": "related", "issue": {"identifier": "ENG-190", "state": {"type": "started"}}},
            {"type": "duplicate", "issue": {"identifier": "ENG-191", "state": {"type": "started"}}},
        ]
        self.assertEqual(tuple(self.now_standing().waiting_on), ())

    def test_pure_standing_of_a_missing_ticket(self):
        self.assertIsNotNone(standing(None, self.a, self.policy).withdrawn)


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


API_KEY = "lin_api_" + "Q7w8E9r0T1y2U3i4" * 3
OAUTH_TOKEN = "lin_oauth_" + "Z9x8C7v6B5n4M3a2" * 3


class HttpTransportTests(unittest.TestCase):
    def transport(self, answer, key=API_KEY):
        self.requests = []

        def opener(req, timeout):
            self.requests.append((req, timeout))
            if isinstance(answer, BaseException):
                raise answer
            return answer if hasattr(answer, "read") else _Resp(answer)

        return HttpTransport(lambda: key, opener=opener)

    def ok(self, data):
        return json.dumps({"data": data}).encode()

    def test_posts_the_query_to_linear_only(self):
        out = self.transport(self.ok({"viewer": {"id": "u"}}))(VIEWER_QUERY, {"a": 1})
        self.assertEqual(out, {"viewer": {"id": "u"}})
        ((req, timeout),) = self.requests
        self.assertEqual(req.full_url, linear.API)
        self.assertEqual(linear.API, "https://api.linear.app/graphql")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"query": VIEWER_QUERY, "variables": {"a": 1}})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertIsNotNone(timeout)

    def test_personal_api_key_is_sent_raw(self):
        t = self.transport(self.ok({}), key=API_KEY)
        t(VIEWER_QUERY, {})
        self.assertEqual(self.requests[0][0].get_header("Authorization"), API_KEY)

    def test_oauth_token_is_sent_as_bearer(self):
        t = self.transport(self.ok({}), key=OAUTH_TOKEN)
        t(VIEWER_QUERY, {})
        self.assertEqual(self.requests[0][0].get_header("Authorization"), f"Bearer {OAUTH_TOKEN}")

    def test_key_is_read_each_call(self):
        keys = iter([API_KEY, OAUTH_TOKEN])
        self.requests = []

        def opener(req, timeout):
            self.requests.append(req)
            return _Resp(self.ok({}))

        t = HttpTransport(lambda: next(keys), opener=opener)
        t(VIEWER_QUERY, {})
        t(VIEWER_QUERY, {})
        self.assertEqual(
            [r.get_header("Authorization") for r in self.requests],
            [API_KEY, f"Bearer {OAUTH_TOKEN}"],
        )

    def unavailable(self, answer, key=API_KEY):
        with self.assertRaises(LinearUnavailable) as cm:
            self.transport(answer, key)(VIEWER_QUERY, {})
        text = str(cm.exception)
        self.assertNotIn(key, text)
        self.assertNotIn(key, repr(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        return text

    def test_failures_are_unavailable_and_never_show_the_key(self):
        body = f'{{"errors": [{{"message": "bad key {API_KEY}"}}]}}'.encode()
        cases = {
            "http 401": urllib.error.HTTPError(linear.API, 401, "no", {}, BytesIO(body)),
            "http 500": urllib.error.HTTPError(linear.API, 500, "err", {}, BytesIO(b"")),
            "redirect": urllib.error.HTTPError(linear.API, 302, "moved", {}, BytesIO(b"")),
            "network": urllib.error.URLError("down"),
            "timeout": TimeoutError("timed out"),
            "bad json": b"<html>nope</html>",
            "not utf-8": b"\xff\xfe",
            "not an object": b"[1, 2]",
            "no data": b"{}",
            "graphql errors": json.dumps(
                {"data": None, "errors": [{"message": "Authentication required"}]}
            ).encode(),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.unavailable(answer)

    def test_http_error_names_the_status(self):
        err = urllib.error.HTTPError(linear.API, 429, "slow", {}, BytesIO(b""))
        self.assertIn("429", self.unavailable(err))

    def test_graphql_error_names_the_first_message(self):
        body = json.dumps({"errors": [{"message": "Rate limited"}, {"message": "x"}]}).encode()
        self.assertIn("Rate limited", self.unavailable(body))

    def test_graphql_error_echoing_the_key_does_not_show_it(self):
        # Was a bug (fixed): HttpTransport.__call__ (controller/intake/linear.py:504-506) copies
        # Linear's first GraphQL error message into LinearUnavailable verbatim, so
        # an error text that echoes the credential puts it in the exception (and
        # the probe CLI's traceback). The docstring says "the key never appears in
        # errors"; the GitHub transport redacts its token (github_http._message).
        body = json.dumps({"errors": [{"message": f"Invalid token {OAUTH_TOKEN}"}]}).encode()
        self.unavailable(body, key=OAUTH_TOKEN)

    def test_cut_off_response_is_unavailable(self):
        # Was a bug (fixed): HttpTransport.__call__ (controller/intake/linear.py:497-501) catches
        # only HTTPError, OSError and ValueError. A body cut off mid-read raises
        # http.client.IncompleteRead (an HTTPException, not an OSError), which
        # escapes instead of LinearUnavailable, contrary to the Transport contract
        # ("Raises LinearUnavailable"). github_http.py catches HTTPException.
        self.unavailable(_ResponseCutOff())

    def test_redirects_are_not_followed(self):
        handler = linear._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "x", {}, "https://evil/"))


class _ResponseCutOff:
    """An answer whose body is cut off mid-read."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        raise http.client.IncompleteRead(b'{"data":', 100)


class PolicyFromTests(unittest.TestCase):
    def onboarding(self, **kw):
        from controller.service import onboarding as ob

        project = ob.Project(
            PROJECT,
            "Pilot",
            "o/r",
            "main",
            "trig_1",
            frozenset({"modify-files"}),
            frozenset({"npm test"}),
            3,
            STATUS_ID,
            frozenset({"ENG-187"}),
            frozenset({"baseline"}),
        )
        fields = dict(approver_linear_user_id=APPROVER, intake_since=SINCE)
        fields.update(kw)
        return ob.Onboarding({PROJECT: project}, "0" * 64, True, **fields)

    def test_rules_come_from_the_onboarding_file(self):
        p = policy_from(self.onboarding())
        self.assertEqual(p.approver_id, APPROVER)
        self.assertEqual(p.since, SINCE)
        rule = p.projects[PROJECT]
        self.assertEqual(rule.issues, frozenset({"ENG-187"}))
        self.assertEqual(rule.skip_labels, frozenset({"baseline"}))
        self.assertEqual(p.status_issues(), {STATUS_ID})
        self.assertEqual(p.todo, "Todo")
        self.assertEqual(p.settle, SETTLE)

    def test_missing_approver_or_since_blocks_intake(self):
        for name, kw in {
            "no approver": dict(approver_linear_user_id=None),
            "empty approver": dict(approver_linear_user_id=""),
            "no since": dict(intake_since=None),
        }.items():
            with self.subTest(name), self.assertRaises(IntakeBlocked):
                policy_from(self.onboarding(**kw))

    def test_source_with_blocked_policy_reads_nothing(self):
        world = FakeLinear()
        bad = self.onboarding(intake_since=None)
        source = LinearSource(world, lambda: policy_from(bad), now=lambda: NOW)
        with self.assertRaises(IntakeBlocked):
            source.poll(None)
        self.assertNotIn("issues", world.names())


if __name__ == "__main__":
    unittest.main()
