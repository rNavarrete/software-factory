"""The go-live qualification run, rehearsed offline (docs/go-live.md).

The live run uses the shipped pilot settings and three ordinary sample
tickets. This rehearses the same run on the offline harnesses: the shipped
onboarding and drafting files, the exact ticket texts in Linear, and the
whole order of events, with the edge cases the live run could meet. Nothing
here touches the network, starts a worker or runs a review.
"""

import json
import unittest
from datetime import timedelta
from pathlib import Path

from controller.dispatch.dispatch import FACTORY_ROUTINE
from controller.intake import policy_from
from controller.prepare import policy as drafting
from controller.recovery import PILOT_REPO
from controller.service import onboarding
from controller.service.seams import Prepared, Question
from tests.test_intake_linear import BACKLOG, TODO
from tests.test_prepare import authorize, prep, project, ticket
from tests.test_repair import SHA_B, RepairServiceCase, finding
from tests.test_service import SHA_A, contract_for

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "deploy" / "pilot" / "onboarding.json"

# The sample tickets exactly as they are in Linear (Factory Pilot Demo).
WORK_TITLE = "Show how many books are on the list"
WORK_ASKED = """## Outcome

Readers can see how many books are on their reading list, written the way a person would say it.

## Acceptance criteria

- [ ] bookCountLabel(count) returns "1 book" for one book and "N books" for two or more, for example "2 books".
- [ ] For an empty list, bookCountLabel(0) returns TBD: either "No books yet", or an empty string so the count is hidden?
- [ ] bookCountLabel throws an error for a negative or fractional count.

## Notes

Show the label next to the list heading."""  # noqa: E501
EDITED_TEXT = """## Outcome

The README shows how to run a single test file.

## Acceptance criteria

- [ ] README.md shows how to run one test file, using tests/books.test.ts as the example.

## Notes

Used only during the factory's go-live check, to show that editing a ticket after its Todo move stops it before any work starts."""  # noqa: E501
PROBED_TEXT = """## Outcome

The README says which Node version the project uses.

## Acceptance criteria

- [ ] README.md says the project uses Node 22.

## Notes

Used only during the factory's go-live check, to show that a Todo move made through an app (not by Rolando in Linear itself) is refused. It should never start."""  # noqa: E501


def answered(text: str) -> str:
    """Rolando's answer, written into the ticket as the line it replaces."""
    return text.replace(
        'returns TBD: either "No books yet", or an empty string so the count is hidden?',
        'returns "No books yet".',
    )


class ShippedSettingsTests(unittest.TestCase):
    """What the live service will read at the switch."""

    def setUp(self):
        self.config = onboarding.load(PILOT, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
        (self.project,) = self.config.projects.values()

    def test_intake_is_on_from_a_fixed_time(self):
        self.assertTrue(self.config.intake_enabled)
        self.assertIsNotNone(self.config.intake_since)
        policy_from(self.config)  # raises if intake couldn't run

    def test_one_repair_inside_two_attempts_from_the_switch(self):
        self.assertEqual(self.project.max_attempts, 2)
        self.assertEqual(self.project.repair_allowance, 1)
        # Moves before the switch never carry repair terms.
        self.assertEqual(self.project.repair_allowance_since, self.config.intake_since)

    def test_only_listed_tickets_can_start_and_baselines_never(self):
        self.assertEqual(
            sorted(self.project.issues),
            ["ENG-187", "ENG-188", "ENG-189", "ENG-191", "ENG-200", "ENG-201", "ENG-202"],
        )
        for baseline in ("ENG-186", "ENG-190", "ENG-192", "ENG-193"):
            self.assertNotIn(baseline, self.project.issues)
        self.assertIn("baseline", self.project.skip_labels)

    def test_status_ticket_is_set_by_id_and_is_never_work(self):
        # Intake compares Linear's issue id, so the status ticket is named by id.
        self.assertRegex(self.project.status_issue_id, r"^[0-9a-f]{8}-[0-9a-f-]{27}$")
        self.assertNotIn(self.project.status_issue_id, self.project.issues)

    def test_protected_controls_stay_protected(self):
        for path in (".github/", "CLAUDE.md", "package.json", "tsconfig.json"):
            self.assertIn(path, self.project.protected_paths)

    def test_drafting_policy_matches(self):
        policies = drafting.load(ROOT / "deploy" / "pilot" / "drafting.json")
        p = policies.project(self.project.linear_project_id)
        self.assertIsNotNone(p)
        self.assertEqual(p.base.branch, self.project.base_branch)


class SampleTicketTests(unittest.TestCase):
    """The sample tickets through the real drafting code."""

    def prepare(self, text, title, key):
        snap = ticket(text, title=title, key=key)
        return prep(snap).prepare(authorize(snap), project().as_mapping())

    def test_the_work_ticket_first_asks_its_open_question(self):
        out = self.prepare(WORK_ASKED, WORK_TITLE, "ENG-200")
        self.assertIsInstance(out, Question)
        self.assertEqual(out.kind, "product")
        self.assertIn("bookCountLabel(0)", out.text)
        self.assertNotIn("1 book", out.text)  # only the open line is asked about

    def test_the_answered_ticket_becomes_a_tested_task(self):
        out = self.prepare(answered(WORK_ASKED), WORK_TITLE, "ENG-200")
        self.assertIsInstance(out, Prepared, getattr(out, "text", ""))
        c = out.contract
        self.assertEqual(
            [a["evidence"]["type"] for a in c["acceptance_criteria"]], ["automated-check"] * 3
        )
        self.assertEqual(
            c["acceptance_criteria"][1]["statement"],
            'For an empty list, bookCountLabel(0) returns "No books yet".',
        )
        self.assertEqual(c["attempt_budget"], 2)
        for path in c["permitted_paths"]:
            self.assertFalse(path.startswith((".github", "package", "CLAUDE")), path)

    def test_the_two_check_tickets_would_draft_if_they_were_accepted(self):
        # So when they are stopped, it is by the control under test, not the text.
        for key, title, text in (
            ("ENG-201", "Qualification check: a Todo move made by an app", PROBED_TEXT),
            ("ENG-202", "Qualification check: edited after the Todo move", EDITED_TEXT),
        ):
            with self.subTest(key=key):
                self.assertIsInstance(self.prepare(text, title, key), Prepared)


class RehearsalCase(RepairServiceCase):
    """The live run's order of events with the pilot's terms: two attempts, one
    of them an automatic repair. ENG-187 stands in for the work ticket."""

    allowance = 1
    budget = 2

    def setUp(self):
        super().setUp()
        entry = self.config["projects"][0]
        entry["issues"] = [*entry["issues"], "ENG-201", "ENG-202"]
        for key in ("ENG-201", "ENG-202"):
            self.preparer.by_issue[key] = contract_for(key, attempt_budget=self.budget)

    def ticket_launches(self, task):
        return [
            r for r in self.adapter.requests if json.loads(r.text)["contract"]["task_id"] == task
        ]

    def worker_running(self):
        """Steps 1 and 2: the Todo move starts exactly one worker; a restart
        starts nothing more."""
        self.start()
        self.restart()
        self.run_rounds(2)
        self.assertEqual(self.launched(), 1)


class RehearsalTests(RehearsalCase):
    def test_full_run_with_one_repair_and_then_the_cap(self):
        self.worker_running()
        # The review fails attempt 1; the repair waits for Rolando's clearing.
        self.review_fails(self.a1, 7, SHA_A)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(self.count("first it needs to know"), 1)
        self.clear(self.a1)
        r = self.tick(minutes=6)
        self.assertEqual(r.fired, ["eng-187-a2-f1"])
        # A restart during the repair starts nothing more.
        self.restart()
        self.run_rounds(2)
        self.assertEqual(self.launched(), 2)
        # The repair fails review too: the budget is spent and it says so once.
        self.review_fails(self.a2, 8, SHA_B, finding(3))
        self.clear(self.a2)
        self.run_rounds(6)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(self.repair_records()[0].attempt, self.a2)
        self.assertLessEqual(self.count("new ticket"), 2)
        self.assertGreaterEqual(self.count("new ticket"), 1)

    def test_bot_move_while_the_worker_runs_is_refused(self):
        self.worker_running()
        self.linear.add("ENG-201", created=self.now - timedelta(hours=1))
        self.linear.move(
            "ENG-201",
            TODO,
            self.now - timedelta(minutes=5),
            botActor={"type": "oauthClient", "name": "Claude"},
        )
        self.run_rounds(3)
        self.assertEqual(self.ticket_launches("eng-201"), [])
        self.assertTrue(
            any("integration" in t for t in self.texts("ENG-201")), self.texts("ENG-201")
        )

    def test_edit_while_queued_behind_the_worker_is_withdrawn(self):
        self.worker_running()
        eid = self.rolando_moves("ENG-202")
        self.tick(minutes=6)
        self.assertIsNone(self.item(eid).closed)  # queued: the lane is busy
        self.linear.edit("ENG-202", self.now - timedelta(seconds=30), description="Also do X")
        self.run_rounds(2)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")
        self.assertEqual(self.ticket_launches("eng-202"), [])
        # Even once the lane frees up, it never starts.
        self.recovery_close(self.a1)
        self.run_rounds(3)
        self.assertEqual(self.ticket_launches("eng-202"), [])

    def test_work_ticket_leaving_todo_after_start_blocks_the_repair(self):
        # Linear's GitHub sync (or anyone) moving it to In Progress voids repairs.
        self.worker_running()
        self.linear.move("ENG-187", BACKLOG, self.now - timedelta(minutes=1))
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.run_rounds(4)
        self.assertEqual(self.launched(), 1)


class LiveSwitchTests(unittest.TestCase):
    """The practice run's record stays behind at the switch: the live service
    and Rolando's commands use a new, empty home."""

    def test_live_mode_uses_its_own_home_and_commands_follow_it(self):
        start = (ROOT / "deploy" / "fly" / "entrypoint.sh").read_text()
        live = start.split("live)", 1)[1].split(";;", 1)[0]
        practice = start.split("qualification)", 1)[1].split(";;", 1)[0]
        self.assertIn("HOME=/data/factory", live)
        self.assertIn("HOME=/data/qualification", practice)
        self.assertIn("printf '%s\\n' \"$HOME\" >/run/factory-home", start)
        commands = (ROOT / "deploy" / "fly" / "factory").read_text()
        self.assertIn("HOME=$(cat /run/factory-home", commands)

    def test_an_empty_home_has_no_attempts_holds_or_alerts(self):
        import tempfile

        from controller.attempts import AttemptGate
        from controller.ledger import SqliteLedgerStore
        from controller.service.queue import ServiceView

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteLedgerStore(Path(tmp) / "ledger.db")
            try:
                self.assertEqual(list(store.events()), [])
                self.assertEqual(ServiceView.build(store.events()).items, {})
                from datetime import UTC, datetime

                self.assertEqual(list(AttemptGate(store).run_alerts(datetime.now(UTC))), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
