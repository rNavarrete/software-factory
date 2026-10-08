"""ENG-160: a Todo move records bounded repair terms, never a launch permission."""

import unittest
from datetime import timedelta
from unittest import mock

from controller import contract as contracts
from controller.approval import Approvals
from controller.approval.approval import APPROVER, authentic
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.interfaces import LedgerEvent
from controller.service.onboarding import OnboardingError
from tests.test_onboarding_intake import parse
from tests.test_service import PROJECT, config
from tests.test_todo_move_approval import (
    BACKLOG,
    KEY,
    NOW,
    TODO,
    AuthorizerCase,
    contract_for,
    intake_config,
    launched,
    resign,
    yes,
)

SINCE = (NOW - timedelta(hours=1)).isoformat()


def repair_config(allowance=1, **entry):
    return intake_config(
        entry={
            "repair_allowance": allowance,
            "repair_allowance_since": SINCE,
            **entry,
        }
    )


class OnboardingAllowanceTests(unittest.TestCase):
    def test_existing_projects_default_to_no_repairs(self):
        project = parse(config()).project(PROJECT)
        self.assertEqual(project.repair_allowance, 0)
        self.assertEqual(project.as_mapping()["repair_allowance"], 0)

    def test_allowance_fits_inside_the_total_attempt_budget(self):
        for budget in (1, 2, 3):
            for allowance in range(budget):
                with self.subTest(budget=budget, allowance=allowance):
                    project = parse(
                        config(
                            entry={
                                "max_attempts": budget,
                                "repair_allowance": allowance,
                                "repair_allowance_since": SINCE,
                            }
                        )
                    ).project(PROJECT)
                    self.assertEqual(project.repair_allowance, allowance)
            with self.assertRaises(OnboardingError):
                parse(config(entry={"max_attempts": budget, "repair_allowance": budget}))

    def test_malformed_allowance_refuses_the_configuration(self):
        for value in (True, False, None, "1", 1.0, -1, [], {}):
            with self.subTest(value=value), self.assertRaises(OnboardingError):
                parse(config(entry={"repair_allowance": value}))

    def test_positive_allowance_needs_an_explicit_activation_time(self):
        for since in (None, "", "yesterday", "2026-10-09", True, 123):
            with self.subTest(since=since), self.assertRaises(OnboardingError):
                parse(config(entry={"repair_allowance": 1, "repair_allowance_since": since}))
        with self.assertRaises(OnboardingError):
            parse(config(entry={"repair_allowance": 1}))


class SignedAllowanceTests(AuthorizerCase):
    def test_allowance_is_from_signer_policy_and_bound_to_the_contract(self):
        self.config = repair_config(2)
        event = self.authorize(self.move(), contract=contract_for("ENG-187", attempt_budget=2))
        self.assertEqual(event.data["repair_allowance"], 1)
        self.assertTrue(authentic(event, KEY, frozenset({APPROVER})))
        self.append(event)
        self.assertTrue(self.check(contract_for("ENG-187", attempt_budget=2)).approved)

    def test_no_allowance_is_explicitly_signed_as_zero(self):
        event = self.authorize(self.move())
        self.assertEqual(event.data["repair_allowance"], 0)

    def test_adding_changing_or_removing_the_allowance_breaks_the_signature(self):
        self.config = repair_config()
        event = self.authorize(self.move())
        for value in (0, 2, True, "1", None):
            with self.subTest(value=value):
                changed = LedgerEvent(
                    event.kind, event.at, event.task, data={**event.data, "repair_allowance": value}
                )
                self.assertFalse(authentic(changed, KEY, frozenset({APPROVER})))
        old_data = {k: v for k, v in event.data.items() if k != "repair_allowance"}
        removed = LedgerEvent(event.kind, event.at, event.task, data=old_data)
        self.assertFalse(authentic(removed, KEY, frozenset({APPROVER})))
        legacy = resign(removed)
        self.assertTrue(authentic(legacy, KEY, frozenset({APPROVER})))
        self.append(legacy)
        self.assertTrue(self.check().approved)
        added = LedgerEvent(
            legacy.kind, legacy.at, legacy.task, data={**legacy.data, "repair_allowance": 1}
        )
        self.assertFalse(authentic(added, KEY, frozenset({APPROVER})))

    def test_move_before_activation_cannot_gain_an_allowance(self):
        eid = self.move()
        self.config = repair_config(repair_allowance_since=NOW.isoformat())
        self.assertTrue(self.refused(eid).final)
        self.assertFalse(self.state.exists())

    def test_renewing_or_restarting_cannot_increase_an_existing_allowance(self):
        self.config = repair_config(1)
        eid = self.move()
        self.authorize(eid)
        self.authorizer = self.make_authorizer()
        self.config = repair_config(2)
        self.assertTrue(self.refused(eid).final)

    def test_existing_zero_cannot_be_upgraded_by_a_policy_edit(self):
        eid = self.move()
        self.authorize(eid)
        self.config = repair_config(1)
        self.assertTrue(self.refused(eid).final)

    def test_legacy_move_records_cannot_be_upgraded(self):
        import json

        eid = self.move()
        self.state.write_text(json.dumps({eid: contracts.digest(contract_for("ENG-187")).value}))
        self.assertEqual(self.authorize(eid).data["repair_allowance"], 0)
        self.config = repair_config(1)
        self.assertTrue(self.refused(eid).final)

    def test_positive_allowance_is_pinned_to_its_repair_settings_and_ticket_revision(self):
        self.config = repair_config(1)
        eid = self.move()
        first = self.authorize(eid)
        self.now += timedelta(minutes=40)
        again = self.authorize(eid)
        self.assertEqual(first.data["repair_allowance"], again.data["repair_allowance"])
        self.assertNotEqual(first.data["decision_id"], again.data["decision_id"])
        # An unrelated onboarding edit doesn't block renewing a queued move.
        self.config["projects"][0]["skip_labels"] = ["baseline", "manual"]
        self.authorize(eid)
        self.config["projects"][0]["repair_allowance_since"] = (
            NOW - timedelta(minutes=30)
        ).isoformat()
        self.assertTrue(self.refused(eid).final)
        self.config = repair_config(1)
        self.world.find("ENG-187")["description"] = "Quietly different"
        self.assertTrue(self.refused(eid).final)

    def test_a_new_move_can_bind_new_repair_terms(self):
        self.config = repair_config(1)
        old = self.move(ago=timedelta(minutes=20))
        self.authorize(old)
        self.config = repair_config(2)
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15))
        new = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        self.assertEqual(self.authorize(new).data["repair_allowance"], 2)

    def test_allowance_does_not_bypass_manual_repair_or_writer_clearing(self):
        self.config = repair_config(2)
        eid = self.move()
        contract = contract_for("ENG-187")
        self.append(self.authorize(eid))
        gate = AttemptGate(self.store)
        gate.record_snapshot(NOW, 10, 20, 0, NOW)
        verdict = self.check()
        self.assertTrue(verdict.approved)
        run = gate.reserve(verdict.run.attempt.task, contracts.digest(contract), NOW)
        gate.record_launch(run, launched(), NOW)
        self.assertIn("unresolved-attempt", self.codes(self.check()))
        desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        desk.record_clearing(run.attempt, ev.ClearingBasis.COMPLETED, "https://claude.ai/x", NOW)
        self.append(self.authorize(eid))
        self.assertIn("repair-not-authorized", self.codes(self.check()))
        desk.authorize_repair(contract, 2, "CI red on a1", NOW)
        self.assertTrue(self.check().approved)
        desk.revoke(run.attempt.task, contracts.digest(contract), "stop", NOW)
        self.append(self.authorize(eid))
        self.assertIn("approval-revoked", self.codes(self.check()))

    def test_failed_durable_save_does_not_sign_an_authorization(self):
        self.config = repair_config(1)
        eid = self.move()
        with mock.patch("controller.signer.authorize.os.fsync", side_effect=OSError("disk")):
            with mock.patch.object(KEY, "sign", wraps=KEY.sign) as sign:
                with self.assertRaises(OSError):
                    self.authorize(eid)
                sign.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual(self.authorize(eid).data["repair_allowance"], 1)

    def test_lost_reply_or_directory_sync_failure_still_pins_the_terms(self):
        self.config = repair_config(1)
        eid = self.move()
        with mock.patch(
            "controller.signer.authorize.os.fsync", side_effect=[None, OSError("disk")]
        ):
            with mock.patch.object(KEY, "sign", wraps=KEY.sign) as sign:
                with self.assertRaises(OSError):
                    self.authorize(eid)
                sign.assert_not_called()
        self.authorizer = self.make_authorizer()
        self.config = repair_config(2)
        self.assertTrue(self.refused(eid).final)
        self.config = repair_config(1)
        self.assertEqual(self.authorize(eid).data["repair_allowance"], 1)

    def test_signed_allowance_survives_the_real_ledger_and_restart(self):
        from controller.ledger import SqliteLedgerStore

        self.config = repair_config(2)
        event = self.authorize(self.move())
        self.store = SqliteLedgerStore(self.dir / "ledger.db")
        self.append(event)
        self.store.close()
        self.store = SqliteLedgerStore(self.dir / "ledger.db")
        self.addCleanup(self.store.close)
        saved = self.store.events()[0].event
        self.assertEqual(saved.data["repair_allowance"], 2)
        self.assertTrue(authentic(saved, KEY, frozenset({APPROVER})))
        self.assertTrue(self.check().approved)
