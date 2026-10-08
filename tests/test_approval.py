"""Approval bound to the exact dispatched contract (ENG-151).

``ApprovalTests`` runs on the in-memory ledger from tests/test_attempts.py.
``SqliteApprovalTests`` reruns every test on the durable ledger (ENG-147) when
it is present, restarts included, and checks every record passes its rules.
"""

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval import (
    APPROVER,
    DEFAULT_TTL,
    HUMAN_DECISION,
    MAX_TTL,
    SCOPE,
    ApprovalRefused,
    Approvals,
    ContractStore,
    KeychainKey,
    StaticKey,
    authentic,
    tty_confirm,
)
from controller.approval import approval as approval_module
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.interfaces import AttemptId, LedgerEvent, RunId, TaskId
from tests.test_attempts import LOST, MemoryLedger, launched, not_launched

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"k" * 32)
OTHER_KEY = StaticKey(b"o" * 32)


def example(**changes):
    c = json.loads((ROOT / "schema/examples/filter-by-status.json").read_text())
    c.update(changes)
    return c


def yes(summary, code):
    return True


class Typed:
    """A confirm that records what it was shown and types a given answer."""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def __call__(self, summary, code):
        self.calls.append((summary, code))
        return (self.answer if self.answer is not None else code) == code


class ApprovalTests(unittest.TestCase):
    def make_store(self):
        return MemoryLedger()

    def restart(self, store):
        return store

    def setUp(self):
        self.store = self.make_store()
        self.desk = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        self.gate = AttemptGate(self.store)
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))
        self.contract = example()
        self.task = TaskId(self.contract["task_id"])
        self.digest = contracts.digest(self.contract)

    # --- helpers ---

    def append(self, *events):
        with self.store.writer_lock():
            return self.store.append(*events)

    def codes(self, verdict):
        return [b.code for b in verdict.blocks]

    def assert_refused(self, verdict, code):
        self.assertFalse(verdict.approved)
        self.assertIn(code, self.codes(verdict))

    def dispatch(self, contract=None, result=None, refire_of=None, at=NOW):
        """What dispatch will do: check approval, then reserve, then record."""
        contract = contract or self.contract
        verdict = self.desk.check(contract, at, refire_of=refire_of)
        self.assertTrue(verdict.approved, verdict.blocks)
        run = self.gate.reserve(
            TaskId(contract["task_id"]), contracts.digest(contract), at, refire_of=refire_of
        )
        self.assertEqual(run, verdict.run)
        self.gate.record_launch(run, result or launched(), at)
        return run

    def finish(self, attempt, at=NOW):
        self.desk.record_clearing(attempt, ev.ClearingBasis.COMPLETED, "https://claude.ai/x", at)

    def resign(self, event, key=KEY, **changes):
        """Re-sign an event with changes, as if someone held the key."""
        signature = ("mac", "key_id", "decision_id", "binding_sha256")
        fields = {k: v for k, v in event.data.items() if k not in signature}
        base = LedgerEvent(
            changes.pop("kind", event.kind),
            event.at,
            changes.pop("task", event.task),
            changes.pop("attempt", event.attempt),
            changes.pop("run", event.run),
            fields | changes,
        )
        return approval_module._sign(base, key)

    def forge(self, event, **changes):
        """Copy an event with changes but keep its old signature."""
        data = dict(event.data) | changes.pop("data", {})
        return LedgerEvent(
            event.kind,
            event.at,
            changes.get("task", event.task),
            changes.get("attempt", event.attempt),
            changes.get("run", event.run),
            data,
        )

    # --- AC1: what an approval stores ---

    def test_approval_stores_identity_time_digest_version_repo_base_scope_budget(self):
        stored = self.desk.approve(self.contract, NOW)
        e = stored.event
        d = e.data
        self.assertEqual(e.kind, HUMAN_DECISION)
        self.assertEqual(e.task, self.task)
        self.assertEqual(d["identity"], APPROVER)
        self.assertEqual(d["os_user"], "rolando")
        self.assertIn(KEY.key_id, d["authenticated_by"])
        self.assertEqual(d["decided_at"], NOW.isoformat())
        self.assertEqual(d["expires_at"], (NOW + DEFAULT_TTL).isoformat())
        self.assertEqual(d["digest"], self.digest.value)
        self.assertEqual(d["digest_type"], "contract")
        self.assertEqual(d["scope"], SCOPE)
        self.assertEqual(d["decision"], "approved")
        b = d["binding"]
        self.assertEqual(b["digest"], self.digest.value)
        self.assertEqual(b["version"], self.contract["version"])
        self.assertEqual(b["task_id"], self.contract["task_id"])
        self.assertEqual(b["repository"], self.contract["repository"])
        self.assertEqual(b["base_commit"], self.contract["base_commit"])
        self.assertEqual(list(b["permitted_paths"]), self.contract["permitted_paths"])
        self.assertEqual(list(b["permitted_actions"]), self.contract["permitted_actions"])
        self.assertEqual(b["attempt_budget"], self.contract["attempt_budget"])
        self.assertTrue(authentic(e, KEY, frozenset({APPROVER})))
        self.assertEqual(self.desk.check(self.contract, NOW).run, RunId(AttemptId(self.task, 1), 1))

    def test_record_survives_a_restart_and_still_verifies(self):
        self.desk.approve(self.contract, NOW)
        self.store = self.restart(self.store)
        desk = Approvals(self.store, KEY, confirm=yes)
        self.assertTrue(desk.check(self.contract, NOW).approved)

    def test_mutating_the_contract_after_approval_changes_nothing_stored(self):
        stored = self.desk.approve(self.contract, NOW)
        self.contract["permitted_paths"].append("src/extra.ts")
        self.assertNotIn("src/extra.ts", stored.event.data["binding"]["permitted_paths"])
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-for-different-contract")

    # --- AC2: missing, rejected, expired, revoked ---

    def test_missing_approval_is_refused(self):
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-missing")

    def test_rejected_contract_is_refused(self):
        self.desk.approve(self.contract, NOW)
        self.desk.reject(self.contract, "criteria too loose", NOW + timedelta(minutes=1))
        self.assert_refused(
            self.desk.check(self.contract, NOW + timedelta(minutes=2)), "approval-rejected"
        )

    def test_rejection_without_any_approval_is_refused(self):
        self.desk.reject(self.contract, "no", NOW)
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-rejected")

    def test_expired_approval_is_refused(self):
        self.desk.approve(self.contract, NOW, ttl=timedelta(hours=1))
        self.assertTrue(self.desk.check(self.contract, NOW + timedelta(minutes=59)).approved)
        self.assert_refused(
            self.desk.check(self.contract, NOW + timedelta(hours=1)), "approval-expired"
        )

    def test_revoked_approval_is_refused(self):
        self.desk.approve(self.contract, NOW)
        self.desk.revoke(self.task, self.digest, "changed my mind", NOW + timedelta(minutes=1))
        self.assert_refused(
            self.desk.check(self.contract, NOW + timedelta(minutes=2)), "approval-revoked"
        )

    def test_revocation_is_seen_by_a_check_that_was_already_set_up(self):
        self.assertTrue(self.desk.approve(self.contract, NOW))
        self.assertTrue(self.desk.check(self.contract, NOW).approved)
        other = Approvals(self.store, KEY, confirm=yes)
        other.revoke(self.task, self.digest, "stop", NOW)
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-revoked")

    def test_an_unsigned_revocation_still_stops_dispatch(self):
        self.desk.approve(self.contract, NOW)
        self.append(
            LedgerEvent(
                HUMAN_DECISION,
                NOW,
                self.task,
                data=self.desk._common(self.digest, NOW)
                | {
                    "scope": SCOPE,
                    "decision": "revoked",
                    "digest_type": "contract",
                    "expires_at": None,
                },
            )
        )
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-revoked")

    def test_a_fresh_approval_after_revocation_counts(self):
        self.desk.approve(self.contract, NOW)
        self.desk.revoke(self.task, self.digest, "wait", NOW)
        self.desk.approve(self.contract, NOW + timedelta(minutes=1))
        self.assertTrue(self.desk.check(self.contract, NOW + timedelta(minutes=1)).approved)

    def test_approval_dated_in_the_future_is_refused(self):
        self.desk.approve(self.contract, NOW + timedelta(hours=1))
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-not-yet-valid")

    def test_approval_lifetime_limits(self):
        for ttl in (timedelta(0), timedelta(seconds=-1), MAX_TTL + timedelta(seconds=1)):
            with self.subTest(ttl=ttl), self.assertRaises(ApprovalRefused):
                self.desk.approve(self.contract, NOW, ttl=ttl)
        self.desk.approve(self.contract, NOW, ttl=MAX_TTL)
        self.assertTrue(
            self.desk.check(self.contract, NOW + MAX_TTL - timedelta(seconds=1)).approved
        )

    def test_expiry_past_the_calendar_end_is_refused_not_crashing(self):
        late = datetime(9999, 12, 31, 23, 0, tzinfo=UTC)
        with self.assertRaises(ApprovalRefused):
            self.desk.approve(self.contract, late, ttl=timedelta(days=1))
        self.assertEqual(self.store.events(self.task), [])

    def test_a_signed_record_with_a_stretched_lifetime_is_refused(self):
        e = self.desk.approve(self.contract, NOW).event
        stretched = (NOW + MAX_TTL + timedelta(days=1)).isoformat()
        self.append(self.resign(e, expires_at=stretched))
        self.store = self.restart(self.store)
        desk = Approvals(self.store, KEY, confirm=yes)
        late = NOW + MAX_TTL + timedelta(hours=1)
        self.assert_refused(desk.check(self.contract, late), "approval-expired")

    def test_naive_times_are_refused(self):
        naive = datetime(2026, 10, 8, 12, 0)
        with self.assertRaises(ValueError):
            self.desk.approve(self.contract, naive)
        with self.assertRaises(ValueError):
            self.desk.check(self.contract, naive)

    def test_contract_needing_clarification_cannot_be_approved_or_dispatched(self):
        unclear = example()
        unclear["acceptance_criteria"][0]["status"] = "needs-clarification"
        unclear["acceptance_criteria"][0]["clarification"] = "which format?"
        with self.assertRaises(ApprovalRefused):
            self.desk.approve(unclear, NOW)
        self.assert_refused(self.desk.check(unclear, NOW), "contract-invalid")
        self.assert_refused(self.desk.check({"not": "a contract"}, NOW), "contract-invalid")

    # --- AC3: any change needs a fresh approval ---

    def test_changed_content_with_the_same_version_label_is_refused(self):
        self.desk.approve(self.contract, NOW)
        changes = {
            "content": {"goal": self.contract["goal"] + " Also add CSV."},
            "base commit": {"base_commit": "f" * 40},
            "paths": {"permitted_paths": [*self.contract["permitted_paths"], "src/extra.ts"]},
            "actions": {"permitted_actions": [*self.contract["permitted_actions"], "add-files"]},
            "budget": {"attempt_budget": 2},
            "repository": {"repository": "rNavarrete/software-factory"},
        }
        for name, change in changes.items():
            with self.subTest(name):
                changed = example(**change)
                self.assertEqual(changed["version"], self.contract["version"])
                self.assert_refused(
                    self.desk.check(changed, NOW), "approval-for-different-contract"
                )

    def test_a_fresh_approval_of_the_changed_contract_counts(self):
        self.desk.approve(self.contract, NOW)
        changed = example(base_commit="f" * 40)
        self.desk.approve(changed, NOW)
        self.assertTrue(self.desk.check(changed, NOW).approved)

    def test_key_order_and_layout_do_not_matter(self):
        self.desk.approve(self.contract, NOW)
        reordered = contracts.loads(
            json.dumps(dict(reversed(list(self.contract.items()))), indent=2)
        )
        self.assertTrue(self.desk.check(reordered, NOW).approved)

    # --- AC4: nothing but Rolando's signed, confirmed action makes an approval ---

    def test_unsigned_approval_record_is_refused(self):
        e = self.desk._common(self.digest, NOW) | {
            "scope": SCOPE,
            "decision": "approved",
            "digest_type": "contract",
            "expires_at": (NOW + DEFAULT_TTL).isoformat(),
        }
        self.append(LedgerEvent(HUMAN_DECISION, NOW, self.task, data=e))
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-unauthenticated")

    def test_approval_signed_with_another_key_is_refused(self):
        other = Approvals(self.make_store(), OTHER_KEY, confirm=yes)
        e = other._append(
            LedgerEvent(
                HUMAN_DECISION,
                NOW,
                self.task,
                data=other._common(self.digest, NOW)
                | {
                    "scope": SCOPE,
                    "decision": "approved",
                    "digest_type": "contract",
                    "expires_at": (NOW + DEFAULT_TTL).isoformat(),
                },
            )
        ).event
        self.append(e)
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-unauthenticated")

    def test_tampered_or_moved_approvals_are_refused(self):
        e = self.desk.approve(example(goal="Something else entirely."), NOW).event
        tampered = {
            "digest swapped": self.forge(e, data={"digest": self.digest.value}),
            "expiry pushed": self.forge(
                e, data={"expires_at": (NOW + timedelta(days=9)).isoformat()}
            ),
            "identity changed": self.forge(e, data={"identity": "rnavarrete-factory-bot"}),
            "mac dropped": self.forge(e, data={"mac": None}),
        }
        for name, bad in tampered.items():
            with self.subTest(name):
                self.assertFalse(authentic(bad, KEY, frozenset({APPROVER})))
        store = self.make_store()
        with store.writer_lock():
            store.append(tampered["digest swapped"])
        desk = Approvals(store, KEY, confirm=yes)
        self.assert_refused(desk.check(self.contract, NOW), "approval-unauthenticated")

    def test_approval_moved_to_another_task_is_refused(self):
        e = self.desk.approve(self.contract, NOW).event
        other = example(task_id="other-task")
        moved = self.forge(
            e, task=TaskId("other-task"), data={"digest": contracts.digest(other).value}
        )
        self.append(moved)
        self.assert_refused(self.desk.check(other, NOW), "approval-unauthenticated")

    def test_a_signed_record_naming_someone_else_is_refused(self):
        e = self.desk.approve(self.contract, NOW).event
        store = self.make_store()
        with store.writer_lock():
            store.append(self.resign(e, identity="rnavarrete-factory-bot"))
        desk = Approvals(store, KEY, confirm=yes)
        self.assert_refused(desk.check(self.contract, NOW), "approval-unauthenticated")

    def test_only_approvers_can_be_the_identity(self):
        with self.assertRaises(ValueError):
            Approvals(self.store, KEY, confirm=yes, identity="rnavarrete-factory-bot")

    def test_nothing_is_written_unless_confirmed(self):
        no = Typed(answer="wrong")
        desk = Approvals(self.store, KEY, confirm=no)
        before = len(self.store.events())
        with self.assertRaises(ApprovalRefused):
            desk.approve(self.contract, NOW)
        with self.assertRaises(ApprovalRefused):
            desk.revoke(self.task, self.digest, "x", NOW)
        self.assertEqual(len(self.store.events()), before)
        self.assertEqual(no.calls[0][1], self.digest.short)
        self.assertIn(self.digest.value, no.calls[0][0])
        self.assertIn("not the PR, a merge or a release", no.calls[0][0])

    def test_terminal_confirmation_refuses_piped_input(self):
        class Piped(io.StringIO):
            def isatty(self):
                return False

        class Tty(io.StringIO):
            def isatty(self):
                return True

        out = io.StringIO()
        code = self.digest.short
        self.assertFalse(tty_confirm("s", code, stdin=Piped(code + "\n"), out=out))
        self.assertFalse(tty_confirm("s", code, stdin=Tty("approved\n"), out=out))
        self.assertFalse(tty_confirm("s", code, stdin=Tty(""), out=out))
        self.assertFalse(tty_confirm("s", code, stdin=object(), out=out))
        self.assertTrue(tty_confirm("s", code, stdin=Tty(f"  {code}\n"), out=out))

    def test_the_approval_package_reads_no_network_or_pr_text(self):
        source = "\n".join(p.read_text() for p in (ROOT / "controller/approval").glob("*.py"))
        for banned in ("socket", "http", "urllib", "ssl", "from_pr_body", "from_pr_title"):
            with self.subTest(banned):
                self.assertIsNone(re.search(rf"\b{banned}\b", source))

    def test_plain_gate_decisions_do_not_count(self):
        """Repair, re-fire and clearing records without a signature are ignored."""
        self.desk.approve(self.contract, NOW)
        first = self.dispatch()
        self.append(
            ev.attempt_cleared(first.attempt, ev.ClearingBasis.COMPLETED, "https://x", "R", NOW),
            ev.repair_authorized(AttemptId(self.task, 2), "CI red", APPROVER, NOW),
        )
        # The gate alone would let attempt 2 go; the approval check does not.
        self.assertTrue(self.gate.decide(self.task, self.digest, NOW).allowed)
        verdict = self.desk.check(self.contract, NOW)
        self.assert_refused(verdict, "unresolved-attempt")
        self.assert_refused(verdict, "repair-not-authorized")

    def test_plain_refire_record_does_not_count(self):
        self.desk.approve(self.contract, NOW)
        first = self.dispatch(result=not_launched(400))
        self.append(ev.refire_authorized(first, APPROVER, NOW))
        self.assertTrue(self.gate.decide(self.task, self.digest, NOW, refire_of=first).allowed)
        self.assert_refused(
            self.desk.check(self.contract, NOW, refire_of=first), "refire-not-authorized"
        )

    # --- AC5: replays ---

    def test_approval_alone_never_authorizes_a_second_attempt(self):
        stored = self.desk.approve(self.contract, NOW)
        first = self.dispatch()
        self.finish(first.attempt)
        self.append(stored.event)  # the same approval, replayed
        self.desk.approve(self.contract, NOW)  # even a fresh one
        self.assert_refused(self.desk.check(self.contract, NOW), "repair-not-authorized")

    def test_repair_grant_cannot_be_replayed_onto_another_attempt_or_contract(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        grant = self.desk.authorize_repair(self.contract, 2, "CI red on a1", NOW).event
        a2 = self.dispatch()
        self.assertEqual(a2.attempt.number, 2)
        self.finish(a2.attempt)
        self.append(grant)  # replayed as is: names attempt 2, already used
        self.append(self.forge(grant, attempt=AttemptId(self.task, 3)))
        self.assert_refused(self.desk.check(self.contract, NOW), "repair-not-authorized")

    def test_repair_grant_for_one_contract_does_not_authorize_another(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        self.desk.authorize_repair(self.contract, 2, "CI red", NOW)
        changed = example(goal="A different goal.")
        self.desk.approve(changed, NOW)
        self.assert_refused(self.desk.check(changed, NOW), "repair-not-authorized")
        self.desk.authorize_repair(changed, 2, "CI red", NOW)
        self.assertTrue(self.desk.check(changed, NOW).approved)

    def test_repair_grant_made_before_the_attempt_it_repairs_does_not_count(self):
        self.desk.approve(self.contract, NOW)
        early = approval_module._sign(
            approval_module._extend(
                ev.repair_authorized(AttemptId(self.task, 2), "pre-approved", APPROVER, NOW),
                self.desk._common(self.digest, NOW),
            ),
            KEY,
        )
        self.append(early)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        self.assert_refused(self.desk.check(self.contract, NOW), "repair-not-authorized")

    def test_repair_cannot_be_written_for_an_attempt_that_never_ran(self):
        self.desk.approve(self.contract, NOW)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_repair(self.contract, 2, "pre-approval", NOW)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_repair(self.contract, 1, "x", NOW)

    def test_a_second_refire_of_the_same_attempt_is_refused(self):
        self.desk.approve(self.contract, NOW)
        r1 = self.dispatch(result=not_launched(429, retry_after=0))
        self.desk.authorize_refire(self.contract, r1, NOW)
        r2 = self.dispatch(refire_of=r1, result=not_launched(400))
        self.assertEqual(r2, RunId(r1.attempt, 2))
        # The grant for f1 says nothing about f2; the fire cap is spent too.
        self.assert_refused(self.desk.check(self.contract, NOW, refire_of=r2), "refire-not-allowed")

    def test_refire_grant_made_before_the_result_does_not_count(self):
        self.desk.approve(self.contract, NOW)
        run = self.gate.reserve(self.task, self.digest, NOW)
        early = approval_module._sign(
            approval_module._extend(
                ev.refire_authorized(run, APPROVER, NOW), self.desk._common(self.digest, NOW)
            ),
            KEY,
        )
        self.append(early)
        self.gate.record_launch(run, not_launched(400), NOW)
        self.assert_refused(
            self.desk.check(self.contract, NOW, refire_of=run), "refire-not-authorized"
        )

    def test_refire_needs_a_definite_not_launched_result(self):
        self.desk.approve(self.contract, NOW)
        run = self.dispatch(result=LOST)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_refire(self.contract, run, NOW)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_refire(example(goal="Other."), run, NOW)

    def test_refire_still_needs_a_standing_approval(self):
        self.desk.approve(self.contract, NOW)
        r1 = self.dispatch(result=not_launched(400))
        self.desk.authorize_refire(self.contract, r1, NOW)
        self.desk.revoke(self.task, self.digest, "stop", NOW)
        self.assert_refused(self.desk.check(self.contract, NOW, refire_of=r1), "approval-revoked")

    def test_clearing_must_be_signed_and_for_the_attempt_s_contract(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        cleared = self.desk.record_clearing(
            a1.attempt, ev.ClearingBasis.TERMINATED, "https://claude.ai/s", NOW
        ).event
        self.assertEqual(cleared.data["digest"], self.digest.value)
        self.desk.authorize_repair(self.contract, 2, "CI red", NOW)
        self.assertTrue(self.desk.check(self.contract, NOW).approved)
        # A clearing re-signed for a different contract does not count.
        store = self.make_store()
        with store.writer_lock():
            store.append(
                *[s.event for s in self.store.events() if s.event.kind != ev.ATTEMPT_CLEARED]
            )
            store.append(self.resign(cleared, digest="0" * 64))
        desk = Approvals(store, KEY, confirm=yes)
        self.assert_refused(desk.check(self.contract, NOW), "unresolved-attempt")

    def test_clearing_needs_a_started_attempt_and_evidence(self):
        with self.assertRaises(ApprovalRefused):
            self.finish(AttemptId(self.task, 1))
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        with self.assertRaises(ApprovalRefused):
            self.desk.record_clearing(a1.attempt, ev.ClearingBasis.COMPLETED, "  ", NOW)

    def test_an_old_approval_replayed_after_a_revocation_or_rejection_does_not_count(self):
        withdrawals = {
            "approval-revoked": lambda at: self.desk.revoke(self.task, self.digest, "stop", at),
            "approval-rejected": lambda at: self.desk.reject(self.contract, "no", at),
        }
        for code, withdraw in withdrawals.items():
            with self.subTest(code):
                self.setUp()
                stored = self.desk.approve(self.contract, NOW)
                withdraw(NOW + timedelta(minutes=1))
                self.append(stored.event)  # byte-identical copy, appended later
                self.assert_refused(
                    self.desk.check(self.contract, NOW + timedelta(minutes=2)), code
                )

    def test_a_withdrawal_cancels_approvals_decided_before_it_whatever_the_order(self):
        e = self.desk.approve(self.contract, NOW).event
        self.desk.revoke(self.task, self.digest, "stop", NOW + timedelta(minutes=5))
        # A record signed with the key but dated before the revocation and
        # written after it (say, a delayed or replayed write) is still cancelled.
        self.append(self.resign(e))
        self.assert_refused(
            self.desk.check(self.contract, NOW + timedelta(minutes=6)), "approval-revoked"
        )

    def test_an_unsigned_withdrawal_cannot_cancel_later_approvals(self):
        far = (NOW + timedelta(days=365)).isoformat()
        self.append(
            LedgerEvent(
                HUMAN_DECISION,
                NOW,
                self.task,
                data=self.desk._common(self.digest, NOW)
                | {
                    "scope": SCOPE,
                    "decision": "revoked",
                    "digest_type": "contract",
                    "decided_at": far,
                },
            )
        )
        self.assert_refused(self.desk.check(self.contract, NOW), "approval-revoked")
        self.desk.approve(self.contract, NOW + timedelta(minutes=1))
        self.assertTrue(self.desk.check(self.contract, NOW + timedelta(minutes=1)).approved)

    def test_approving_a_revised_contract_retires_the_old_version(self):
        self.desk.approve(self.contract, NOW)
        narrow = example(permitted_paths=["src/books.ts", "tests/books.test.ts"])
        self.desk.approve(narrow, NOW + timedelta(minutes=1))
        later = NOW + timedelta(minutes=2)
        self.assertTrue(self.desk.check(narrow, later).approved)
        self.assert_refused(
            self.desk.check(self.contract, later), "approval-for-different-contract"
        )

    def test_an_old_clearing_cannot_clear_a_later_fire(self):
        self.desk.approve(self.contract, NOW)
        r1 = self.gate.reserve(self.task, self.digest, NOW)
        # Cleared before the launch result came back.
        early = self.desk.record_clearing(
            r1.attempt, ev.ClearingBasis.TERMINATED, "https://claude.ai/s1", NOW
        ).event
        self.assertEqual(early.run, r1)
        self.gate.record_launch(r1, not_launched(400), NOW)
        self.desk.authorize_refire(self.contract, r1, NOW)
        self.dispatch(refire_of=r1, result=LOST)
        self.append(early)  # replayed
        self.append(self.resign(early))  # even re-signed, it names f1, not f2
        self.assert_refused(self.desk.check(self.contract, NOW), "unresolved-attempt")

    def test_clearing_evidence_must_fit_its_basis(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        with self.assertRaises(ApprovalRefused):
            self.desk.record_clearing(a1.attempt, ev.ClearingBasis.COMPLETED, "it finished", NOW)

    def test_the_bound_fields_in_an_approval_are_signed(self):
        e = self.desk.approve(self.contract, NOW).event
        binding = dict(e.data["binding"]) | {"base_commit": "f" * 40}
        self.assertFalse(authentic(self.forge(e, data={"binding": binding}), KEY, {APPROVER}))
        binding = dict(e.data["binding"]) | {"digest": "0" * 64}
        resigned = self.resign(e, binding=binding)
        self.assertFalse(authentic(resigned, KEY, frozenset({APPROVER})))

    def test_malformed_records_are_refused_not_crashing(self):
        e = self.desk.approve(self.contract, NOW).event
        bad = [
            self.forge(e, data={"os_user": "\ud800"}),
            self.forge(e, data={"identity": {"x": "y"}}),
            self.forge(e, data={"identity": ["rNavarrete"]}),
            self.forge(e, data={"decided_at": 5}),
            self.forge(e, data={"key_id": None}),
            self.forge(e, data={"binding": "not a mapping"}),
            self.forge(e, data={"decision_id": ""}),
        ]
        store = self.make_store()
        with store.writer_lock():
            for b in bad:
                try:
                    store.append(b)
                except ValueError:
                    pass  # the durable ledger refuses some of these outright
        desk = Approvals(store, KEY, confirm=yes)
        for b in bad:
            self.assertFalse(authentic(b, KEY, frozenset({APPROVER})))
        self.assert_refused(desk.check(self.contract, NOW), "approval-unauthenticated")

    def test_records_signed_with_a_retired_key_still_verify(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        new = Approvals(self.store, OTHER_KEY, confirm=yes, retired_keys=(KEY,))
        new.authorize_repair(self.contract, 2, "CI red", NOW)
        self.assertTrue(new.check(self.contract, NOW).approved)
        # Without the retired key the old approval and clearing no longer count.
        only_new = Approvals(self.store, OTHER_KEY, confirm=yes)
        verdict = only_new.check(self.contract, NOW)
        self.assert_refused(verdict, "approval-unauthenticated")
        self.assert_refused(verdict, "unresolved-attempt")

    # --- AC6: repair attempts need a new go-ahead within the total cap ---

    def test_repair_attempts_need_a_go_ahead_each_within_the_budget(self):
        contract = example(attempt_budget=3)
        self.desk.approve(contract, NOW)
        a1 = self.dispatch(contract)
        self.finish(a1.attempt)
        self.assert_refused(self.desk.check(contract, NOW), "repair-not-authorized")
        self.desk.authorize_repair(contract, 2, "CI red on a1", NOW)
        a2 = self.dispatch(contract)
        self.finish(a2.attempt)
        self.assert_refused(self.desk.check(contract, NOW), "repair-not-authorized")
        self.desk.authorize_repair(contract, 3, "still red on a2", NOW)
        a3 = self.dispatch(contract)
        self.finish(a3.attempt)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_repair(contract, 4, "again", NOW)
        self.assert_refused(self.desk.check(contract, NOW), "attempt-cap")

    def test_repair_past_the_contract_budget_is_refused(self):
        contract = example(attempt_budget=1)
        self.desk.approve(contract, NOW)
        a1 = self.dispatch(contract)
        self.finish(a1.attempt)
        with self.assertRaises(ApprovalRefused):
            self.desk.authorize_repair(contract, 2, "CI red", NOW)
        self.append(
            approval_module._sign(
                approval_module._extend(
                    ev.repair_authorized(AttemptId(self.task, 2), "CI red", APPROVER, NOW),
                    self.desk._common(contracts.digest(contract), NOW),
                ),
                KEY,
            )
        )
        self.assert_refused(self.desk.check(contract, NOW), "over-budget")

    def test_repair_while_the_last_attempt_is_unresolved_is_refused(self):
        self.desk.approve(self.contract, NOW)
        self.dispatch(result=LOST)
        self.desk.authorize_repair(self.contract, 2, "lost", NOW)
        self.assert_refused(self.desk.check(self.contract, NOW), "unresolved-attempt")

    def test_repair_records_carry_the_digest_and_how_the_person_was_verified(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        d = self.desk.authorize_repair(self.contract, 2, "CI red", NOW).event.data
        self.assertEqual(d["digest"], self.digest.value)
        self.assertEqual(d["by"], APPROVER)
        self.assertEqual(d["failure"], "CI red")
        self.assertIn("confirmed by typing the code at a terminal", d["authenticated_by"])
        self.assertEqual(d["decided_at"], NOW.isoformat())

    # --- AC7: approval is for dispatch only ---

    def test_approval_is_scoped_to_dispatch_only(self):
        e = self.desk.approve(self.contract, NOW).event
        self.assertEqual(e.data["scope"], "contract-dispatch")
        public = set(dir(Approvals))
        for word in ("release", "merge", "deploy", "approve_pr"):
            self.assertFalse([m for m in public if word in m], word)

    def test_decisions_in_other_scopes_are_never_read_as_approval(self):
        e = self.desk.approve(self.contract, NOW).event
        store = self.make_store()
        with store.writer_lock():
            store.append(self.resign(e, scope="release"))
        desk = Approvals(store, KEY, confirm=yes)
        self.assert_refused(desk.check(self.contract, NOW), "approval-missing")

    # --- the operator key ---

    def test_static_key_needs_32_bytes_and_hides_its_secret(self):
        with self.assertRaises(ValueError):
            StaticKey(b"short")
        self.assertNotIn("kkkk", repr(KEY))
        self.assertNotEqual(KEY.key_id, OTHER_KEY.key_id)

    def test_keychain_key(self):
        secret = "ab" * 32
        calls = []

        def run(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, secret + "\n", "")

        key = KeychainKey("rolando", run=run)
        self.assertEqual(key.key_id, StaticKey(bytes.fromhex(secret)).key_id)
        self.assertEqual(key.sign(b"x"), StaticKey(bytes.fromhex(secret)).sign(b"x"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["/usr/bin/security", "find-generic-password"])
        self.assertNotIn(secret, repr(key))
        for out, code in (("", 44), ("nothex" * 10, 0), ("ab" * 16, 0)):
            with self.subTest(out=out):
                bad = KeychainKey(
                    "rolando",
                    run=lambda a, out=out, code=code, **k: subprocess.CompletedProcess(
                        a, code, out, ""
                    ),
                )
                with self.assertRaises(ApprovalRefused):
                    _ = bad.key_id


class ContractStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "contracts"

    def test_save_and_load_by_digest(self):
        store = ContractStore(self.root)
        contract = example()
        digest = store.save(contract)
        self.assertEqual(digest, contracts.digest(contract))
        self.assertEqual(store.save(contract), digest)  # idempotent
        self.assertEqual(contracts.digest(store.load(digest)), digest)
        self.assertEqual(os.stat(store.path(digest)).st_mode & 0o777, 0o400)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)

    def test_tampered_file_is_refused(self):
        store = ContractStore(self.root)
        digest = store.save(example())
        path = store.path(digest)
        os.chmod(path, 0o600)
        path.write_bytes(contracts.canonical_bytes(example(goal="Changed.")))
        with self.assertRaises(ValueError):
            store.load(digest)
        with self.assertRaises(ValueError):
            store.save(example())

    def test_refuses_a_folder_inside_a_git_checkout(self):
        with self.assertRaises(ValueError):
            ContractStore(ROOT / "tmp-contracts")

    def test_approve_keeps_the_exact_contract(self):
        store = ContractStore(self.root)
        ledger = MemoryLedger()
        Approvals(ledger, KEY, confirm=yes, contracts=store).approve(example(), NOW)
        self.assertTrue(store.path(contracts.digest(example())).exists())


try:
    from controller.ledger.store import SqliteLedgerStore

    from controller.ledger import kinds as ledger_kinds
except ImportError:  # the durable ledger (ENG-147) is not merged yet
    SqliteLedgerStore = None


@unittest.skipIf(SqliteLedgerStore is None, "durable ledger not present")
class SqliteApprovalTests(ApprovalTests):
    def make_store(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = SqliteLedgerStore(Path(self._tmp.name) / "ledger.db")
        self.addCleanup(store.close)
        return store

    def restart(self, store):
        path = store.path if hasattr(store, "path") else None
        store.close()
        reopened = SqliteLedgerStore(path)
        self.addCleanup(reopened.close)
        self.gate = AttemptGate(reopened)
        return reopened

    def test_every_approval_record_passes_the_ledger_rules(self):
        self.desk.approve(self.contract, NOW)
        a1 = self.dispatch()
        self.finish(a1.attempt)
        self.desk.authorize_repair(self.contract, 2, "token=abcdefgh12345 leaked in CI log", NOW)
        self.desk.revoke(self.task, self.digest, "stop", NOW)
        for s in self.store.events():
            ledger_kinds.check(s.event)
        # Redacted free text does not break the signature.
        repair = [s.event for s in self.store.events() if s.event.kind == ev.REPAIR_AUTHORIZED]
        self.assertIn("[REDACTED]", repair[0].data["failure"])
        self.assertTrue(authentic(repair[0], KEY, frozenset({APPROVER})))


if __name__ == "__main__":
    unittest.main()
