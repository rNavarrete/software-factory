"""The service with real Todo intake (ENG-174): ``LinearSource`` over a fake Linear.

Same harness as tests/test_service.py (``ServiceCase``), with the fixture
source swapped for ``LinearSource`` reading ``FakeLinear`` from
tests/test_intake_linear.py, and its policy read from the onboarding config
each round. Contracts are still approved by Rolando's typed approval
(``rolando_approves``): the Todo move as the approval itself is not built yet.
"""

import unittest
from datetime import timedelta

from controller.approval import Approvals
from controller.intake import LinearSource, LinearUnavailable, policy_from
from controller.intake.linear import SETTLE
from controller.interfaces import AttemptId
from controller.recovery import Recovery, State
from controller.service.fixtures import FixturePreparer
from controller.service.seams import Integrations
from tests.test_approval import yes
from tests.test_intake_linear import (
    BACKLOG,
    CLAUDE_BOT,
    DONE,
    GITHUB_BOT,
    IN_PROGRESS,
    MARIA,
    TODO,
    FakeLinear,
)
from tests.test_recovery import BOT, SHA_A
from tests.test_service import KEY, NOW, PROJECT, ServiceCase, config, contract_for

APPROVER = "user-rolando"
SINCE = NOW - timedelta(hours=3)
STATUS_KEY = "ENG-100"
STATUS_ID = "iss-eng-100"
FACTORY_TICKETS = ["ENG-187", "ENG-188", "ENG-189", "ENG-191"]


class Advise:
    """A repair advisor that always suggests one, to show when none is asked for."""

    def __init__(self):
        self.calls = []

    def advise(self, attempt, detail):
        self.calls.append(attempt)
        return "the tests failed"


class IntakeServiceCase(ServiceCase):
    def setUp(self):
        super().setUp()
        self.linear = FakeLinear()
        self.linear.add(STATUS_KEY, created=SINCE - timedelta(days=1))
        self.config = config(
            approver_linear_user_id=APPROVER,
            intake_since=SINCE.isoformat(),
            entry={
                "status_issue_id": STATUS_ID,
                "issues": FACTORY_TICKETS,
                "skip_labels": ["baseline"],
            },
        )
        self.preparer = FixturePreparer({k: contract_for(k) for k in FACTORY_TICKETS})
        self.restart()

    # --- helpers ---

    def restart(self):
        """A new service process: new source (identity checked afresh), same store."""
        self.source = LinearSource(
            self.linear, lambda: policy_from(self.load_config()), now=self.clock
        )
        self.build()

    def rolando_moves(self, key="ENG-187", ago=timedelta(minutes=5), **kw):
        if self.linear.find(key) is None:
            self.linear.add(key, created=SINCE + timedelta(minutes=1), **kw)
        return self.linear.move(key, TODO, self.now - ago)

    def item(self, event_id):
        return self.view().items[event_id]

    def texts(self, key="ENG-187"):
        return self.reporter.texts(f"iss-{key.lower()}")

    def launched(self):
        return len(self.adapter.requests)

    def recovery_close(self, attempt, state=State.FAILED):
        rec = Recovery(
            self.store,
            Approvals(self.store, KEY, confirm=yes, os_user="rolando"),
            self.github,
            worker_logins=frozenset({BOT}),
            gate=self.gate,
            confirm=yes,
        )
        rec.close_attempt(attempt, state, "tests failed", self.now)


class IntakeServiceTests(IntakeServiceCase):
    # --- the happy path ---

    def test_rolandos_move_is_queued_and_fires_once(self):
        eid = self.rolando_moves()
        self.rolando_approves("ENG-187")
        r = self.tick()
        self.assertEqual(r.errors, [])
        self.assertEqual(r.accepted, ["ENG-187"])
        self.assertEqual(r.fired, ["eng-187-a1-f1"])
        item = self.item(eid)
        self.assertEqual(item.issue_id, "iss-eng-187")
        self.assertEqual(item.project_id, PROJECT)
        self.assertTrue(any("queued" in t for t in self.texts()))

    def test_waits_for_settle_before_accepting(self):
        eid = self.rolando_moves(ago=timedelta(seconds=20))
        self.rolando_approves("ENG-187")
        r = self.tick()
        self.assertEqual((r.accepted, r.fired, r.errors), ([], [], []))
        self.tick(minutes=1)
        self.assertNotIn(eid, self.view().items)
        r = self.tick(minutes=1)
        self.assertEqual(r.accepted, ["ENG-187"])
        self.assertEqual(self.launched(), 1)

    # --- replays and overlap ---

    def test_replayed_and_overlapping_polls_queue_one_item_and_fire_once(self):
        eid = self.rolando_moves()
        self.rolando_approves("ENG-187")
        for i in range(6):
            self.tick(minutes=1 if i else 0)
        # A poll from the very start replays every move again.
        poll = self.source.poll
        self.source.poll = lambda cursor: poll(None)
        for _ in range(3):
            self.tick(minutes=6)
        self.assertEqual(list(self.view().items), [eid])
        self.assertEqual(self.launched(), 1)
        self.assertEqual(len(self.fires()), 1)
        # Every poll but the first reached back before its cursor.
        cursors = [v["since"] for v in self.linear.variables("issues")]
        self.assertGreater(len(cursors), 6)

    # --- refusals ---

    def test_move_by_a_bot_or_another_user_is_refused_with_a_message(self):
        self.linear.add("ENG-187", created=SINCE + timedelta(minutes=1))
        bot = self.linear.move("ENG-187", TODO, NOW - timedelta(minutes=5), botActor=CLAUDE_BOT)
        self.linear.add("ENG-188", created=SINCE + timedelta(minutes=1))
        other = self.linear.move("ENG-188", TODO, NOW - timedelta(minutes=5), actor=MARIA)
        self.rolando_approves("ENG-187")
        self.rolando_approves("ENG-188")
        r = self.tick()
        self.assertEqual(sorted(r.refused), ["ENG-187", "ENG-188"])
        self.assertEqual(self.view().items, {})
        self.assertLessEqual({bot, other}, self.view().seen)
        self.assertTrue(any("integration or app" in t for t in self.texts("ENG-187")))
        self.assertTrue(any("Maria" in t for t in self.texts("ENG-188")))
        for _ in range(3):
            self.tick(minutes=6)
        self.assertEqual(self.launched(), 0)
        # Said once each, however often it is read.
        self.assertEqual(len(self.texts("ENG-187")), 1)
        self.assertEqual(len(self.texts("ENG-188")), 1)

    def test_ticket_created_in_todo_is_refused_once(self):
        self.linear.add("ENG-187", state=TODO, created=NOW - timedelta(minutes=10))
        self.rolando_approves("ENG-187")
        for i in range(3):
            self.tick(minutes=6 if i else 0)
        self.assertEqual(self.view().items, {})
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("Backlog and back", self.texts()[0])
        self.assertEqual(self.launched(), 0)

    def test_wrong_project_is_never_seen(self):
        self.rolando_moves(project="proj-other")
        self.rolando_approves("ENG-187")
        for i in range(3):
            self.tick(minutes=6 if i else 0)
        self.assertEqual(self.view().items, {})
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.launched(), 0)
        self.assertTrue(all(v["projects"] == [PROJECT] for v in self.linear.variables("issues")))

    def test_baseline_ticket_excluded_by_issues_is_refused(self):
        self.rolando_moves("ENG-186")
        self.preparer.by_issue["ENG-186"] = contract_for("ENG-186")
        self.rolando_approves("ENG-186")
        r = self.tick()
        self.assertEqual(r.refused, ["ENG-186"])
        self.assertEqual(self.view().items, {})
        self.assertTrue(any("baseline" in t for t in self.texts("ENG-186")))
        self.assertEqual(self.launched(), 0)

    def test_skip_label_is_refused(self):
        self.rolando_moves(labels=("baseline",))
        self.rolando_approves("ENG-187")
        r = self.tick()
        self.assertEqual(r.refused, ["ENG-187"])
        self.assertEqual(self.launched(), 0)

    # --- changes before launch ---

    def test_edit_after_the_move_before_launch_closes_the_item(self):
        eid = self.rolando_moves()
        self.tick()  # accepted; no approval yet, so it waits
        self.assertIsNone(self.item(eid).closed)
        self.linear.edit("ENG-187", self.now + timedelta(minutes=1), description="Also do X")
        self.rolando_approves("ENG-187")
        self.tick(minutes=6)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")
        closing = [t for t in self.texts() if "stopped before starting" in t]
        self.assertEqual(len(closing), 1)
        self.assertIn("description", closing[0])
        self.assertIn("out of Todo and back", closing[0])
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 0)

    def test_moved_out_before_launch_closes_the_item(self):
        eid = self.rolando_moves()
        self.tick()
        self.linear.move("ENG-187", BACKLOG, self.now + timedelta(minutes=1))
        self.rolando_approves("ENG-187")
        self.tick(minutes=6)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")
        self.assertTrue(any("moved to Backlog" in t for t in self.texts()))
        self.assertEqual(self.launched(), 0)

    def test_back_and_forth_before_start_replaces_the_queued_move(self):
        first = self.rolando_moves(ago=timedelta(minutes=10))
        self.tick()
        self.assertIn(first, self.view().items)
        self.linear.move("ENG-187", BACKLOG, self.now + timedelta(minutes=1))
        second = self.linear.move("ENG-187", TODO, self.now + timedelta(minutes=2))
        r = self.tick(minutes=5)
        self.assertEqual(r.accepted, ["ENG-187"])
        self.assertEqual(self.item(first).closed, "replaced")
        self.assertIsNone(self.item(second).closed)
        self.assertTrue(any("newer move" in t for t in self.texts()))
        self.rolando_approves("ENG-187")
        for _ in range(3):
            self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(len(self.fires()), 1)

    def test_back_and_forth_after_launch_never_fires_again(self):
        first = self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(self.launched(), 1)
        self.linear.move("ENG-187", BACKLOG, self.now + timedelta(minutes=1))
        second = self.linear.move("ENG-187", TODO, self.now + timedelta(minutes=2))
        for _ in range(4):
            self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(len(self.fires()), 1)
        self.assertNotIn(second, self.view().items)
        self.assertIn(second, self.view().seen)
        self.assertIsNone(self.item(first).closed)
        self.assertTrue(any("already queued or in progress" in t for t in self.texts()))

    # --- the single lane ---

    def test_two_tickets_in_one_poll_fire_one_per_round(self):
        a = self.rolando_moves("ENG-187", ago=timedelta(minutes=6))
        b = self.rolando_moves("ENG-188", ago=timedelta(minutes=5))
        self.rolando_approves("ENG-187")
        self.rolando_approves("ENG-188")
        r = self.tick()
        self.assertEqual(sorted(r.accepted), ["ENG-187", "ENG-188"])
        self.assertEqual(len(r.fired), 1)
        for _ in range(3):
            self.assertLessEqual(len(self.tick(minutes=6).fired), 1)
        # Single lane: the second waits while the first runs.
        self.assertEqual(self.launched(), 1)
        self.assertEqual(set(self.view().items), {a, b})
        self.assertTrue(any("Waiting" in t for t in self.texts("ENG-188")))

    def test_two_moves_of_one_ticket_racing_in_one_poll(self):
        self.rolando_moves(ago=timedelta(minutes=10))
        self.linear.move("ENG-187", BACKLOG, self.now - timedelta(minutes=9))
        last = self.linear.move("ENG-187", TODO, self.now - timedelta(minutes=8))
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(list(self.view().items), [last])
        self.assertEqual(self.launched(), 1)

    # --- blocked tickets ---

    def test_blocked_ticket_waits_then_starts_when_the_blocker_is_done(self):
        eid = self.rolando_moves()
        self.linear.find("ENG-187")["inverseRelations"]["nodes"] = [
            {"type": "blocks", "issue": {"identifier": "ENG-190", "state": {"type": "started"}}}
        ]
        self.rolando_approves("ENG-187")
        self.tick()
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 0)
        waiting = [t for t in self.texts() if "blocked by" in t]
        self.assertEqual(len(waiting), 1)
        self.assertIn("ENG-190", waiting[0])
        self.assertIsNone(self.item(eid).closed)
        self.linear.find("ENG-187")["inverseRelations"]["nodes"][0]["issue"]["state"] = {
            "type": "completed"
        }
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)

    # --- changes after launch ---

    def launch(self):
        eid = self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(self.launched(), 1)
        return eid

    def test_moved_to_backlog_after_launch_is_withdrawn_and_reconciled(self):
        eid = self.launch()
        advisor = Advise()
        self.service._x = Integrations(
            self.source, self.preparer, self.reporter, self.reviewer, advisor
        )
        self.linear.move("ENG-187", BACKLOG, self.now + timedelta(minutes=1))
        self.tick(minutes=6)
        item = self.item(eid)
        self.assertIsNotNone(item.withdrawn)
        self.assertIsNone(item.closed)
        notes = [t for t in self.texts() if "no longer stands" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("can't stop a worker", notes[0])
        self.assertIn("keeps checking GitHub", notes[0])
        # The attempt is still reconciled, and its end suggests no repair.
        a1 = AttemptId(self.task("ENG-187"), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr(state="closed", attempt=a1)]
        self.tick(minutes=6)
        self.recovery_close(a1)
        self.tick(minutes=6)
        self.assertEqual(self.item(eid).closed, "withdrawn")
        self.assertEqual(advisor.calls, [])
        self.assertTrue(any("suggests no repair" in t for t in self.texts()))
        self.assertFalse(any("go-ahead" in t for t in self.texts()))
        self.assertEqual(self.launched(), 1)

    def test_text_edited_after_launch_is_reported_once(self):
        eid = self.launch()
        digest = self.item(eid).digest
        self.linear.edit("ENG-187", self.now + timedelta(minutes=1), title="Something else")
        for _ in range(4):
            self.tick(minutes=6)
        notes = [t for t in self.texts() if "changed while the worker was running" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("title", notes[0])
        item = self.item(eid)
        self.assertEqual(item.digest, digest)
        self.assertIsNone(item.withdrawn)
        self.assertIsNone(item.closed)
        self.assertEqual(self.launched(), 1)

    def test_started_by_the_factory_still_stands(self):
        eid = self.launch()
        self.linear.move(
            "ENG-187", IN_PROGRESS, self.now + timedelta(minutes=1), botActor=GITHUB_BOT
        )
        for _ in range(3):
            self.tick(minutes=6)
        self.assertIsNone(self.item(eid).withdrawn)
        self.assertFalse(any("no longer stands" in t for t in self.texts()))

    def test_merge_moving_the_ticket_to_done_is_not_a_withdrawal(self):
        # Was a bug (fixed): Service._watch (controller/service/service.py, _watch ->
        # _check_standing_while_running) checks the move again right after the
        # reconcile that first sees the PR merged, before closing the item. Linear's
        # GitHub integration moves a ticket to Done when its PR merges, and
        # linear.standing() treats Done as withdrawn, so every merged ticket gets
        # "This ticket no longer stands as approved (the ticket was moved to Done)
        # ... It can't stop a worker that is already running ..." on the success
        # path, and the item is marked withdrawn.
        eid = self.launch()
        a1 = AttemptId(self.task("ENG-187"), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [
            self.pr(attempt=a1, state="closed", merged=True, merge_commit="c" * 40)
        ]
        self.linear.move("ENG-187", DONE, self.now + timedelta(minutes=1), botActor=GITHUB_BOT)
        self.tick(minutes=6)
        self.assertEqual(self.item(eid).closed, "merged")
        self.assertFalse(any("no longer stands" in t for t in self.texts()))
        self.assertIsNone(self.item(eid).withdrawn)

    # --- restarts and outages ---

    def test_restart_mid_intake_keeps_the_cursor_and_adds_nothing_twice(self):
        held = self.rolando_moves(ago=timedelta(seconds=30))
        other = self.rolando_moves("ENG-188", ago=timedelta(minutes=10))
        self.tick()
        cursor = self.view().cursor
        self.assertIsNotNone(cursor)
        self.assertEqual(list(self.view().items), [other])
        self.restart()
        self.tick(minutes=SETTLE.total_seconds() / 60)
        self.assertEqual(set(self.view().items), {other, held})
        self.restart()
        self.tick(minutes=1)
        self.restart()
        self.tick(minutes=6)
        self.assertEqual(set(self.view().items), {other, held})
        accepted = [s for s in self.store.events() if s.event.kind == "intake-accepted"]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len([t for t in self.texts() if "queued" in t]), 1)

    def test_restart_polls_from_the_saved_cursor(self):
        self.rolando_moves()
        self.tick()
        saved = self.view().cursor
        self.restart()
        self.tick(minutes=1)
        from controller.intake import linear

        since = linear._when(self.linear.variables("issues")[-1]["since"])
        self.assertEqual(since, linear._when(saved) - linear.OVERLAP)

    def test_linear_unreachable_records_nothing_and_catches_up(self):
        self.tick()
        cursor = self.view().cursor
        eid = self.rolando_moves(ago=timedelta(minutes=3))
        self.linear.fail = LinearUnavailable("Linear answered HTTP 503")
        before = len(self.store.events())
        r = self.tick(minutes=1)
        self.assertTrue(any(e.startswith("intake:") for e in r.errors))
        self.assertEqual(self.view().cursor, cursor)
        self.assertNotIn(eid, self.view().seen)
        kinds = {s.event.kind for s in self.store.events()[before:]}
        self.assertFalse(kinds & {"intake-accepted", "intake-refused", "intake-cursor"})
        self.linear.fail = None
        self.rolando_approves("ENG-187")
        r = self.tick(minutes=1)
        self.assertEqual(r.accepted, ["ENG-187"])
        self.assertEqual(self.launched(), 1)

    def test_linear_down_while_running_still_reconciles(self):
        eid = self.launch()
        self.linear.fail = LinearUnavailable("down")
        a1 = AttemptId(self.task("ENG-187"), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr(attempt=a1)]
        self.tick(minutes=6)
        self.tick(minutes=6)
        self.assertIn(str(a1), self.view().reviews)
        self.assertIsNone(self.item(eid).withdrawn)
        self.assertEqual(self.launched(), 1)

    def test_factory_key_acting_as_rolando_blocks_intake(self):
        self.linear.viewer["id"] = APPROVER
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        r = self.tick()
        self.assertTrue(any("acts as Rolando" in e for e in r.errors))
        self.assertEqual(self.view().items, {})
        self.assertIsNone(self.view().cursor)
        self.assertEqual(self.launched(), 0)
        self.assertEqual(set(self.linear.names()), {"viewer"})

    def test_onboarding_without_approver_blocks_intake(self):
        del self.config["approver_linear_user_id"]
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        r = self.tick()
        self.assertTrue(any("approver_linear_user_id" in e for e in r.errors))
        self.assertEqual(self.view().items, {})
        self.assertEqual(self.launched(), 0)

    # --- pause and resume from the status ticket ---

    def test_pause_label_by_rolando_stops_new_work_until_removed(self):
        from controller.attempts.policy import LedgerView
        from controller.service.service import PAUSE_HOLD

        self.linear.label(STATUS_KEY, self.now - timedelta(minutes=10), add=["factory-pause"])
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertIn(PAUSE_HOLD, LedgerView.build(self.store.events()).holds)
        self.assertEqual(self.launched(), 0)
        self.assertTrue(any("paused" in t for t in self.reporter.texts(STATUS_ID)))
        self.linear.label(STATUS_KEY, self.now + timedelta(minutes=1), remove=["factory-pause"])
        self.tick(minutes=6)
        self.assertNotIn(PAUSE_HOLD, LedgerView.build(self.store.events()).holds)
        self.assertEqual(self.launched(), 1)

    def test_pause_label_by_a_bot_does_nothing(self):
        from controller.attempts.policy import LedgerView
        from controller.service.service import PAUSE_HOLD

        self.linear.label(
            STATUS_KEY, self.now - timedelta(minutes=10), add=["factory-pause"], botActor=CLAUDE_BOT
        )
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertNotIn(PAUSE_HOLD, LedgerView.build(self.store.events()).holds)
        self.assertEqual(self.launched(), 1)


if __name__ == "__main__":
    unittest.main()
