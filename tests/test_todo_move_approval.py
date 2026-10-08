"""Rolando's verified Todo move as the approval itself (ENG-174, his choice of 2026-10-08).

- ``Approvals`` with ``source_routine``: when a signed ``source-authorization``
  counts, and when it never does.
- ``TodoMoveAuthorizer`` (the signer's side): it reads Linear and the
  onboarding file itself and signs only Rolando's own settled move, for one
  contract inside the project's limits.
- The signer socket's ``authorize`` request and ``SignerAuthorizer``.
- The service asking for the authorization and appending it, never signing.

Linear is ``FakeLinear`` from tests/test_intake_linear.py; nothing touches
the network.
"""

import functools
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval import Approvals, StaticKey, authentic
from controller.approval import approval as approval_module
from controller.approval.approval import (
    APPROVER,
    HUMAN_DECISION,
    MAX_SOURCE_TTL,
    SOURCE_AUTHORIZATION,
    sign_source_authorization,
)
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.dispatch import Dispatcher
from controller.intake import LinearSource, LinearUnavailable, judge, policy_from, revision
from controller.intake.linear import IntakeBlocked, parse_ticket
from controller.interfaces import AttemptId, LedgerEvent, TaskId
from controller.recovery import Recovery
from controller.service import onboarding
from controller.service.fixtures import NoRepair
from controller.service.seams import AuthorizationRefused, Integrations, Standing
from controller.service.service import Heartbeat, Service, never
from controller.signer import SignerKey, SignerServer, SignerUnavailable
from controller.signer import signer as signer_module
from controller.signer.authorize import (
    TTL,
    OneContractPerMove,
    Refused,
    TodoMoveAuthorizer,
    event_from_json,
    event_to_json,
    policy_sha256,
)
from controller.signer.signer import SignerAuthorizer, authorize_handler
from tests.test_approval import yes
from tests.test_attempts import MemoryLedger, launched
from tests.test_intake_linear import BACKLOG, CLAUDE_BOT, IN_PROGRESS, MARIA, TODO, FakeLinear
from tests.test_intake_service import (
    APPROVER as LINEAR_APPROVER,
)
from tests.test_intake_service import (
    FACTORY_TICKETS,
    SINCE,
    STATUS_ID,
    IntakeServiceCase,
)
from tests.test_recovery import BOT, REPO
from tests.test_service import KEY, NOW, PROJECT, START_KEY, TRIG, config, contract_for

logging.getLogger("factory").addHandler(logging.NullHandler())

OTHER_KEY = StaticKey(b"o" * 32)


def intake_config(**changes):
    entry = {"status_issue_id": STATUS_ID, "issues": FACTORY_TICKETS, "skip_labels": ["baseline"]}
    entry.update(changes.pop("entry", {}))
    doc = config(
        approver_linear_user_id=LINEAR_APPROVER, intake_since=SINCE.isoformat(), entry=entry
    )
    doc.update(changes)
    return doc


def resign(event, key=KEY, **changes):
    """Sign a changed copy of a source-authorization, as the signer would."""
    signature = ("mac", "key_id", "decision_id", "binding_sha256")
    data = {k: v for k, v in event.data.items() if k not in signature}
    base = LedgerEvent(
        changes.pop("kind", event.kind),
        event.at,
        changes.pop("task", event.task),
        changes.pop("attempt", event.attempt),
        changes.pop("run", event.run),
        data | changes,
    )
    return approval_module._sign(base, key)


class AuthorizerCase(unittest.TestCase):
    """The signer's ``TodoMoveAuthorizer`` over a fake Linear, wired as on the
    host: ``LinearSource(...).fetch`` with the signer's own transport."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.now = NOW
        self.world = FakeLinear()
        self.config = intake_config()
        self.reader = LinearSource(self.world, lambda: None, now=lambda: self.now)
        self.state = self.dir / "moves.json"
        self.authorizer = self.make_authorizer()
        self.store = MemoryLedger()

    def make_authorizer(self, key=KEY):
        return TodoMoveAuthorizer(
            key,
            self.load_config,
            self.reader.fetch,
            self.reader.viewer_id,
            OneContractPerMove(self.state),
            self.clock,
        )

    def clock(self):
        return self.now

    def load_config(self):
        return onboarding.parse(json.dumps(self.config).encode(), repository=REPO, routine_id=TRIG)

    def move(self, key="ENG-187", ago=timedelta(minutes=5), **fields):
        if self.world.find(key) is None:
            self.world.add(key, created=SINCE + timedelta(minutes=1))
        return self.world.move(key, TODO, self.now - ago, **fields)

    def ticket(self, key="ENG-187"):
        node = self.world.find(key)
        return parse_ticket(node, node["history"]["nodes"])

    def authorize(self, event_id, key="ENG-187", contract=None, text=None):
        """Ask as the service would, with the text the contract was drafted
        from (``text``; by default the ticket's text now)."""
        contract = contract_for(key) if contract is None else contract
        text = revision(self.ticket(key)) if text is None else text
        return self.authorizer.authorize(event_id, f"iss-{key.lower()}", contract, text)

    def refused(self, event_id, key="ENG-187", contract=None, text=None):
        with self.assertRaises(Refused) as cm:
            self.authorize(event_id, key, contract, text)
        return cm.exception

    def append(self, *events):
        with self.store.writer_lock():
            return self.store.append(*events)

    def service_approvals(self, key=KEY, routine=TRIG):
        return Approvals(self.store, key, confirm=never, os_user="factory", source_routine=routine)

    def check(self, contract=None, at=None, **kw):
        contract = contract_for("ENG-187") if contract is None else contract
        return self.service_approvals(**kw).check(contract, at or self.now)

    def codes(self, verdict):
        return [b.code for b in verdict.blocks]


class AuthorizerTests(AuthorizerCase):
    # --- Rolando's own move ---

    def test_rolandos_settled_move_is_signed_and_approves_the_contract(self):
        eid = self.move()
        event = self.authorize(eid)
        self.append(event)
        self.assertTrue(self.check().approved)

    def test_signed_fields(self):
        eid = self.move()
        contract = contract_for("ENG-187")
        event = self.authorize(eid, contract=contract)
        d = event.data
        self.assertEqual(event.kind, SOURCE_AUTHORIZATION)
        self.assertEqual(event.task, TaskId("eng-187"))
        self.assertIsNone(event.attempt)
        self.assertIsNone(event.run)
        self.assertEqual(event.at, NOW)
        self.assertEqual(d["identity"], APPROVER)
        self.assertEqual(APPROVER, "rNavarrete")
        self.assertEqual(d["event_id"], eid)
        self.assertEqual(d["issue_id"], "iss-eng-187")
        self.assertEqual(d["issue_key"], "ENG-187")
        self.assertEqual(d["revision"], revision(self.ticket()))
        self.assertEqual(d["routine_id"], TRIG)
        self.assertEqual(d["linear_actor_id"], LINEAR_APPROVER)
        self.assertEqual(d["policy_sha256"], policy_sha256(self.load_config(), PROJECT))
        self.assertEqual(d["decided_at"], NOW.isoformat())
        self.assertEqual(d["expires_at"], (NOW + TTL).isoformat())
        self.assertEqual(TTL, timedelta(minutes=30))
        self.assertLessEqual(TTL, MAX_SOURCE_TTL)
        self.assertEqual(d["digest"], contracts.digest(contract).value)
        self.assertEqual(d["decision"], "approved")
        self.assertEqual(d["key_id"], KEY.key_id)
        self.assertTrue(authentic(event, KEY, frozenset({APPROVER})))

    def test_policy_hash_follows_the_onboarding_entry(self):
        base = policy_sha256(self.load_config(), PROJECT)
        for name, changes in {
            "issues": dict(entry={"issues": ["ENG-187"]}),
            "skip labels": dict(entry={"skip_labels": ["other"]}),
            "budget": dict(entry={"max_attempts": 2}),
            "approver": dict(approver_linear_user_id="user-someone"),
        }.items():
            with self.subTest(name):
                saved = self.config
                self.config = intake_config(**changes)
                self.assertNotEqual(policy_sha256(self.load_config(), PROJECT), base)
                self.config = saved

    def test_the_authorization_expires_with_its_ttl(self):
        self.append(self.authorize(self.move()))
        self.assertTrue(self.check(at=NOW + TTL - timedelta(seconds=1)).approved)
        self.assertIn("approval-expired", self.codes(self.check(at=NOW + TTL)))

    # --- refusals ---

    def test_refusals_that_asking_again_could_fix_are_not_final(self):
        eid = self.move()
        cases = {
            "intake off": lambda: self.config.update(intake_enabled=False),
            "no approver": lambda: self.config.pop("approver_linear_user_id"),
            "no intake_since": lambda: self.config.pop("intake_since"),
            "unusable onboarding": lambda: self.config.update(format="v0"),
            "linear down": lambda: setattr(self.world, "fail", LinearUnavailable("503")),
            "network error": lambda: setattr(self.world, "fail", OSError("reset")),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.config = intake_config()
                self.world.fail = None
                change()
                r = self.refused(eid)
                self.assertFalse(r.final, r.reason)
        self.assertFalse(self.state.exists())

    def test_moves_that_are_not_rolandos_own_are_refused_for_good(self):
        cases = {
            "a bot": dict(botActor=CLAUDE_BOT),
            "another user": dict(actor=MARIA),
            "an import": dict(issueImport={"id": "imp-1"}),
            "an automation": dict(workflowMetadata={"__typename": "X"}),
        }
        for i, (name, fields) in enumerate(cases.items()):
            with self.subTest(name):
                key = FACTORY_TICKETS[i]
                eid = self.move(key, **fields)
                r = self.refused(eid, key)
                self.assertTrue(r.final)
                self.assertIn("Rolando", r.reason)

    def test_ticket_edited_after_the_move_is_refused(self):
        eid = self.move(ago=timedelta(minutes=10))
        self.world.edit("ENG-187", NOW - timedelta(minutes=5), description="Also do X")
        r = self.refused(eid)
        self.assertTrue(r.final)
        self.assertIn("description", r.reason)

    def test_baseline_ticket_is_refused(self):
        eid = self.move("ENG-186")
        r = self.refused(eid, "ENG-186", contract_for("ENG-186"))
        self.assertTrue(r.final)

    def test_only_the_latest_move_and_its_own_id_count(self):
        old = self.move(ago=timedelta(minutes=20))
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15))
        new = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        self.assertTrue(self.refused(old).final)
        self.assertTrue(self.refused("hist-9999").final)
        self.authorize(new)

    def test_move_out_of_todo_is_refused(self):
        eid = self.move(ago=timedelta(minutes=10))
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=1))
        self.assertTrue(self.refused(eid).final)

    def test_gone_ticket_is_refused(self):
        eid = self.move()
        text = revision(self.ticket())
        self.world.nodes.clear()
        self.assertTrue(self.refused(eid, text=text).final)

    def test_event_for_another_ticket_is_refused(self):
        eid = self.move("ENG-187")
        self.move("ENG-188")
        with self.assertRaises(Refused) as cm:
            self.authorizer.authorize(
                eid, "iss-eng-188", contract_for("ENG-188"), revision(self.ticket("ENG-188"))
            )
        self.assertTrue(cm.exception.final)

    def test_paths_that_may_reach_protected_files_are_refused(self):
        self.config = intake_config(entry={"protected_paths": [".github/", "package.json"]})
        eid = self.move()
        for paths in (
            [".github/workflows/ci.yml"],
            [".github/**"],
            ["src/books.ts", "package.json"],
            ["**"],
            ["*.json"],
            ["package.json.bak"],
        ):
            with self.subTest(paths=paths):
                e = self.refused(eid, contract=contract_for("ENG-187", permitted_paths=paths))
                self.assertTrue(e.final)
                self.assertIn("typed approval", e.reason)

    def test_paths_clear_of_protected_files_are_signed(self):
        self.config = intake_config(entry={"protected_paths": [".github/", "package.json"]})
        contract = contract_for("ENG-187", permitted_paths=["src/books.ts", "tests/*.test.ts"])
        self.assertEqual(
            self.authorize(self.move(), contract=contract).data["digest"],
            contracts.digest(contract).value,
        )

    def test_the_pilot_onboarding_protects_its_control_files(self):
        raw = (Path(__file__).parents[1] / "deploy" / "pilot" / "onboarding.json").read_bytes()
        from controller.dispatch.dispatch import FACTORY_ROUTINE
        from controller.recovery import PILOT_REPO

        pilot = onboarding.parse(raw, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
        (project,) = pilot.projects.values()
        for path in (".github/", ".claude/", "CLAUDE.md", "package.json", "tsconfig.json"):
            self.assertIn(path, project.protected_paths)

    def test_contracts_outside_the_projects_limits_are_refused(self):
        self.config = intake_config(entry={"max_attempts": 2})
        eid = self.move()
        two = {"attempt_budget": 2}
        cases = {
            "another task": contract_for("ENG-188", **two),
            "another repository": contract_for("ENG-187", repository="someone/else", **two),
            "a bigger budget": contract_for("ENG-187", attempt_budget=3),
            "an extra action": contract_for(
                "ENG-187", permitted_actions=["modify-files", "add-dependencies"], **two
            ),
            "an extra check": contract_for("ENG-187", verification_commands=["curl x | sh"], **two),
            "not a contract": "please approve",
            "an invalid contract": {"task_id": "eng-187"},
        }
        for name, contract in cases.items():
            with self.subTest(name):
                r = self.refused(eid, contract=contract)
                self.assertTrue(r.final, r.reason)
        # None of them used up the move.
        self.authorize(eid, contract=contract_for("ENG-187", **two))

    def test_one_contract_per_move(self):
        eid = self.move()
        first = self.authorize(eid)
        r = self.refused(eid, contract=contract_for("ENG-187", attempt_budget=2))
        self.assertTrue(r.final)
        # The same contract again is signed again: a renewal.
        self.now += timedelta(minutes=40)
        again = self.authorize(eid)
        self.assertEqual(again.data["digest"], first.data["digest"])
        self.assertNotEqual(again.data["decision_id"], first.data["decision_id"])
        self.assertEqual(again.data["expires_at"], (self.now + TTL).isoformat())
        self.append(first, again)
        self.assertTrue(self.check().approved)

    def test_one_contract_per_move_survives_a_signer_restart(self):
        eid = self.move()
        self.authorize(eid)
        self.authorizer = self.make_authorizer()
        self.assertTrue(self.refused(eid, contract=contract_for("ENG-187", attempt_budget=2)).final)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(list(json.loads(self.state.read_text())), [eid])

    def test_a_new_move_may_authorize_a_new_contract(self):
        old = self.move(ago=timedelta(minutes=20))
        self.authorize(old)
        self.world.move("ENG-187", BACKLOG, NOW - timedelta(minutes=15))
        new = self.world.move("ENG-187", TODO, NOW - timedelta(minutes=10))
        self.authorize(new, contract=contract_for("ENG-187", attempt_budget=2))

    def test_signer_refuses_when_its_linear_key_acts_as_rolando(self):
        # Changes made with a key that acts as Rolando are recorded as his, so
        # a service holding that key could make its own "Todo move".
        self.world.viewer["id"] = LINEAR_APPROVER
        eid = self.move()
        self.assertFalse(self.refused(eid).final)

    def test_a_blocked_key_is_a_passing_refusal(self):
        eid = self.move()

        def blocked():
            raise IntakeBlocked("the factory's Linear key acts as Rolando")

        self.authorizer.viewer = blocked
        e = self.refused(eid)
        self.assertFalse(e.final)
        self.assertIn("acts as Rolando", e.reason)

    def test_text_changed_without_a_history_entry_is_not_signed(self):
        # An edit Linear never wrote to the history: the signer compares the
        # text the contract was drafted from with the text it reads now.
        eid = self.move()
        accepted = judge(self.ticket(), policy_from(self.load_config()), self.now)
        self.world.find("ENG-187")["description"] = "Quietly different"
        self.assertTrue(self.refused(eid, text=accepted.revision).final)
        self.assertEqual(self.authorize(eid).data["revision"], revision(self.ticket()))


class SourceAuthorizationApprovalTests(AuthorizerCase):
    """When ``Approvals`` counts a signed source-authorization."""

    def setUp(self):
        super().setUp()
        self.eid = self.move()
        self.event = self.authorize(self.eid)
        self.contract = contract_for("ENG-187")

    def test_counts_only_where_built_with_the_same_routine(self):
        self.append(self.event)
        self.assertTrue(self.check().approved)
        plain = Approvals(self.store, KEY, confirm=never)
        self.assertIn("approval-unauthenticated", self.codes(plain.check(self.contract, NOW)))
        self.assertIn("approval-unauthenticated", self.codes(self.check(routine="trig_other")))

    def test_counts_only_for_its_own_contract(self):
        self.append(self.event)
        other = contract_for("ENG-187", attempt_budget=2)
        self.assertFalse(self.check(other).approved)
        self.assertFalse(self.check(contract_for("ENG-188")).approved)

    def test_must_name_its_own_task_and_no_attempt_or_run(self):
        a1 = AttemptId(TaskId("eng-187"), 1)
        cases = {
            "issue key of another task": resign(self.event, issue_key="ENG-188"),
            "an attempt": resign(self.event, attempt=a1),
        }
        for name, event in cases.items():
            with self.subTest(name):
                self.store = MemoryLedger()
                self.append(event)
                self.assertFalse(self.check().approved)

    def test_lasts_at_most_an_hour(self):
        for ttl, ok in (
            (MAX_SOURCE_TTL, True),
            (MAX_SOURCE_TTL + timedelta(seconds=1), False),
            (timedelta(days=3), False),
            (timedelta(0), False),
        ):
            with self.subTest(ttl=ttl):
                self.store = MemoryLedger()
                self.append(resign(self.event, expires_at=(NOW + ttl).isoformat()))
                self.assertEqual(self.check().approved, ok)

    def test_unsigned_forged_or_tampered_never_count(self):
        def without(name):
            return {k: v for k, v in self.event.data.items() if k != name}

        def tampered(**changes):
            return LedgerEvent(
                self.event.kind, self.event.at, self.event.task, data=self.event.data | changes
            )

        cases = {
            "no mac": LedgerEvent(SOURCE_AUTHORIZATION, NOW, self.event.task, data=without("mac")),
            "another key": resign(self.event, key=OTHER_KEY),
            "revision changed": tampered(revision="f" * 64),
            "event id changed": tampered(event_id="hist-9999"),
            "routine changed": tampered(routine_id="trig_other"),
            "expiry stretched": tampered(expires_at=(NOW + timedelta(minutes=50)).isoformat()),
            "identity changed": tampered(identity="mallory"),
            "other approver": resign(self.event, identity="mallory"),
            "task changed": LedgerEvent(
                SOURCE_AUTHORIZATION, NOW, TaskId("eng-188"), data=self.event.data
            ),
        }
        for name, event in cases.items():
            with self.subTest(name):
                self.store = MemoryLedger()
                self.append(event)
                self.assertFalse(self.check().approved)
                self.assertFalse(self.check(contract_for("ENG-188")).approved)

    def test_can_never_pass_for_a_typed_approval(self):
        as_human = LedgerEvent(HUMAN_DECISION, NOW, self.event.task, data=self.event.data)
        self.append(as_human)
        self.assertFalse(self.check().approved)
        self.assertFalse(
            Approvals(self.store, KEY, confirm=never).check(self.contract, NOW).approved
        )
        with self.assertRaises(ValueError):
            sign_source_authorization(as_human, KEY)

    def test_a_copy_counts_once_and_changes_nothing(self):
        self.append(self.event, self.event)
        self.assertTrue(self.check().approved)

    def test_withdrawn_by_a_later_rejection_or_revocation(self):
        for decision in ("reject", "revoke"):
            with self.subTest(decision):
                self.store = MemoryLedger()
                self.append(self.event)
                desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
                later = NOW + timedelta(minutes=1)
                if decision == "reject":
                    desk.reject(self.contract, "not this", later)
                else:
                    desk.revoke(TaskId("eng-187"), contracts.digest(self.contract), "stop", later)
                verdict = self.check(at=later + timedelta(minutes=1))
                self.assertFalse(verdict.approved)
                code = {"reject": "approval-rejected", "revoke": "approval-revoked"}[decision]
                self.assertIn(code, self.codes(verdict))

    def test_asking_the_signer_again_after_a_withdrawal_brings_nothing_back(self):
        # A compromised service can call the signer itself; the signer would
        # sign the same contract for the same move with a later time.
        for decision in ("reject", "revoke"):
            with self.subTest(decision):
                self.store = MemoryLedger()
                self.append(self.event)
                desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
                later = NOW + timedelta(minutes=1)
                if decision == "reject":
                    desk.reject(self.contract, "not this", later)
                else:
                    desk.revoke(TaskId("eng-187"), contracts.digest(self.contract), "stop", later)
                self.now = later + timedelta(minutes=1)
                again = self.authorize(self.eid)
                self.assertGreater(again.at, later)
                self.append(again)
                self.assertFalse(self.check(at=self.now).approved)
                # Rolando's own typed approval still brings it back.
                desk.approve(self.contract, self.now)
                self.assertTrue(self.check(at=self.now).approved)

    def test_a_withdrawal_of_another_contract_changes_nothing(self):
        desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        desk.reject(contract_for("ENG-187", attempt_budget=2), "not this", NOW)
        self.append(self.event)
        self.assertTrue(self.check().approved)

    def test_repairs_still_need_a_typed_go_ahead(self):
        self.append(self.event)
        gate = AttemptGate(self.store)
        gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))
        verdict = self.check()
        self.assertTrue(verdict.approved)
        run = gate.reserve(TaskId("eng-187"), contracts.digest(self.contract), NOW)
        self.assertEqual(run, verdict.run)
        gate.record_launch(run, launched(), NOW)
        desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        desk.record_clearing(run.attempt, ev.ClearingBasis.COMPLETED, "https://claude.ai/x", NOW)
        # The Todo move covers attempt 1 only; a fresh one changes nothing.
        self.append(self.authorize(self.eid))
        self.assertIn("repair-not-authorized", self.codes(self.check()))
        desk.authorize_repair(self.contract, 2, "CI red on a1", NOW)
        self.assertTrue(self.check().approved)


def serve_signer(case, allowed=None):
    """A real signer with the ``authorize`` request on a socket in ``case.dir``,
    answered by ``case.authorizer``; sets ``case.path`` and ``case.client``."""

    class Delegate:
        def authorize(self, event_id, issue_id, contract, text):
            return case.authorizer.authorize(event_id, issue_id, contract, text)

    case.path = case.dir / "signer.sock"
    allowed = {os.getuid()} if allowed is None else allowed
    server = SignerServer(
        KEY, case.path, allowed_uids=allowed, extra={"authorize": authorize_handler(Delegate())}
    )
    server.listen()
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop.is_set,), daemon=True)
    thread.start()

    def shutdown():
        stop.set()
        try:  # wake the accept loop so it sees the stop at once
            signer_module.call(case.path, {"op": "key-id"}, timeout=1)
        except SignerUnavailable:
            pass
        thread.join(5)
        server.close()

    case.addCleanup(shutdown)
    case.client = SignerAuthorizer(case.path, functools.partial(signer_module.call, timeout=5))


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED is Linux only")
class SignerSocketTests(AuthorizerCase):
    """The ``authorize`` request over a real socket."""

    def setUp(self):
        super().setUp()
        serve_signer(self)

    def authorization(self, key="ENG-187"):
        a = judge(self.ticket(key), policy_from(self.load_config()), self.now)
        self.assertIsNotNone(a)
        return a

    def test_round_trip_approves_through_the_signer_key(self):
        self.move()
        event = self.client.authorize(self.authorization(), contract_for("ENG-187"))
        self.assertEqual(event.kind, SOURCE_AUTHORIZATION)
        self.append(event)
        signer_key = SignerKey(self.path, functools.partial(signer_module.call, timeout=5))
        service = Approvals(self.store, signer_key, confirm=never, source_routine=TRIG)
        self.assertTrue(service.check(contract_for("ENG-187"), NOW).approved)
        self.assertFalse(
            Approvals(self.store, signer_key, confirm=never)
            .check(contract_for("ENG-187"), NOW)
            .approved
        )

    def test_refusals_come_back_with_their_finality(self):
        self.move("ENG-188", botActor=CLAUDE_BOT)
        bot = judge(self.ticket("ENG-188"), policy_from(self.load_config()), self.now)
        from controller.service.seams import Authorization

        as_if = Authorization(
            bot.event_id, bot.issue_id, bot.issue_key, PROJECT, "x", NOW, "0" * 64, "x"
        )
        with self.assertRaises(AuthorizationRefused) as cm:
            self.client.authorize(as_if, contract_for("ENG-188"))
        self.assertTrue(cm.exception.final)
        self.move()
        self.config["intake_enabled"] = False
        with self.assertRaises(AuthorizationRefused) as cm:
            self.client.authorize(self.authorization(), contract_for("ENG-187"))
        self.assertFalse(cm.exception.final)

    def test_malformed_authorize_requests_are_errors(self):
        for req in (
            {"op": "authorize", "event_id": 1, "issue_id": "iss-eng-187"},
            {"op": "authorize", "issue_id": "iss-eng-187"},
        ):
            with self.subTest(req), self.assertRaises(SignerUnavailable):
                signer_module.call(self.path, req, timeout=5)
        self.assertFalse(self.state.exists())

    def test_contract_survives_the_trip_unchanged(self):
        self.move()
        contract = contracts.freeze(contract_for("ENG-187"))
        event = self.client.authorize(self.authorization(), contract)
        self.assertEqual(event.data["digest"], contracts.digest(contract).value)


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED is Linux only")
class SignerRefusedCallerTests(AuthorizerCase):
    def test_caller_not_allowed_gets_nothing_signed(self):
        serve_signer(self, allowed={os.getuid() + 4242})
        eid = self.move()
        a = judge(self.ticket(), policy_from(self.load_config()), self.now)
        self.assertEqual(a.event_id, eid)
        with self.assertRaises(SignerUnavailable):
            self.client.authorize(a, contract_for("ENG-187"))
        self.assertFalse(self.state.exists())


class EventJsonTests(AuthorizerCase):
    def test_round_trip_keeps_the_signature(self):
        event = self.authorize(self.move())
        raw = json.loads(json.dumps(event_to_json(event)))
        back = event_from_json(raw)
        self.assertEqual((back.kind, back.at, back.task), (event.kind, event.at, event.task))
        self.assertEqual(json.loads(json.dumps(back.data, default=dict)), raw["data"])
        self.assertTrue(authentic(back, KEY, frozenset({APPROVER})))
        self.append(back)
        self.assertTrue(self.check().approved)

    def test_a_changed_round_trip_breaks_the_signature(self):
        raw = event_to_json(self.authorize(self.move()))
        for name, change in {
            "kind": lambda r: r.update(kind=HUMAN_DECISION),
            "task": lambda r: r.update(task="eng-188"),
            "revision": lambda r: r["data"].update(revision="f" * 64),
        }.items():
            with self.subTest(name):
                bad = json.loads(json.dumps(raw))
                change(bad)
                self.assertFalse(authentic(event_from_json(bad), KEY, frozenset({APPROVER})))


# --- The service ---------------------------------------------------------------------


class Wrapped:
    """The service's authorizer: a real TodoMoveAuthorizer behind the seam,
    as the signer socket would answer. ``answer`` overrides it."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []
        self.answer = None

    def authorize(self, authorization, contract):
        self.calls.append(authorization.event_id)
        if self.answer is not None:
            return self.answer(authorization, contract)
        try:
            return self.inner.authorize(
                authorization.event_id, authorization.issue_id, contract, authorization.revision
            )
        except Refused as e:
            raise AuthorizationRefused(e.reason, final=e.final) from None


class TodoMoveServiceCase(IntakeServiceCase):
    dispatcher_class = Dispatcher

    def setUp(self):
        self.signer_state = None
        super().setUp()

    def build(self):
        """ServiceCase.build, with the dispatcher's Approvals counting
        source-authorizations for its routine and an authorizer integration."""
        if not hasattr(self, "linear"):
            return super().build()  # ServiceCase.setUp; restart() builds again
        if self.signer_state is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self.signer_state = Path(tmp.name) / "moves.json"
            self.signer_config = lambda: self.load_config()
            reader = LinearSource(self.linear, lambda: None, now=self.clock)
            self.authorizer = Wrapped(
                TodoMoveAuthorizer(
                    KEY,
                    lambda: self.signer_config(),
                    reader.fetch,
                    reader.viewer_id,
                    OneContractPerMove(self.signer_state),
                    self.clock,
                )
            )
        self.gate = AttemptGate(self.store)
        self.approvals = Approvals(
            self.store, KEY, confirm=never, os_user="factory-service", source_routine=TRIG
        )
        self.recovery = Recovery(
            self.store,
            self.approvals,
            self.github,
            worker_logins=frozenset({BOT}),
            gate=self.gate,
            confirm=never,
        )
        self.dispatcher = self.dispatcher_class(
            self.store,
            self.approvals,
            self.recovery,
            self.gate,
            self.base,
            routine_id=TRIG,
            adapter=lambda trig, key: self.adapter,
            start_key=lambda trig: START_KEY,
            model_config_version="test",
            now=self.clock,
            sleep=lambda s: None,
            warn=lambda text: None,
        )
        self.service = Service(
            self.store,
            self.dispatcher,
            self.recovery,
            self.gate,
            approval_module.ContractStore(self.root / "contracts"),
            self.load_config,
            Integrations(
                self.source,
                self.preparer,
                self.reporter,
                self.reviewer,
                NoRepair(),
                authorizer=self.authorizer,
            ),
            now=self.clock,
            heartbeat=Heartbeat(self.root / "service.heartbeat"),
            backup=self.backups.append,
        )

    def kinds(self, task="eng-187"):
        return [s.event.kind for s in self.store.events(TaskId(task))]

    def source_authorizations(self, task="eng-187"):
        return [k for k in self.kinds(task) if k == SOURCE_AUTHORIZATION]


class TodoMoveServiceTests(TodoMoveServiceCase):
    def test_todo_move_alone_fires_exactly_once(self):
        eid = self.rolando_moves()
        r = self.tick()
        self.assertEqual(r.errors, [])
        self.assertEqual(r.fired, ["eng-187-a1-f1"])
        for _ in range(4):
            self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(len(self.fires()), 1)
        self.assertEqual(self.authorizer.calls, [eid])
        self.assertEqual(len(self.source_authorizations()), 1)
        self.assertNotIn(HUMAN_DECISION, {s.event.kind for s in self.store.events()})

    def test_no_authorizer_call_when_a_typed_approval_stands(self):
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(self.launched(), 1)
        self.assertEqual(self.authorizer.calls, [])
        self.assertEqual(self.source_authorizations(), [])

    def test_final_refusal_closes_the_item_with_a_message(self):
        eid = self.rolando_moves()

        def refuse(a, c):
            raise AuthorizationRefused("the move was made by an app", final=True)

        self.authorizer.answer = refuse
        r = self.tick()
        self.assertEqual(self.item(eid).closed, "authorization-refused")
        self.assertIn("ENG-187", r.closed)
        texts = [t for t in self.texts() if "can't count your Todo move" in t]
        self.assertEqual(len(texts), 1)
        self.assertIn("made by an app", texts[0])
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 0)

    def test_refusal_that_may_pass_later_waits_with_one_notice(self):
        eid = self.rolando_moves()

        def refuse(a, c):
            raise AuthorizationRefused("Linear could not be read", final=False)

        self.authorizer.answer = refuse
        for i in range(4):
            self.tick(minutes=6 if i else 0)
        self.assertIsNone(self.item(eid).closed)
        self.assertEqual(len(self.authorizer.calls), 4)
        waits = [t for t in self.texts() if "Linear could not be read" in t]
        self.assertEqual(len(waits), 1)
        self.assertEqual(self.launched(), 0)
        self.authorizer.answer = None
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)

    def test_answer_for_another_contract_or_move_is_an_error(self):
        eid = self.rolando_moves()
        node = self.linear.find("ENG-187")
        text = revision(parse_ticket(node, node["history"]["nodes"]))
        good = self.authorizer.inner.authorize(eid, "iss-eng-187", contract_for("ENG-187"), text)
        other_move = resign(good, event_id="hist-9999")
        other_contract = contract_for("ENG-187", attempt_budget=2)
        other_digest = resign(
            good,
            digest=contracts.digest(other_contract).value,
            binding=contracts.binding(contracts.freeze(other_contract)),
        )
        other_task = resign(good, task=TaskId("eng-188"), issue_key="ENG-188")
        as_human = LedgerEvent(HUMAN_DECISION, good.at, good.task, data=good.data)
        for name, answer in {
            "another move": other_move,
            "another contract": other_digest,
            "another task": other_task,
            "another kind": as_human,
        }.items():
            with self.subTest(name):
                self.authorizer.answer = lambda a, c, answer=answer: answer
                before = len(self.store.events())
                r = self.tick(minutes=6)
                self.assertTrue(any("different move or contract" in e for e in r.errors))
                self.assertEqual(r.fired, [])
                new = [s.event.kind for s in self.store.events()[before:]]
                self.assertNotIn(SOURCE_AUTHORIZATION, new)
                self.assertNotIn(HUMAN_DECISION, new)
        self.assertEqual(self.launched(), 0)

    def test_authorization_lapsing_while_waiting_on_the_lane_is_asked_again(self):
        first = self.rolando_moves("ENG-187", ago=timedelta(minutes=6))
        second = self.rolando_moves("ENG-188", ago=timedelta(minutes=5))
        self.tick()
        self.assertEqual(self.launched(), 1)
        self.tick(minutes=6)  # ENG-188 is authorized, then waits on the lane
        self.assertEqual(self.authorizer.calls, [first, second])
        for _ in range(6):
            self.tick(minutes=6)
        self.assertGreaterEqual(self.authorizer.calls.count(second), 2)
        self.assertGreaterEqual(len(self.source_authorizations("eng-188")), 2)
        self.assertEqual(self.launched(), 1)
        # The lane frees; ENG-188 fires on a fresh authorization.
        a1 = AttemptId(self.task("ENG-187"), 1)
        self.recovery_close(a1)
        session = next(
            s.event.data["session_url"]
            for s in self.store.events(a1.task)
            if s.event.kind == ev.FIRE_RESULT and s.event.data.get("session_url")
        )
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").record_clearing(
            a1, ev.ClearingBasis.COMPLETED, session, self.now
        )
        self.tick(minutes=6)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(self.item(first).closed, "failed")
        self.assertNotIn(HUMAN_DECISION, self.kinds("eng-188"))

    def test_edit_after_the_move_is_refused_by_the_signer_itself(self):
        eid = self.rolando_moves()
        blockers = self.linear.find("ENG-187")["inverseRelations"]["nodes"]
        blockers.append(
            {"type": "blocks", "issue": {"identifier": "ENG-190", "state": {"type": "started"}}}
        )
        self.tick()  # accepted; waits on ENG-190 before asking the signer
        self.assertEqual(self.authorizer.calls, [])
        self.linear.edit("ENG-187", self.now + timedelta(minutes=1), description="Also do X")
        blockers.clear()
        # Bypass the service's own check: it would close the item itself.
        self.source.standing = lambda a: Standing()
        self.tick(minutes=6)
        self.assertEqual(self.authorizer.calls, [eid])
        self.assertEqual(self.item(eid).closed, "authorization-refused")
        refused = [t for t in self.texts() if "can't count your Todo move" in t]
        self.assertEqual(len(refused), 1)
        self.assertIn("description", refused[0])
        self.assertEqual(self.launched(), 0)
        self.assertEqual(self.source_authorizations(), [])

    def test_unready_signer_onboarding_waits(self):
        eid = self.rolando_moves()
        self.signer_config = lambda: onboarding.parse(
            json.dumps(intake_config(intake_enabled=False)).encode(),
            repository=REPO,
            routine_id=TRIG,
        )
        self.tick()
        self.assertIsNone(self.item(eid).closed)
        self.assertEqual(self.launched(), 0)
        self.assertTrue(any("switched off" in t for t in self.texts()))

    def test_rolandos_rejection_is_not_undone_by_asking_the_signer_again(self):
        # The signer can't see the ledger and would re-sign the same contract
        # for the same move, so the service must not ask after a rejection.
        from controller.attempts.policy import LedgerView

        self.gate.hold("manual", "hold for the test", self.now)
        eid = self.rolando_moves()
        self.tick()  # authorized by the signer, then held
        self.assertEqual(self.launched(), 0)
        self.assertEqual(len(self.source_authorizations()), 1)
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").reject(
            contract_for("ENG-187"), "not this task", self.now + timedelta(minutes=1)
        )
        self.gate.resume("go", self.now + timedelta(minutes=2), reason="manual")
        self.assertEqual(LedgerView.build(self.store.events()).holds, {})
        for _ in range(3):
            self.tick(minutes=6)
        self.assertIn(eid, self.view().items)
        self.assertEqual(self.launched(), 0)


class ChangedWhileQueuedTests(TodoMoveServiceCase):
    """Rolando's review of 2d6ad80: what changes while an approved ticket
    waits for the lane must still stop it before launch."""

    def queued(self, typed=False):
        self.gate.hold("manual", "hold for the test", self.now)
        eid = self.rolando_moves()
        if typed:
            self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(self.launched(), 0)
        return eid

    def resume(self):
        self.gate.resume("go", self.now, reason="manual")
        for _ in range(3):
            self.tick(minutes=6)

    def out_and_back_by_a_bot(self, typed):
        eid = self.queued(typed)
        self.linear.move("ENG-187", IN_PROGRESS, self.now + timedelta(seconds=10))
        self.linear.move("ENG-187", TODO, self.now + timedelta(seconds=20), botActor=CLAUDE_BOT)
        self.resume()
        self.assertEqual(self.launched(), 0)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")

    def test_out_of_todo_and_back_by_a_bot_cancels_the_queued_task(self):
        self.out_and_back_by_a_bot(typed=False)

    def test_out_of_todo_and_back_by_a_bot_cancels_a_typed_approval_too(self):
        self.out_and_back_by_a_bot(typed=True)

    def test_out_of_todo_and_back_by_rolando_starts_the_new_move_only(self):
        old = self.queued()
        self.linear.move("ENG-187", IN_PROGRESS, self.now + timedelta(seconds=10))
        self.linear.move("ENG-187", TODO, self.now + timedelta(seconds=20))
        self.resume()
        self.assertIsNotNone(self.item(old).closed)
        self.assertEqual(self.launched(), 1)

    def removed_from_the_allowed_list(self, typed):
        eid = self.queued(typed)
        self.config["projects"][0]["issues"] = ["ENG-188"]
        self.resume()
        self.assertEqual(self.launched(), 0)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")

    def test_ticket_removed_from_the_allowed_list_does_not_start(self):
        self.removed_from_the_allowed_list(typed=False)

    def test_ticket_removed_from_the_allowed_list_does_not_start_when_typed(self):
        self.removed_from_the_allowed_list(typed=True)

    def test_baseline_label_added_while_waiting_does_not_start(self):
        eid = self.queued(typed=True)
        self.linear.label("ENG-187", self.now + timedelta(seconds=10), add=["baseline"])
        self.resume()
        self.assertEqual(self.launched(), 0)
        self.assertEqual(self.item(eid).closed, "authorization-withdrawn")

    def test_paths_protected_while_waiting_need_a_typed_approval(self):
        eid = self.queued()
        self.assertEqual(len(self.source_authorizations()), 1)
        self.config["projects"][0]["protected_paths"] = ["src/"]
        self.resume()
        self.assertEqual(self.launched(), 0)
        self.assertIsNone(self.item(eid).closed)
        waits = [t for t in self.texts() if "typed approval" in t]
        self.assertEqual(len(waits), 1)
        self.rolando_approves("ENG-187")
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 1)


class RealDispatcherTests(TodoMoveServiceCase):
    def test_real_dispatcher_fires_on_a_todo_move(self):
        self.rolando_moves()
        r = self.tick()
        self.assertEqual(r.errors, [])
        self.assertEqual(self.launched(), 1)

    def test_real_dispatcher_fires_a_typed_approval_with_an_authorizer(self):
        self.rolando_moves()
        self.rolando_approves("ENG-187")
        self.tick()
        self.assertEqual(self.launched(), 1)


class NoSigningInTheServiceTests(unittest.TestCase):
    def test_the_service_never_signs(self):
        root = Path(__file__).parents[1] / "controller" / "service"
        files = sorted(root.glob("*.py"))
        self.assertTrue(files)
        for path in files:
            text = path.read_text()
            for banned in (
                "sign_source_authorization",
                "approval_module._sign",
                "import _sign",
                "_sign(",
            ):
                with self.subTest(file=path.name, call=banned):
                    self.assertNotIn(banned, text)

    def test_service_module_does_not_import_the_signing_helpers(self):
        import ast

        path = Path(__file__).parents[1] / "controller" / "service" / "service.py"
        names = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        self.assertFalse(names & {"sign_source_authorization", "_sign", "sign", "StaticKey"})


class SeamTests(unittest.TestCase):
    def test_signer_authorizer_fits_the_seam(self):
        from controller.service.seams import Authorizer

        self.assertIsInstance(SignerAuthorizer("/nonexistent"), Authorizer)
        with self.assertRaises(SignerUnavailable):
            SignerAuthorizer(Path(tempfile.gettempdir()) / "no-such-signer.sock").authorize(
                _Auth(), contract_for("ENG-187")
            )


class _Auth:
    event_id = "hist-0001"
    issue_id = "iss-eng-187"
    revision = "0" * 64


if __name__ == "__main__":
    unittest.main()
