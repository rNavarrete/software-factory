"""Bounded automatic repairs (ENG-160).

- ``controller.repair.policy``: which failures the factory may repair on its
  own, and where it stops.
- ``Approvals``: when a signed ``source-repair-authorized`` record lets
  attempt n start, and every way it doesn't.
- The signer's ``use_repair_allowance``: what it checks with Linear and the
  onboarding file before it signs.
- The worker payload's ``repair`` block.
- The service end to end, over a fake Linear, a fake GitHub, the real
  dispatcher, gate, recovery and signer code, and a fake worker runtime:
  one routine repair, the allowance, the cap, stale commits, a ticket leaving
  Todo, restarts, races, holds and forged records.

Nothing here touches the network, Linear, GitHub or the real start endpoint.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from controller import contract as contracts
from controller.adapter import routine
from controller.adapter.routine import PayloadRejected, build_fire_text, repair_brief
from controller.approval import Approvals
from controller.approval import approval as approval_module
from controller.approval.approval import MAX_SOURCE_TTL, authentic
from controller.attempts import events as ev
from controller.attempts.policy import LedgerView
from controller.interfaces import AttemptId, LedgerEvent, TaskId
from controller.ledger import SqliteLedgerStore
from controller.ledger.kinds import InvalidEvent
from controller.recovery import PR_OBSERVED
from controller.repair import findings as rf
from controller.repair import policy
from controller.repair.findings import RepairFinding
from controller.repair.review import ReviewFailures
from controller.service import onboarding
from controller.service.fixtures import FixtureFailures
from controller.service.seams import AuthorizationRefused, FailureReport, FailureSource
from controller.signer.authorize import Refused, event_from_json, event_to_json
from controller.signer.signer import REPAIR_OP, SignerAuthorizer, authorize_handler
from tests.test_approval import yes
from tests.test_intake_linear import BACKLOG
from tests.test_recovery import REPO, SHA_A, SHA_B
from tests.test_service import KEY, TRIG, contract_for
from tests.test_todo_move_approval import (
    OTHER_KEY,
    AuthorizerCase,
    TodoMoveServiceCase,
    Wrapped,
    intake_config,
    resign,
)

SHA_C = "c" * 40


def finding(n=1, *, category="criterion-failed", route="repair", blocking=True, **kw):
    fields = dict(
        id=f"F-{n:012x}",
        category=category,
        summary=f"AC{n} failed: the list shows finished books",
        evidence=f"tests/books.test.ts:{n} expected 2, got 3",
        suggested_action=f"Make AC{n} hold on the candidate.",
        route=route,
        blocking=blocking,
    )
    fields.update(kw)
    return RepairFinding(**fields)


def plan(findings=None, **kw):
    findings = (finding(),) if findings is None else findings
    args = dict(attempts_used=1, contract_budget=3, project_max=3, allowance=1, automatic_used=0)
    args.update(kw)
    return policy.plan(findings, **args)


# --- The decision ------------------------------------------------------------------


class PolicyTests(unittest.TestCase):
    def test_routine_failure_within_the_allowance_is_a_plan(self):
        out = plan()
        self.assertIsInstance(out, policy.Plan)
        self.assertEqual(out.attempt, 2)
        self.assertEqual((out.allowance, out.used), (1, 0))
        self.assertIn("AC1 failed", out.failure)

    def test_every_routine_category(self):
        for category in sorted(policy.ROUTINE_CATEGORIES | policy.ROUTINE_FLAGS) + [
            "assertion-uncovered"
        ]:
            with self.subTest(category=category):
                self.assertIsInstance(plan((finding(category=category),)), policy.Plan)

    def test_anything_routed_to_rolando_stops_even_beside_routine_findings(self):
        out = plan((finding(1), finding(2, category="needs-observation", route="rolando")))
        self.assertIsInstance(out, policy.Stop)
        self.assertEqual(out.code, "needs-rolando")
        self.assertIn("AC2", out.reason)

    def test_protected_and_unknown_categories_are_never_routine(self):
        for category in (
            "integrity",
            "stale-ci",
            "flag-control-change",
            "flag-deleted-test",
            "flag-weakened-test",
            "flag-changed-test",
            "flag-changed-setup",
            "flag-check-suppression",
            "review-product",
            "review-security",
            "something-new",
        ):
            with self.subTest(category=category):
                out = plan((finding(category=category),))
                self.assertIsInstance(out, policy.Stop)
                self.assertEqual(out.code, "not-routine")

    def test_a_request_to_weaken_a_check_is_never_routine_whatever_its_label(self):
        for action in (
            "Delete the failing test.",
            "Skip the flaky assertion for now.",
            "Remove the typecheck step from CI.",
            "Relax the acceptance criterion so it passes.",
            "Comment out the lint check.",
            "Turn off coverage.",
            "Mark it xfail in the tests.",
        ):
            with self.subTest(action=action):
                out = plan((finding(category="review-code", suggested_action=action),))
                self.assertIsInstance(out, policy.Stop)
                self.assertEqual(out.code, "asks-to-weaken")

    def test_ordinary_fixes_are_not_mistaken_for_weakening(self):
        for action in (
            "Make AC1 hold on the candidate.",
            "Add a test whose assertion shows AC2, and that fails without the change.",
            "Strengthen the test so it checks the behavior.",
            "Undo the changes to files the task doesn't permit.",
        ):
            with self.subTest(action=action):
                self.assertIsInstance(plan((finding(suggested_action=action),)), policy.Plan)

    def test_advisory_findings_alone_are_nothing_to_repair(self):
        out = plan((finding(blocking=False),))
        self.assertEqual(out.code, "nothing-to-repair")

    def test_too_many_findings(self):
        many = tuple(finding(i) for i in range(1, rf.MAX_FINDINGS + 2))
        self.assertEqual(plan(many).code, "too-many-findings")

    def test_no_allowance_asks_rolando(self):
        out = plan(allowance=0)
        self.assertEqual(out.code, "no-allowance")
        self.assertIn("attempt 2", out.decision)
        self.assertFalse(out.capped)

    def test_allowance_used(self):
        out = plan(attempts_used=2, automatic_used=1)
        self.assertEqual(out.code, "allowance-used")
        self.assertIn("attempt 3", out.decision)

    def test_capped_by_the_contract_or_the_project(self):
        for kw in (dict(contract_budget=2), dict(project_max=2)):
            with self.subTest(**kw):
                out = plan(attempts_used=2, automatic_used=0, allowance=2, **kw)
                self.assertEqual(out.code, "capped")
                self.assertTrue(out.capped)


class FindingsTests(unittest.TestCase):
    def test_bad_findings_are_refused_not_cut(self):
        for kw in (
            dict(id="has space"),
            dict(category="Upper"),
            dict(summary=" "),
            dict(evidence="x" * (rf.MAX_FIELD_CHARS + 1)),
            dict(route="wait"),
        ):
            with self.subTest(**kw), self.assertRaises(ValueError):
                finding(**kw)

    def test_digest_is_stable_and_binds_every_field(self):
        data = rf.as_data([finding(1), finding(2)])
        self.assertEqual(rf.digest(data), rf.digest(json.loads(json.dumps(data))))
        self.assertEqual(rf.digest(data), rf.digest(tuple(data)))
        changed = [dict(data[0], summary="something else"), data[1]]
        self.assertNotEqual(rf.digest(data), rf.digest(changed))
        self.assertIsNone(rf.digest([dict(data[0], extra="x")]))
        self.assertIsNone(rf.digest("not a list"))

    def test_secret_looking_text_is_blanked_before_it_is_bound(self):
        """The ledger redacts on write; the finding is redacted first, so the
        digest the signer binds is still the one the ledger hands back."""
        f = finding(evidence="token: sk-ant-oat01-" + "x" * 40)
        (data,) = rf.as_data([f])
        self.assertNotIn("sk-ant", data["evidence"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SqliteLedgerStore(Path(tmp.name) / "ledger.db")
        with store.writer_lock():
            store.append(LedgerEvent("note", NOW, data={"findings": [data]}))
        back = store.events()[-1].event.data["findings"]
        self.assertEqual(rf.digest(back), rf.digest([data]))


# --- Signing and counting ------------------------------------------------------------


from tests.test_todo_move_approval import NOW  # noqa: E402


class RepairSignerCase(AuthorizerCase):
    """The signer's side, and how ``Approvals`` counts what it signs."""

    def setUp(self):
        super().setUp()
        self.config = intake_config(entry={"repair_allowance": 1})
        self.contract = contract_for("ENG-187")
        self.digest = contracts.digest(self.contract)
        self.a1 = AttemptId(TaskId("eng-187"), 1)
        self.a2 = AttemptId(TaskId("eng-187"), 2)
        self.eid = self.move()
        self.findings = rf.as_data([finding(1)])

    def first_attempt(self, head=SHA_A, pr=7):
        """The Todo move approves attempt 1; it fires and opens PR ``pr``."""
        self.append(self.authorize(self.eid, contract=self.contract))
        gate_events = [
            ev.attempt_reserved(self.a1, self.digest, self.now),
            LedgerEvent(ev.FIRE_INTENT, self.now, self.a1.task, self.a1, run(self.a1), {}),
            LedgerEvent(
                ev.FIRE_RESULT,
                self.now,
                self.a1.task,
                self.a1,
                run(self.a1),
                {"outcome": "launched", "http_status": 200, "session_url": SESSION},
            ),
        ]
        self.append(*gate_events)
        self.observe(self.a1, pr, head)

    def observe(self, attempt, pr, head, **changes):
        data = {
            "number": pr,
            "head_sha": head,
            "state": "open",
            "merged": False,
            "matches": True,
        }
        data.update(changes)
        self.append(LedgerEvent(PR_OBSERVED, self.now, attempt.task, attempt, run(attempt), data))

    def clear(self, attempt=None):
        attempt = attempt or self.a1
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").record_clearing(
            attempt, ev.ClearingBasis.COMPLETED, SESSION, self.now
        )

    def use(self, attempt=2, head=SHA_A, pr=7, contract=None, findings=None, eid=None):
        return self.authorizer.use_repair_allowance(
            eid or self.eid,
            "iss-eng-187",
            self.contract if contract is None else contract,
            self.revision(),
            attempt,
            pr,
            head,
            "AC1 failed",
            self.findings if findings is None else findings,
        )

    def use_refused(self, **kw):
        with self.assertRaises(Refused) as cm:
            self.use(**kw)
        return cm.exception

    def revision(self):
        from controller.intake import revision

        return revision(self.ticket())

    def check(self, contract=None, at=None, **kw):
        return super().check(self.contract if contract is None else contract, at, **kw)

    def ready(self):
        """Attempt 1 failed with its session cleared; attempt 2's go-ahead appended."""
        self.first_attempt()
        self.clear()
        event = self.use()
        self.append(event)
        return event


SESSION = "https://claude.ai/code/cse_1"


def run(attempt):
    from controller.interfaces import RunId

    return RunId(attempt, 1)


class SignerRepairTests(RepairSignerCase):
    def test_signs_a_repair_for_exactly_the_next_attempt(self):
        self.first_attempt()
        event = self.use()
        d = event.data
        self.assertEqual(event.kind, ev.SOURCE_REPAIR_AUTHORIZED)
        self.assertEqual(event.attempt, self.a2)
        self.assertEqual(event.task, TaskId("eng-187"))
        self.assertIsNone(event.run)
        self.assertEqual(d["digest"], self.digest.value)
        self.assertEqual(d["event_id"], self.eid)
        self.assertEqual(d["prior_head"], SHA_A)
        self.assertEqual(d["prior_pr"], "7")
        self.assertEqual(d["repair_allowance"], "1")
        self.assertEqual([dict(f) for f in d["findings"]], self.findings)
        self.assertEqual(d["findings_sha256"], rf.digest(self.findings))
        self.assertIn("not a decision Rolando made", d["authenticated_by"])
        self.assertIn("factory", d["by"])
        self.assertEqual(
            datetime_of(d["expires_at"]) - datetime_of(d["decided_at"]), timedelta(minutes=30)
        )
        self.assertTrue(
            authentic(event, [KEY], frozenset({approval_module.APPROVER})),
        )

    def test_refuses_without_an_allowance(self):
        self.first_attempt()
        self.config = intake_config()
        e = self.use_refused()
        self.assertTrue(e.final)
        self.assertIn("no automatic repairs", e.reason)

    def test_refuses_past_the_allowance_or_the_budget(self):
        self.first_attempt()
        self.assertIn("would be repair 2", self.use_refused(attempt=3).reason)
        self.config = intake_config(entry={"repair_allowance": 2, "max_attempts": 3})
        contract = contract_for("ENG-187", attempt_budget=2)
        e = self.use_refused(attempt=3, contract=contract)
        self.assertTrue(e.final)

    def test_refuses_attempt_one(self):
        self.first_attempt()
        self.assertIn("attempt 2 or later", self.use_refused(attempt=1).reason)

    def test_refuses_a_contract_the_move_did_not_authorize(self):
        # No source authorization was ever signed for this move.
        e = self.use_refused()
        self.assertIn("did not authorize this contract", e.reason)
        self.first_attempt()
        other = contract_for("ENG-187", goal="Something else entirely")
        self.assertIn("did not authorize", self.use_refused(contract=other).reason)

    def test_refuses_once_the_ticket_left_todo_or_changed(self):
        self.first_attempt()
        self.world.move("ENG-187", BACKLOG, self.now - timedelta(minutes=1))
        self.assertTrue(self.use_refused().final)

    def test_refuses_when_the_ticket_text_changed(self):
        self.first_attempt()
        old = self.revision()
        self.world.edit("ENG-187", self.now - timedelta(minutes=1), title="Something else")
        with self.assertRaises(Refused) as cm:
            self.authorizer.use_repair_allowance(
                self.eid, "iss-eng-187", self.contract, old, 2, 7, SHA_A, "x", self.findings
            )
        self.assertTrue(cm.exception.final)

    def test_intake_off_is_a_passing_refusal(self):
        self.first_attempt()
        self.config = dict(self.config, intake_enabled=False)
        self.assertFalse(self.use_refused().final)

    def test_refuses_bad_inputs(self):
        self.first_attempt()
        for kw, words in (
            (dict(head="abc"), "40-character"),
            (dict(pr=0), "pull request"),
            (dict(findings=[]), "findings"),
            (dict(findings=[{"id": "x"}]), "findings"),
        ):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                self.assertIn(words, self.use_refused(**kw).reason)

    def test_one_failed_commit_per_repair_attempt(self):
        self.first_attempt()
        first = self.use()
        again = self.use()  # the same commit: renewed
        self.assertEqual(again.data["prior_head"], first.data["prior_head"])
        e = self.use_refused(head=SHA_B)
        self.assertTrue(e.final)
        self.assertIn("another failed commit", e.reason)

    def test_event_json_keeps_the_attempt(self):
        self.first_attempt()
        event = self.use()
        back = event_from_json(json.loads(json.dumps(event_to_json(event))))
        self.assertEqual(back.attempt, self.a2)
        self.assertEqual(back.kind, event.kind)
        self.assertTrue(authentic(back, [KEY], frozenset({approval_module.APPROVER})))

    def test_socket_request_and_client(self):
        self.first_attempt()
        handler = authorize_handler(self.authorizer)
        sent = []

        def transport(path, req):
            sent.append(req)
            return handler(json.loads(json.dumps(req)), 0)

        client = SignerAuthorizer("/unused", transport)
        a = self.store_authorization()
        event = client.use_repair_allowance(
            a, self.contract, self.a2, 7, SHA_A, "AC1 failed", tuple(self.findings)
        )
        self.assertEqual(sent[0]["op"], REPAIR_OP)
        self.assertEqual(event.attempt, self.a2)
        self.assertEqual(event.kind, ev.SOURCE_REPAIR_AUTHORIZED)
        with self.assertRaises(AuthorizationRefused) as cm:
            client.use_repair_allowance(
                a, self.contract, self.a2, 7, SHA_B, "AC1 failed", self.findings
            )
        self.assertTrue(cm.exception.final)

    def store_authorization(self):
        from controller.service.seams import Authorization

        return Authorization(
            self.eid,
            "iss-eng-187",
            "ENG-187",
            "proj-pilot",
            "Rolando",
            self.now - timedelta(minutes=5),
            self.revision(),
            "test",
        )


def datetime_of(text):
    from datetime import datetime

    return datetime.fromisoformat(text)


class SourceRepairCountingTests(RepairSignerCase):
    """``Approvals.check`` for attempt 2 under the allowance."""

    def codes(self, verdict):
        return [b.code for b in verdict.blocks]

    def test_counts_once_everything_holds(self):
        self.ready()
        v = self.check()
        self.assertEqual(self.codes(v), [])
        self.assertEqual(v.run.attempt, self.a2)
        record = self.service_approvals().automatic_repair(self.contract, self.a2, self.now)
        self.assertEqual(record["prior_head"], SHA_A)

    def test_terminal_approvals_never_count_it(self):
        self.ready()
        terminal = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        self.assertIn("repair-not-authorized", self.codes(terminal.check(self.contract, self.now)))

    def test_other_routine_or_key(self):
        self.ready()
        self.assertIn("repair-not-authorized", self.codes(self.check(routine="trig_other")))
        self.assertIn("repair-not-authorized", self.codes(self.check(key=OTHER_KEY)))

    def test_expired_or_not_yet_valid(self):
        self.ready()
        later = self.now + timedelta(minutes=31)
        # Renew the attempt-1 approval so only the repair record is at issue.
        self.now = later - timedelta(minutes=1)
        self.append(self.authorize(self.eid, contract=self.contract))
        self.now = NOW
        self.assertIn("repair-not-authorized", self.codes(self.check(at=later)))
        self.assertIn(
            "repair-not-authorized", self.codes(self.check(at=self.now - timedelta(seconds=1)))
        )

    def test_a_longer_life_than_allowed_never_counts(self):
        self.first_attempt()
        self.clear()
        event = self.use()
        long = resign(
            event,
            expires_at=(self.now + MAX_SOURCE_TTL + timedelta(seconds=1)).isoformat(),
        )
        self.append(long)
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def forged(self, **changes):
        self.first_attempt()
        self.clear()
        self.append(resign(self.use(), **changes))
        return self.codes(self.check())

    def test_over_its_own_allowance(self):
        self.assertIn("repair-not-authorized", self.forged(repair_allowance="0"))

    def test_policy_other_than_the_moves_first_authorization(self):
        self.assertIn("repair-not-authorized", self.forged(policy_sha256="f" * 64))

    def test_findings_changed_after_signing(self):
        tampered = [dict(self.findings[0], suggested_action="Delete the failing test.")]
        self.first_attempt()
        self.clear()
        event = self.use()
        data = dict(event.data, findings=tampered)  # unsigned field, signature intact
        self.append(LedgerEvent(event.kind, event.at, event.task, event.attempt, event.run, data))
        self.assertTrue(authentic(self.store.events()[-1].event, [KEY], frozenset({"rNavarrete"})))
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_another_move_or_contract(self):
        self.assertIn("repair-not-authorized", self.forged(event_id="hist-9999"))

    def test_for_a_different_attempt_number(self):
        self.first_attempt()
        self.clear()
        self.append(resign(self.use(), attempt=AttemptId(TaskId("eng-187"), 3)))
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_unsigned_copy_never_counts(self):
        self.first_attempt()
        self.clear()
        event = self.use()
        data = {k: v for k, v in event.data.items() if k != "mac"}
        self.append(LedgerEvent(event.kind, event.at, event.task, event.attempt, event.run, data))
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_a_push_after_the_verdict_makes_it_stale(self):
        self.ready()
        self.observe(self.a1, 7, SHA_B)
        self.assertIn("repair-not-authorized", self.codes(self.check()))
        self.observe(self.a1, 7, SHA_A)  # back where it was: counts again
        self.assertEqual(self.codes(self.check()), [])

    def test_a_closed_or_unmarked_pr_makes_it_stale(self):
        for changes in (dict(state="closed"), dict(matches=False), dict(merged=True)):
            with self.subTest(**changes):
                self.setUp()
                self.ready()
                self.observe(self.a1, 7, SHA_A, **changes)
                self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_a_revoked_contract_ends_it_even_after_a_fresh_approval(self):
        self.ready()
        rolando = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        rolando.revoke(self.a1.task, self.digest, "stop", self.now)
        rolando.approve(self.contract, self.now)
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_signed_before_attempt_one_started_never_counts(self):
        self.append(self.authorize(self.eid, contract=self.contract))
        early = self.use()  # no reservation yet: there is nothing to repair
        self.store = type(self.store)()
        self.first_attempt()
        self.clear()
        events = self.store.events()
        # Put the early record before the reservation, as a replay would.
        reordered = type(self.store)()
        with reordered.writer_lock():
            reordered.append(events[0].event, early, *(s.event for s in events[1:]))
        self.store = reordered
        self.assertIn("repair-not-authorized", self.codes(self.check()))

    def test_still_needs_the_earlier_writer_cleared(self):
        self.first_attempt()
        self.append(self.use())
        self.assertIn("unresolved-attempt", self.codes(self.check()))

    def test_gate_view_counts_it_like_a_repair(self):
        self.ready()
        self.assertIn(self.a2, LedgerView.build(self.store.events()).repairs)

    def test_pr_observed_name_matches_recovery(self):
        self.assertEqual(approval_module._PR_OBSERVED, PR_OBSERVED)


class LedgerShapeTests(unittest.TestCase):
    def test_malformed_record_is_refused_on_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SqliteLedgerStore(Path(tmp.name) / "ledger.db")
        a2 = AttemptId(TaskId("eng-187"), 2)
        for data in (
            {"by": "factory", "digest": "a" * 64, "decided_at": NOW.isoformat()},
            {"failure": "x", "by": "factory", "digest": "nope"},
        ):
            with self.subTest(data=data), self.assertRaises(InvalidEvent):
                with store.writer_lock():
                    store.append(
                        LedgerEvent(ev.SOURCE_REPAIR_AUTHORIZED, NOW, a2.task, a2, None, data)
                    )


# --- The worker payload ------------------------------------------------------------------


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract_for("ENG-187")
        self.digest = contracts.digest(self.contract)
        self.a1 = AttemptId(TaskId("eng-187"), 1)
        self.a2 = AttemptId(TaskId("eng-187"), 2)
        self.brief = repair_brief(self.a1, 7, SHA_A, rf.as_data([finding(1), finding(2)]))

    def test_repair_block_goes_to_attempt_two(self):
        text = build_fire_text(self.contract, self.digest, self.a2, repair=self.brief)
        envelope = json.loads(text)
        self.assertEqual(envelope["repair"]["previous_branch"], "claude/eng-187-a1")
        self.assertEqual(envelope["repair"]["previous_pr"], 7)
        self.assertEqual(len(envelope["repair"]["findings"]), 2)
        # The adapter checks it again before sending.
        self.assertEqual(routine._text_errors(text, self.digest, self.a2, contracts.validate), [])

    def test_never_on_attempt_one(self):
        with self.assertRaises(PayloadRejected):
            build_fire_text(self.contract, self.digest, self.a1, repair=self.brief)

    def test_without_repair_the_envelope_is_unchanged(self):
        envelope = json.loads(build_fire_text(self.contract, self.digest, self.a2))
        self.assertEqual(set(envelope), routine.ENVELOPE_KEYS)

    def test_malformed_repair_blocks(self):
        for change in (
            {"previous_attempt": 2},
            {"previous_branch": "claude/eng-187-a9"},
            {"previous_pr": True},
            {"previous_head": "abc"},
            {"findings": []},
            {"findings": [{"id": "x"}]},
            {"extra": 1},
        ):
            with self.subTest(change=change), self.assertRaises(PayloadRejected):
                build_fire_text(
                    self.contract, self.digest, self.a2, repair={**self.brief, **change}
                )

    def test_too_long_is_refused_not_cut(self):
        big = [
            finding(i, evidence="e" * rf.MAX_FIELD_CHARS, summary="s" * rf.MAX_FIELD_CHARS)
            for i in range(1, rf.MAX_FINDINGS + 1)
        ]
        brief = repair_brief(self.a1, 7, SHA_A, rf.as_data(big))
        with self.assertRaises(PayloadRejected) as cm:
            build_fire_text(self.contract, self.digest, self.a2, repair=brief)
        self.assertIn("limit", str(cm.exception))

    def test_saved_prompt_explains_the_repair_block(self):
        text = Path(routine.__file__).with_name("routine_prompt.md").read_text()
        self.assertIn("`repair`", text)
        self.assertIn("Never delete, skip or weaken a test", text)


class OnboardingTests(unittest.TestCase):
    def parse(self, **entry):
        doc = intake_config(entry=entry)
        return onboarding.parse(json.dumps(doc).encode(), repository=REPO, routine_id=TRIG)

    def test_defaults_to_no_automatic_repair(self):
        self.assertEqual(self.parse().project("proj-pilot").repair_allowance, 0)

    def test_must_be_below_max_attempts(self):
        self.assertEqual(self.parse(repair_allowance=2).project("proj-pilot").repair_allowance, 2)
        for bad in (3, -1, True, "1", 1.0):
            with self.subTest(bad=bad), self.assertRaises(onboarding.OnboardingError):
                self.parse(repair_allowance=bad)
        with self.assertRaises(onboarding.OnboardingError):
            self.parse(repair_allowance=1, max_attempts=1)

    def test_changes_the_policy_digest(self):
        from controller.signer.authorize import policy_sha256

        a = policy_sha256(self.parse(), "proj-pilot")
        b = policy_sha256(self.parse(repair_allowance=1), "proj-pilot")
        self.assertNotEqual(a, b)


# --- The review bridge -----------------------------------------------------------------


class _Enum:
    def __init__(self, value):
        self.value = value


class _Finding:
    def __init__(self, n, route="repair", severity="blocking", resolved=False, **kw):
        self.resolved = resolved
        self.data = {
            "id": f"F-{n:012x}",
            "severity": severity,
            "category": "criterion-failed",
            "route": route,
            "summary": f"AC{n} failed",
            "evidence": "a test",
            "suggested_action": "Make it hold.",
            "commit": SHA_A,
            "criterion": f"AC{n}",
            "resolved": resolved,
            "source": "verifier",
        }
        self.data.update(kw)

    def as_data(self):
        return dict(self.data)


class _Status:
    def __init__(self, state, attempt, findings, head=SHA_A, pr=7):
        self.state = _Enum(state)
        self.attempt = attempt
        self.pr = pr
        self.key = "rv-test"
        self.revision = type("Rev", (), {"head": head})()
        self.findings = findings


class _Reviewer:
    def __init__(self, status):
        self.status = status

    def evidence(self, attempt):
        return self.status


class ReviewBridgeTests(unittest.TestCase):
    a1 = AttemptId(TaskId("eng-187"), 1)

    def failures(self, status):
        return ReviewFailures(_Reviewer(status)).failure(self.a1)

    def test_failed_verdict_becomes_a_report(self):
        report = self.failures(
            _Status("failed", self.a1, [_Finding(1), _Finding(2, resolved=True)])
        )
        self.assertIsInstance(report, FailureReport)
        self.assertEqual((report.pr, report.head, report.source), (7, SHA_A, "rv-test"))
        self.assertEqual([f.id for f in report.findings], ["F-000000000001"])
        self.assertIsInstance(ReviewFailures(_Reviewer(None)), FailureSource)

    def test_other_verdicts_are_nothing(self):
        for state in ("passed", "needs-rolando", "waiting-for-ci", "unknown", "blocked"):
            with self.subTest(state=state):
                self.assertIsNone(self.failures(_Status(state, self.a1, [_Finding(1)])))
        self.assertIsNone(self.failures(None))
        other = AttemptId(TaskId("eng-187"), 2)
        self.assertIsNone(self.failures(_Status("failed", other, [_Finding(1)])))
        self.assertIsNone(ReviewFailures(object()).failure(self.a1))

    def test_unreadable_or_odd_findings_go_to_rolando(self):
        report = self.failures(
            _Status(
                "failed",
                self.a1,
                [_Finding(1, route="wait"), _Finding(2, summary="x" * 5000), object()],
            )
        )
        self.assertTrue(all(f.route == "rolando" for f in report.findings))
        self.assertIsInstance(plan(report.findings), policy.Stop)


# --- The service, end to end ---------------------------------------------------------------


class RepairWrapped(Wrapped):
    """The service's authorizer, also answering repair requests as the signer would."""

    def setup_repair(self):
        self.repair_calls = []
        self.repair_answer = None

    def use_repair_allowance(
        self, authorization, contract, attempt, prior_pr, prior_head, failure, findings
    ):
        self.repair_calls.append((attempt, prior_head))
        if self.repair_answer is not None:
            return self.repair_answer()
        try:
            return self.inner.use_repair_allowance(
                authorization.event_id,
                authorization.issue_id,
                contract,
                authorization.revision,
                attempt.number,
                prior_pr,
                prior_head,
                failure,
                findings,
            )
        except Refused as e:
            raise AuthorizationRefused(e.reason, final=e.final) from None


class RepairServiceCase(TodoMoveServiceCase):
    allowance = 1
    budget = 3

    def setUp(self):
        self.failures = FixtureFailures()
        super().setUp()
        self.config = intake_config(entry={"repair_allowance": self.allowance})
        self.preparer.by_issue["ENG-187"] = contract_for("ENG-187", attempt_budget=self.budget)
        self.a1 = AttemptId(TaskId("eng-187"), 1)
        self.a2 = AttemptId(TaskId("eng-187"), 2)
        self.a3 = AttemptId(TaskId("eng-187"), 3)

    def build(self):
        super().build()
        if not hasattr(self, "linear"):
            return
        if type(self.authorizer) is Wrapped:
            self.authorizer.__class__ = RepairWrapped
            self.authorizer.setup_repair()
        self.service._x = replace(self.service._x, repair=self.failures, authorizer=self.authorizer)

    # --- helpers ---

    def start(self):
        eid = self.rolando_moves()
        r = self.tick()
        self.assertEqual(r.fired, ["eng-187-a1-f1"])
        return eid

    def contract(self):
        return contract_for("ENG-187", attempt_budget=self.budget)

    def open_pr(self, attempt, number, head):
        self.github.branches[attempt.branch] = head
        pr = self.pr(number=number, attempt=attempt, head_sha=head)
        digest = contracts.digest(self.contract())
        pr = replace(
            pr,
            title=f"{attempt.pr_title_marker(digest)} Filter",
            body=f"x\n\n{digest.pr_body_line}\n",
        )
        self.github.pulls = [p for p in self.github.pulls if p.number != number] + [pr]

    def review_fails(self, attempt, number, head, *findings):
        """Its PR is open on ``head`` and the review failed it."""
        self.open_pr(attempt, number, head)
        self.tick(minutes=6)
        self.failures.by_attempt[attempt] = FailureReport(
            attempt, number, head, findings or (finding(1),), f"rv-{attempt}"
        )

    def session(self, attempt):
        return next(
            s.event.data["session_url"]
            for s in self.store.events(attempt.task)
            if s.event.kind == ev.FIRE_RESULT
            and s.event.attempt == attempt
            and s.event.data.get("session_url")
        )

    def clear(self, attempt):
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").record_clearing(
            attempt, ev.ClearingBasis.COMPLETED, self.session(attempt), self.now
        )

    def repair_records(self):
        return [
            s.event
            for s in self.store.events(TaskId("eng-187"))
            if s.event.kind == ev.SOURCE_REPAIR_AUTHORIZED
        ]

    def run_rounds(self, n=4, minutes=6):
        for _ in range(n):
            self.tick(minutes=minutes)

    def count(self, words):
        return len([t for t in self.texts() if words in t])


class RepairServiceTests(RepairServiceCase):
    def test_one_routine_repair_end_to_end(self):
        eid = self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.tick(minutes=6)
        # It needs the earlier worker cleared first, and says exactly how.
        asks = [t for t in self.texts() if "first it needs to know" in t]
        self.assertEqual(len(asks), 1)
        rendered = asks[0].replace("\\_", "_")  # Linear shows the escaped text plainly
        self.assertIn(f'/app/factory clear eng-187 {self.session(self.a1)}"', rendered)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertEqual(self.launched(), 1)
        self.clear(self.a1)
        r = self.tick(minutes=6)
        self.assertEqual(r.errors, [])
        self.assertEqual(r.fired, ["eng-187-a2-f1"])
        self.assertEqual(self.authorizer.repair_calls, [(self.a2, SHA_A)])
        (record,) = self.repair_records()
        self.assertEqual(record.attempt, self.a2)
        self.assertEqual(self.count("starting repair attempt 2"), 1)
        # The worker gets what failed, exactly as signed.
        envelope = json.loads(self.adapter.requests[1].text)
        self.assertEqual(envelope["attempt"], 2)
        self.assertEqual(envelope["repair"]["previous_head"], SHA_A)
        self.assertEqual(envelope["repair"]["findings"], rf.as_data([finding(1)]))
        self.assertEqual(envelope["contract_digest"], contracts.digest(self.contract()).value)
        # Nothing more starts, however many rounds pass.
        self.run_rounds(6)
        self.assertEqual(self.launched(), 2)
        self.assertIsNone(self.item(eid).closed)
        self.assertEqual(self.count("asks"), 0)

    def test_repaired_pr_is_reviewed_and_merging_it_closes_the_item(self):
        eid = self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)
        self.open_pr(self.a2, 8, SHA_B)
        self.run_rounds(2)
        self.assertIn(8, [p.number for p in self.reviewer.started])
        merged = replace(
            next(p for p in self.github.pulls if p.number == 8),
            state="closed",
            merged=True,
            merge_commit=SHA_C,
        )
        self.github.pulls = [p for p in self.github.pulls if p.number != 8] + [merged]
        self.tick(minutes=6)
        self.assertEqual(self.item(eid).closed, "merged")
        self.assertEqual(self.launched(), 2)

    def test_allowance_used_up_stops_with_one_decision_request(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.tick(minutes=6)
        self.review_fails(self.a2, 8, SHA_B, finding(3))
        self.clear(self.a2)
        self.run_rounds(4)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(self.count("used its 1 automatic repair"), 1)
        self.assertEqual(len(self.repair_records()), 1)

    def test_typed_repair_still_works_after_the_allowance(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.tick(minutes=6)
        self.review_fails(self.a2, 8, SHA_B, finding(3))
        self.clear(self.a2)
        self.tick(minutes=6)
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").authorize_repair(
            self.contract(), 3, "AC3 still fails", self.now
        )
        r = self.tick(minutes=6)
        self.assertEqual(r.fired, ["eng-187-a3-f1"])
        self.run_rounds(3)
        self.assertEqual(self.launched(), 3)
        # A repair Rolando allowed by hand fires with the contract alone.
        self.assertNotIn("repair", json.loads(self.adapter.requests[2].text))

    def test_rolandos_findings_are_never_repaired_automatically(self):
        self.start()
        self.review_fails(
            self.a1,
            7,
            SHA_A,
            finding(1),
            finding(2, category="flag-weakened-test", route="rolando"),
        )
        self.clear(self.a1)
        self.run_rounds(4)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertEqual(self.count("only you can settle"), 1)

    def test_a_request_to_weaken_a_test_is_never_repaired(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A, finding(1, suggested_action="Delete the flaky test."))
        self.clear(self.a1)
        self.run_rounds(3)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(self.count("never a routine fix"), 1)

    def test_stale_commit_asks_nothing(self):
        """The verdict is for SHA_A but the PR has moved on to SHA_B."""
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.open_pr(self.a1, 7, SHA_B)
        self.run_rounds(3)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertEqual(self.launched(), 1)

    def test_push_between_the_last_read_and_the_go_ahead_is_caught(self):
        """The service reads GitHub again right before asking the signer."""
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        # GitHub moves on, but the service's last read (the ledger) is still SHA_A.
        self.open_pr(self.a1, 7, SHA_B)
        self.service._last_reconcile[self.a1] = self.now + timedelta(minutes=6)
        self.tick(minutes=6)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertEqual(self.launched(), 1)

    def test_ticket_leaving_todo_while_the_worker_runs_stops_repairs(self):
        eid = self.start()
        self.linear.move("ENG-187", BACKLOG, self.now + timedelta(minutes=1))
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.run_rounds(4)
        self.assertIsNotNone(self.item(eid).withdrawn)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertEqual(self.launched(), 1)

    def test_ticket_leaving_todo_after_the_go_ahead_starts_nothing(self):
        """Signed, then the ticket leaves Todo before the fire: dispatch's own
        standing check catches it and the go-ahead goes unused."""
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        inner = self.service._try_dispatch

        def leave_then_dispatch(item, r, *, may_fire):
            self.linear.move("ENG-187", BACKLOG, self.now)
            return inner(item, r, may_fire=may_fire)

        self.service._try_dispatch = leave_then_dispatch
        self.tick(minutes=6)
        self.service._try_dispatch = inner
        self.run_rounds(3)
        self.assertEqual(len(self.repair_records()), 1)
        self.assertEqual(self.launched(), 1)

    def test_signer_refusals(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)

        def passing():
            raise AuthorizationRefused("Linear could not be read", final=False)

        self.authorizer.repair_answer = passing
        self.run_rounds(3)
        self.assertEqual(self.count("Linear could not be read"), 1)
        self.assertEqual(len(self.authorizer.repair_calls), 3)
        self.assertEqual(self.launched(), 1)
        self.authorizer.repair_answer = None
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)

    def test_final_signer_refusal_is_reported_once(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)

        def final():
            raise AuthorizationRefused("the move was made by an app", final=True)

        self.authorizer.repair_answer = final
        self.run_rounds(3)
        self.assertEqual(self.count("made by an app"), 1)
        self.assertEqual(self.launched(), 1)

    def test_signer_answer_for_another_repair_is_an_error(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        good = self.authorizer.inner.use_repair_allowance(
            self.item_event_id(),
            "iss-eng-187",
            self.contract(),
            self.item_revision(),
            2,
            7,
            SHA_A,
            "x",
            rf.as_data([finding(1)]),
        )
        for bad in (
            resign(good, prior_head=SHA_B),
            resign(good, attempt=self.a3),
            resign(good, event_id="hist-9999"),
            resign(good, kind=ev.REPAIR_AUTHORIZED),
        ):
            with self.subTest(bad=bad.data.get("prior_head")):
                self.authorizer.repair_answer = lambda bad=bad: bad
                r = self.tick(minutes=6)
                self.assertTrue(any("different repair" in e for e in r.errors), r.errors)
        self.assertEqual(self.repair_records(), [])
        self.assertEqual(self.launched(), 1)

    def item_event_id(self):
        return next(iter(self.view().items))

    def item_revision(self):
        return next(iter(self.view().items.values())).revision

    def test_forged_record_in_the_ledger_starts_nothing(self):
        """Anything that can append to the ledger but not sign can't start a repair."""
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.authorizer.repair_answer = lambda: (_ for _ in ()).throw(
            AuthorizationRefused("no", final=True)
        )
        forged = resign(
            self.authorizer.inner.use_repair_allowance(
                self.item_event_id(),
                "iss-eng-187",
                self.contract(),
                self.item_revision(),
                2,
                7,
                SHA_A,
                "x",
                rf.as_data([finding(1)]),
            ),
            key=OTHER_KEY,
        )
        with self.store.writer_lock():
            self.store.append(forged)
        self.run_rounds(3)
        self.assertEqual(self.launched(), 1)

    def test_pause_holds_the_repair_until_resume(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.gate.hold("paused", "pausing", self.now)
        self.run_rounds(3)
        self.assertEqual(self.launched(), 1)
        # The go-ahead lapses while paused; resuming asks for it again.
        self.run_rounds(6)
        self.gate.resume("go", self.now, reason="paused")
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)
        self.run_rounds(3)
        self.assertEqual(self.launched(), 2)
        self.assertGreaterEqual(len(self.repair_records()), 2)
        self.assertEqual({e.attempt for e in self.repair_records()}, {self.a2})

    def test_restart_after_signing_before_recording(self):
        """The service dies after the signer answered but before the record
        was saved: a new service asks again and starts one repair."""
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        real = self.service._append

        def die_on_repair(*events):
            if any(e.kind == ev.SOURCE_REPAIR_AUTHORIZED for e in events):
                raise SystemExit("killed")
            return real(*events)

        self.service._append = die_on_repair
        with self.assertRaises(SystemExit):
            self.tick(minutes=6)
        self.restart()
        self.run_rounds(4)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(len(self.authorizer.repair_calls), 2)

    def test_restart_after_recording_before_firing(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        real = self.service._try_dispatch

        def die(*a, **kw):
            raise SystemExit("killed")

        self.service._try_dispatch = die
        with self.assertRaises(SystemExit):
            self.tick(minutes=6)
        self.assertEqual(len(self.repair_records()), 1)
        self.service._try_dispatch = real
        self.restart()
        self.run_rounds(4)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(len(self.authorizer.repair_calls), 1)

    def test_crash_mid_launch_of_the_repair_is_never_relaunched(self):
        from controller.adapter.fake import FakeRuntimeAdapter, FakeStep

        self.adapter = FakeRuntimeAdapter([FakeStep.launch(), FakeStep.lost_response()] * 3)
        self.restart()
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)
        self.run_rounds(6)
        self.assertEqual(self.launched(), 2)
        self.assertEqual(self.count("unclear whether worker eng-187-a2"), 1)

    def test_two_services_racing_start_one_repair(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        first = self.service
        self.restart()
        second = self.service
        self.now += timedelta(minutes=6)
        first.tick()
        second.tick()
        first.tick()
        self.now += timedelta(minutes=6)
        second.tick()
        self.assertEqual(self.launched(), 2)
        fires = [s.event.run for s in self.store.events() if s.event.kind == ev.FIRE_INTENT]
        self.assertEqual(len(fires), 2)

    def test_no_signer_connected_asks_rolando(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.service._x = replace(self.service._x, authorizer=_AuthorizeOnly(self.authorizer))
        self.run_rounds(3)
        self.assertEqual(self.count("need the signer"), 1)
        self.assertEqual(self.launched(), 1)

    def test_failure_source_errors_wait_for_the_next_round(self):
        self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.failures.fail_next = OSError("GitHub is down")
        r = self.tick(minutes=6)
        self.assertEqual(r.errors, [])
        self.assertEqual(self.launched(), 1)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)

    def test_service_never_signs_repairs(self):
        text = (Path(__file__).parents[1] / "controller" / "service" / "service.py").read_text()
        self.assertNotIn("sign_source_repair", text)


class _AuthorizeOnly:
    def __init__(self, inner):
        self._inner = inner

    def authorize(self, authorization, contract):
        return self._inner.authorize(authorization, contract)


class NoAllowanceServiceTests(RepairServiceCase):
    allowance = 0

    def test_asks_rolando_and_his_typed_repair_still_works(self):
        eid = self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.run_rounds(3)
        self.assertEqual(self.launched(), 1)
        self.assertEqual(self.count("come with no automatic repairs"), 1)
        self.assertEqual(self.authorizer.repair_calls, [])
        self.assertIsNone(self.item(eid).closed)
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").authorize_repair(
            self.contract(), 2, "AC1 failed", self.now
        )
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)


class CappedServiceTests(RepairServiceCase):
    budget = 2

    def test_at_the_cap_one_report_and_nothing_starts(self):
        eid = self.start()
        self.review_fails(self.a1, 7, SHA_A)
        self.clear(self.a1)
        self.tick(minutes=6)
        self.assertEqual(self.launched(), 2)
        self.review_fails(self.a2, 8, SHA_B, finding(3))
        self.clear(self.a2)
        self.run_rounds(4)
        self.assertEqual(self.launched(), 2)
        reports = [t for t in self.texts() if "used all 2 of its attempts" in t]
        self.assertEqual(len(reports), 1)
        self.assertIn("AC3 failed", reports[0])
        self.assertIn("new ticket", reports[0])
        self.assertIsNone(self.item(eid).closed)  # still watched, so a merge is recorded


if __name__ == "__main__":
    unittest.main()
