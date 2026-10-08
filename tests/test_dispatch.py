"""Dispatch: approve if needed, then fire one attempt (ENG-176).

Integration tests through ``Dispatcher.dispatch`` on the in-memory ledger (and
the SQLite ledger for restarts), with a scripted adapter, a fake base check, a
fake start key and an injected clock. Nothing here touches the network, the
Keychain or ``gh``.

Test names start with the acceptance criterion they cover (``ac1`` .. ``ac10``).
"""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.adapter import routine
from controller.adapter.fake import FakeRuntimeAdapter, FakeStep
from controller.approval import HUMAN_DECISION, Approvals, StaticKey
from controller.attempts import AttemptGate, DispatchRefused
from controller.attempts import events as ev
from controller.attempts.policy import LedgerView, Notice
from controller.dispatch import (
    BaseUnreadable,
    Dispatcher,
    GhBaseCheck,
    Refused,
)
from controller.dispatch.base import GH_TIMEOUT_SECONDS
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchResult,
    LedgerLocked,
    RunId,
    TaskId,
)
from controller.ledger import SqliteLedgerStore
from controller.ledger import kinds as ledger_kinds
from controller.recovery import LATE_FIRE_RESULT, Recovery, State
from tests.test_approval import example
from tests.test_attempts import MemoryLedger, launched, not_launched
from tests.test_recovery import FakeGitHub

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"k" * 32)
START_KEY = "sk-ant-oat01-" + "S3cretStartKey_" * 4
BOT = "rnavarrete-factory-bot"
TRIG = "trig_01TestRoutine"
INTERRUPTED = NOW + timedelta(minutes=11)
"""Past recovery's ten minutes for a fire-intent without a result."""


class Confirm:
    """Records every prompt; types the code unless ``answer`` is False."""

    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, summary, code):
        self.calls.append((summary, code))
        return self.answer


class FakeBase:
    """A BaseCheck that answers ``answer`` (or raises it) and records calls."""

    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def on_branch(self, repo, sha, branch):
        self.calls.append((repo, sha, branch))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class ScriptedAdapter:
    """Answers each launch with the next scripted item: a LaunchResult, an
    exception to raise, or a callable taking the request. ``before`` runs at
    the start of every launch (for checks made while the send is in flight)."""

    def __init__(self, answers=(), before=None):
        self.answers = list(answers)
        self.before = before
        self.requests = []

    def launch(self, request):
        if self.before is not None:
            self.before(request)
        self.requests.append(request)
        if not self.answers:
            raise AssertionError(f"unscripted launch for {request.run}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(request)
        return answer


class DispatchCase(unittest.TestCase):
    def make_store(self):
        return MemoryLedger()

    def setUp(self):
        self.store = self.make_store()
        self.now = NOW
        self.confirm = Confirm()
        self.base = FakeBase()
        self.adapter = ScriptedAdapter([launched(1)])
        self.adapters_made = []
        self.keys_fetched = []
        self.key_side_effect = None
        self.backups = []
        self.contract = example()
        self.task = TaskId(self.contract["task_id"])
        self.digest = contracts.digest(self.contract)
        self.a1 = AttemptId(self.task, 1)
        self.build()
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))

    # --- helpers ---

    def clock(self):
        return self.now

    def start_key(self, trig_id):
        self.keys_fetched.append(trig_id)
        if self.key_side_effect is not None:
            effect, self.key_side_effect = self.key_side_effect, None
            effect()
        return START_KEY

    def make_adapter(self, trig_id, key):
        self.adapters_made.append((trig_id, key(trig_id)))
        return self.adapter

    def build(self):
        """A fresh controller on the current store, as a new process would make."""
        self.gate = AttemptGate(self.store)
        self.approvals = Approvals(self.store, KEY, confirm=self.ask, os_user="rolando")
        self.recovery = Recovery(
            self.store,
            self.approvals,
            FakeGitHub(),
            worker_logins=frozenset({BOT}),
            gate=self.gate,
            confirm=lambda summary, code: True,
        )
        self.dispatcher = Dispatcher(
            self.store,
            self.approvals,
            self.recovery,
            self.gate,
            self.base,
            routine_id=TRIG,
            adapter=self.make_adapter,
            start_key=self.start_key,
            model_config_version="test",
            now=self.clock,
            backup=self.backups.append,
            sleep=lambda seconds: None,
        )

    def ask(self, summary, code):
        return self.confirm(summary, code)

    def approve(self, contract=None, at=None):
        """Rolando's approval, made outside dispatch (not counted as a prompt)."""
        saved, self.confirm = self.confirm, Confirm()
        try:
            self.approvals.approve(contract or self.contract, at or self.now)
        finally:
            self.confirm = saved

    def kinds(self, kind, task=None):
        return [
            s.event
            for s in self.store.events()
            if s.event.kind == kind and (task is None or s.event.task == task)
        ]

    def assert_nothing_sent(self):
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(self.kinds(ev.FIRE_INTENT), [])

    def refused(self, contract=None):
        with self.assertRaises(Refused) as cm:
            self.dispatcher.dispatch(contract or self.contract)
        return {b.code for b in cm.exception.blocks}

    def dispatch(self, contract=None):
        return self.dispatcher.dispatch(contract or self.contract)


class DispatchTests(DispatchCase):
    # --- ac1: no fire without a valid, approved, in-budget contract ---

    def test_ac1_launches_after_approval_at_the_prompt(self):
        result = self.dispatch()
        self.assertEqual(result.outcome, "launched")
        self.assertEqual(len(self.confirm.calls), 1)
        self.assertEqual(self.confirm.calls[0][1], self.digest.short)
        self.assertEqual(result.run, RunId(self.a1, 1))
        self.assertEqual(len(self.adapter.requests), 1)
        request = self.adapter.requests[0]
        self.assertEqual(request.digest, self.digest)
        envelope = json.loads(request.text)
        self.assertEqual(envelope["branch"], self.a1.branch)
        self.assertEqual(
            self.base.calls, [(self.contract["repository"], self.contract["base_commit"], "main")]
        )

    def test_ac1_unapproved_and_declined_sends_nothing(self):
        self.confirm.answer = False
        self.assertIn("not-approved", self.refused())
        self.assertEqual(len(self.confirm.calls), 1)
        self.assert_nothing_sent()
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_expired_approval_prompts_and_declined_sends_nothing(self):
        self.approve(at=NOW - timedelta(days=4))
        self.confirm.answer = False
        self.assertIn("not-approved", self.refused())
        self.assertEqual(len(self.confirm.calls), 1)
        self.assert_nothing_sent()

    def test_ac1_expired_approval_renewed_at_the_prompt_fires(self):
        self.approve(at=NOW - timedelta(days=4))
        self.assertEqual(self.dispatch().outcome, "launched")
        self.assertEqual(len(self.confirm.calls), 1)

    def test_ac1_modified_contract_prompts_and_declined_sends_nothing(self):
        self.approve()
        changed = example(goal="Something else entirely.")
        self.assertNotEqual(contracts.digest(changed), self.digest)
        self.confirm.answer = False
        self.assertIn("not-approved", self.refused(changed))
        self.assertEqual(len(self.confirm.calls), 1)
        self.assertEqual(self.confirm.calls[0][1], contracts.digest(changed).short)
        self.assert_nothing_sent()

    def test_ac1_revoked_contract_is_refused_without_a_prompt(self):
        self.approve()
        saved, self.confirm = self.confirm, Confirm()
        self.approvals.revoke(self.task, self.digest, "changed my mind", NOW)
        self.confirm = saved
        self.assertIn("approval-revoked", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assert_nothing_sent()
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_invalid_contract_is_refused_before_anything(self):
        broken = example()
        del broken["base_commit"]
        self.assertEqual(self.refused(broken), {"contract-invalid"})
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(self.base.calls, [])
        self.assert_nothing_sent()
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_over_budget_contract_is_refused_without_a_prompt(self):
        one = example(attempt_budget=1)
        self.approve(one)
        self.assertEqual(self.dispatch(one).outcome, "launched")
        url = "https://claude.ai/code/cse_1"
        saved, self.confirm = self.confirm, Confirm()
        self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=[url])
        self.recovery.close_attempt(self.a1, State.FAILED, "tests failed", NOW)
        self.confirm = saved
        self.confirm.calls.clear()
        self.now += timedelta(minutes=1)
        codes = self.refused(one)
        self.assertIn("over-budget", codes)
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac1_wrong_repository_is_refused_before_any_prompt(self):
        other = example(repository="someone/elsewhere")
        self.assertEqual(contracts.approval_errors(other), [])
        self.assertEqual(self.refused(other), {"wrong-repository"})
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(self.base.calls, [])
        self.assert_nothing_sent()
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_base_commit_not_on_main_is_refused(self):
        self.approve()
        self.base.answer = False
        self.assertEqual(self.refused(), {"base-not-on-main"})
        self.assertEqual(self.confirm.calls, [])
        self.assert_nothing_sent()
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_unreadable_base_check_is_refused(self):
        self.approve()
        self.base.answer = BaseUnreadable("gh is down")
        self.assertEqual(self.refused(), {"base-unreadable"})
        self.assertEqual(self.confirm.calls, [])
        self.assert_nothing_sent()

    def test_ac1_lane_held_by_another_attempt_refuses_before_any_prompt(self):
        other = example(task_id="other-task")
        self.approve(other)
        run = self.gate.reserve(TaskId("other-task"), contracts.digest(other), NOW)
        self.gate.record_launch(run, launched(9), NOW)
        self.now += timedelta(minutes=1)
        self.assertIn("unresolved-attempt", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(self.kinds(ev.FIRE_INTENT, self.task), [])
        self.assertEqual(self.kinds(HUMAN_DECISION, self.task), [])
        self.assertEqual(self.keys_fetched, [])

    def test_ac1_hold_refuses_before_any_prompt(self):
        self.gate.hold("manual", "stop for now", NOW)
        self.assertIn("hold", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assert_nothing_sent()

    # --- ac2: a repeated dispatch returns what is on record ---

    def test_ac2_repeat_after_a_launch_sends_nothing_more(self):
        self.approve()
        first = self.dispatch()
        self.now += timedelta(minutes=1)
        again = self.dispatch()
        self.assertEqual(again.outcome, "already-dispatched")
        self.assertIsNone(again.run)
        self.assertEqual(again.attempt, first.attempt)
        self.assertEqual(again.status.state, State.RUNNING)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)
        self.assertEqual(self.keys_fetched, [TRIG])

    def test_ac2_repeat_after_an_unknown_outcome_sends_nothing_more(self):
        self.approve()
        self.adapter = FakeRuntimeAdapter([FakeStep.lost_response()])
        self.assertEqual(self.dispatch().outcome, "launch-outcome-unknown")
        self.now += timedelta(minutes=30)
        again = self.dispatch()
        self.assertEqual(again.outcome, "already-dispatched")
        self.assertEqual(again.status.state, State.UNKNOWN)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_ac2_repeat_while_the_fire_is_in_flight_sends_nothing(self):
        self.approve()
        seen = []

        def second_dispatch(request):
            seen.append(self.dispatch())
            return launched(1)

        self.adapter = ScriptedAdapter([second_dispatch])
        self.assertEqual(self.dispatch().outcome, "launched")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].outcome, "already-dispatched")
        self.assertEqual(seen[0].status.state, State.DISPATCHING)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac2_repeat_of_an_older_contract_version_reports_the_one_on_record(self):
        self.approve()
        self.dispatch()
        changed = example(goal="Something else entirely.")
        again = self.dispatch(changed)
        self.assertEqual(again.outcome, "already-dispatched")
        self.assertIn("different version", again.message)
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(len(self.adapter.requests), 1)

    # --- ac3: launch results are recorded; ambiguity is never re-sent ---

    def test_ac3_launch_records_session_and_backs_up(self):
        self.approve()
        result = self.dispatch()
        (fire,) = self.kinds(ev.FIRE_RESULT)
        self.assertEqual(fire.data["outcome"], "launched")
        self.assertEqual(fire.data["session_id"], "cse_1")
        self.assertEqual(fire.data["session_url"], "https://claude.ai/code/cse_1")
        self.assertEqual(result.launch.session_url, "https://claude.ai/code/cse_1")
        self.assertIn("https://claude.ai/code/cse_1", result.message)
        self.assertEqual(self.backups, [NOW])

    def test_ac3_run_context_and_intent_are_recorded_before_the_send(self):
        self.approve()
        at_send = []

        def check(request):
            run_ctx = [e for e in self.kinds(ledger_kinds.RUN_CONTEXT) if e.run == request.run]
            intents = [e for e in self.kinds(ev.FIRE_INTENT) if e.run == request.run]
            results = self.kinds(ev.FIRE_RESULT)
            at_send.append((len(run_ctx), len(intents), len(results)))
            self.assertEqual(run_ctx[0].data["model_config_version"], "test")
            self.assertEqual(run_ctx[0].data["contract_digest"], self.digest.value)
            self.assertEqual(run_ctx[0].data["base_commit"], self.contract["base_commit"])

        self.adapter.before = check
        self.dispatch()
        self.assertEqual(at_send, [(1, 1, 0)])
        kinds = [s.event.kind for s in self.store.events()]
        self.assertLess(kinds.index(ev.FIRE_INTENT), kinds.index(ledger_kinds.RUN_CONTEXT))
        self.assertLess(kinds.index(ledger_kinds.RUN_CONTEXT), kinds.index(ev.FIRE_RESULT))

    def _assert_ambiguous(self, step):
        self.approve()
        self.adapter = FakeRuntimeAdapter([step])
        result = self.dispatch()
        self.assertEqual(result.outcome, "launch-outcome-unknown")
        self.assertEqual(result.status.state, State.UNKNOWN)
        (fire,) = self.kinds(ev.FIRE_RESULT)
        self.assertEqual(fire.data["outcome"], "launch-outcome-unknown")
        self.assertIn("Nothing will be sent again", result.message)
        for later in (timedelta(minutes=5), timedelta(days=1)):
            self.now = NOW + later
            self.assertEqual(self.dispatch().outcome, "already-dispatched")
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac3_no_response_is_unknown_and_never_resent(self):
        self._assert_ambiguous(FakeStep.lost_response(status=None))

    def test_ac3_5xx_is_unknown_and_never_resent(self):
        self._assert_ambiguous(FakeStep.lost_response(status=502, body="bad gateway"))

    def test_ac3_200_without_session_is_unknown_and_never_resent(self):
        self._assert_ambiguous(FakeStep.lost_response(status=200, body="{}"))

    def test_ac3_ctrl_c_during_the_send_is_recorded_unknown(self):
        self.approve()
        interrupted = routine.LaunchInterrupted(
            LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, detail="KeyboardInterrupt")
        )
        self.adapter = ScriptedAdapter([interrupted])
        with self.assertRaises(KeyboardInterrupt):
            self.dispatch()
        (fire,) = self.kinds(ev.FIRE_RESULT)
        self.assertEqual(fire.data["outcome"], "launch-outcome-unknown")
        self.now += timedelta(minutes=1)
        self.assertEqual(self.dispatch().outcome, "already-dispatched")
        self.assertEqual(len(self.adapter.requests), 1)

    def test_ac3_any_other_exception_during_the_send_is_recorded_unknown(self):
        self.approve()
        self.adapter = ScriptedAdapter([RuntimeError("socket exploded")])
        with self.assertRaises(RuntimeError):
            self.dispatch()
        (fire,) = self.kinds(ev.FIRE_RESULT)
        self.assertEqual(fire.data["outcome"], "launch-outcome-unknown")
        self.assertIn("RuntimeError", fire.data["detail"])
        self.now += timedelta(minutes=1)
        self.assertEqual(self.dispatch().outcome, "already-dispatched")
        self.assertEqual(len(self.adapter.requests), 1)

    def test_ac3_failure_preparing_the_fire_uses_nothing(self):
        self.approve()

        def no_key():
            raise LookupError("no Keychain item")

        self.key_side_effect = no_key
        with self.assertRaises(LookupError):
            self.dispatch()
        self.assert_nothing_sent()
        self.assertEqual(self.kinds(ev.ATTEMPT_RESERVED), [])

    # --- ac4: 429 ---

    def test_ac4_rate_limit_waits_then_asks_for_the_refire(self):
        self.approve()
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600), launched(2)])
        first = self.dispatch()
        self.assertEqual(first.outcome, "not-launched")
        self.assertIn("rate limited", first.message)
        (fire,) = self.kinds(ev.FIRE_RESULT)
        self.assertEqual(fire.data["reason"], "rate-limited")
        self.assertEqual(len(self.kinds(ev.RATE_LIMIT_WAIT)), 1)

        self.now = NOW + timedelta(minutes=5)
        self.assertIn("rate-limited", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(self.kinds(ev.REFIRE_AUTHORIZED), [])

        self.now = NOW + timedelta(minutes=11)
        second = self.dispatch()
        self.assertEqual(second.outcome, "launched")
        self.assertEqual(len(self.confirm.calls), 1)
        self.assertEqual(second.run, RunId(self.a1, 2))
        self.assertEqual(len(self.kinds(ev.REFIRE_AUTHORIZED)), 1)
        self.assertEqual(
            [r.run for r in self.adapter.requests], [RunId(self.a1, 1), RunId(self.a1, 2)]
        )
        # Same attempt, same branch.
        self.assertEqual(json.loads(self.adapter.requests[1].text)["branch"], self.a1.branch)
        self.assertEqual(self.kinds(ev.ATTEMPT_RESERVED, self.task)[0].attempt, self.a1)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 1)

    def test_ac4_rate_limit_wait_with_an_expired_approval_still_asks_nothing(self):
        self.approve(at=NOW - timedelta(days=3) + timedelta(minutes=2))
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        self.dispatch()
        self.now = NOW + timedelta(minutes=5)
        self.assertIn("rate-limited", self.refused())
        self.assertEqual(self.confirm.calls, [])

    def test_ac4_hold_after_the_wait_refuses_before_the_refire_prompt(self):
        self.approve()
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        self.dispatch()
        self.gate.hold("manual", "pause", NOW + timedelta(minutes=1))
        self.now = NOW + timedelta(minutes=11)
        self.assertIn("hold", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(self.kinds(ev.REFIRE_AUTHORIZED), [])
        self.assertEqual(len(self.adapter.requests), 1)

    def test_ac4_declined_refire_sends_nothing(self):
        self.approve()
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        self.dispatch()
        self.now = NOW + timedelta(minutes=11)
        self.confirm.answer = False
        self.assertIn("refire-not-authorized", self.refused())
        self.assertEqual(len(self.confirm.calls), 1)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac4_refire_after_revocation_is_refused_and_sends_nothing(self):
        self.approve()
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        self.dispatch()
        saved, self.confirm = self.confirm, Confirm()
        self.approvals.revoke(
            self.task, self.digest, "no longer wanted", NOW + timedelta(minutes=1)
        )
        self.confirm = saved
        self.now = NOW + timedelta(minutes=11)
        self.assertIn("approval-revoked", self.refused())
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)
        self.assertEqual(self.keys_fetched, [TRIG])

    def test_ac4_refire_of_a_changed_contract_is_refused(self):
        self.approve()
        self.adapter = ScriptedAdapter([not_launched(429, retry_after=600)])
        self.dispatch()
        self.now = NOW + timedelta(minutes=11)
        changed = example(goal="Something else entirely.")
        self.assertEqual(self.refused(changed), {"contract-changed"})
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(len(self.adapter.requests), 1)

    # --- ac5: concurrency ---

    def test_ac5_ledger_busy_at_reserve_sends_nothing(self):
        self.approve()

        def lock_it():
            self.store.locked_elsewhere = True

        self.key_side_effect = lock_it
        self.assertEqual(self.refused(), {"ledger-busy"})
        self.store.locked_elsewhere = False
        self.assert_nothing_sent()

    def test_ac5_revocation_between_check_and_reserve_is_caught_under_the_lock(self):
        self.approve()

        def revoke():
            self.approvals.revoke(self.task, self.digest, "race", NOW)

        self.key_side_effect = revoke
        self.assertIn("approval-revoked", self.refused())
        self.assert_nothing_sent()
        (refusal,) = self.kinds(ev.DISPATCH_REFUSED)
        self.assertIn("approval-revoked", [b["code"] for b in refusal.data["blocks"]])

    def test_ac5_hold_between_check_and_reserve_is_caught(self):
        self.approve()
        self.key_side_effect = lambda: self.gate.hold("manual", "race", NOW)
        self.assertIn("hold", self.refused())
        self.assert_nothing_sent()

    def test_ac5_other_reservation_between_check_and_reserve_is_caught(self):
        self.approve()
        other = example(task_id="other-task")
        self.approve(other)

        def reserve_other():
            self.gate.reserve(TaskId("other-task"), contracts.digest(other), NOW)

        self.key_side_effect = reserve_other
        self.assertIn("unresolved-attempt", self.refused())
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual([e.task for e in self.kinds(ev.FIRE_INTENT)], [TaskId("other-task")])

    def test_ac5_same_task_reserved_between_check_and_reserve_fires_once(self):
        self.approve()

        def reserve_same():
            self.gate.reserve(self.task, self.digest, NOW)

        self.key_side_effect = reserve_same
        self.refused()
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 1)

    # --- ac6: the start key ---

    def test_ac6_start_key_is_nowhere_but_the_adapter(self):
        self.approve()
        result = self.dispatch()
        self.assertEqual(self.adapters_made, [(TRIG, START_KEY)])
        self.assertNotIn(START_KEY, self.adapter.requests[0].text)
        for s in self.store.events():
            self.assertNotIn(START_KEY, repr(s.event))
        self.assertNotIn(START_KEY, repr(result))
        again = self.dispatch()
        self.assertNotIn(START_KEY, repr(again))

    def test_ac6_start_key_absent_from_unknown_and_interrupted_records(self):
        self.approve()
        self.adapter = ScriptedAdapter([RuntimeError(f"boom with {START_KEY}")])
        with self.assertRaises(RuntimeError):
            self.dispatch()
        for s in self.store.events():
            self.assertNotIn(START_KEY, repr(s.event))

    # --- ac7: launch is not completion ---

    def test_ac7_a_launched_attempt_is_running_not_merged(self):
        self.approve()
        result = self.dispatch()
        self.assertEqual(result.status.state, State.RUNNING)
        self.assertNotEqual(result.status.state, State.MERGED)
        self.assertIn("not a finished task", result.message)
        self.assertEqual(self.recovery.status(self.task, NOW).state, State.RUNNING)

    # --- ac8: recovery marks the run unknown mid-send ---

    def test_ac8_late_launch_after_recovery_is_kept_and_reads_unknown(self):
        self.approve()

        def slow(request):
            self.now = INTERRUPTED
            self.assertEqual(self.recovery.recover(self.now), [request.run])
            return launched(1)

        self.adapter = ScriptedAdapter([slow])
        result = self.dispatch()
        (late,) = self.kinds(LATE_FIRE_RESULT)
        self.assertEqual(late.run, RunId(self.a1, 1))
        results = self.kinds(ev.FIRE_RESULT)
        self.assertEqual([r.data["outcome"] for r in results], ["launch-outcome-unknown"])
        # The gate still reads the run as launch-outcome-unknown; the late
        # session URL is kept beside it for the clearing record.
        last = LedgerView.build(self.store.events()).attempts[self.a1].last_fire
        self.assertIs(last.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        status = self.recovery.attempt_status(self.a1, self.now)
        self.assertFalse(status.writer_cleared)
        self.assertIn("https://claude.ai/code/cse_1", status.session_urls)
        self.assertIn("https://claude.ai/code/cse_1", result.status.session_urls)
        self.assertIn("answered late", result.message)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.dispatch().outcome, "already-dispatched")
        self.assertEqual(len(self.adapter.requests), 1)

    # --- review findings ---

    def test_repair_moves_on_from_an_attempt_that_never_reached_a_close(self):
        # a1 launched and its session finished without a PR; Rolando clears
        # it and authorizes attempt 2. Dispatch fires a2 rather than reporting a1.
        self.approve()
        self.dispatch()
        self.now += timedelta(hours=1)
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.COMPLETED,
            self.now,
            session_urls=["https://claude.ai/code/cse_1"],
        )
        self.approvals.authorize_repair(self.contract, 2, "the session ended with no PR", self.now)
        self.adapter = ScriptedAdapter([launched(2)])
        result = self.dispatch()
        self.assertEqual(result.outcome, "launched")
        self.assertEqual(result.run, RunId(AttemptId(self.task, 2), 1))
        envelope = json.loads(self.adapter.requests[0].text)
        self.assertEqual(envelope["branch"], f"claude/{self.task}-a2")

    def test_without_a_repair_a_finished_session_is_still_reported_not_refired(self):
        self.approve()
        self.dispatch()
        self.now += timedelta(hours=1)
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.COMPLETED,
            self.now,
            session_urls=["https://claude.ai/code/cse_1"],
        )
        self.assertEqual(self.dispatch().outcome, "already-dispatched")
        self.assertEqual(len(self.adapter.requests), 1)

    def test_run_context_is_written_with_the_fire_intent(self):
        self.approve()
        self.dispatch()
        seqs = {
            s.event.kind: s.seq
            for s in self.store.events()
            if s.event.kind in (ev.FIRE_INTENT, ledger_kinds.RUN_CONTEXT)
        }
        self.assertEqual(seqs[ledger_kinds.RUN_CONTEXT], seqs[ev.FIRE_INTENT] + 1)

    def test_a_result_that_cannot_be_recorded_is_printed_with_its_session(self):
        warnings = []
        self.dispatcher.launcher._warn = warnings.append
        self.approve()

        def lock_out(request):
            self.store.locked_elsewhere = True

        self.adapter = ScriptedAdapter([launched(1)], before=lock_out)
        with self.assertRaises(LedgerLocked):
            self.dispatch()
        self.assertEqual(len(warnings), 1)
        self.assertIn("https://claude.ai/code/cse_1", warnings[0])
        self.assertNotIn(START_KEY, warnings[0])

    def test_a_failed_backup_warns_and_keeps_the_launch(self):
        warnings = []
        self.dispatcher.launcher._warn = warnings.append

        def broken(now):
            raise OSError("disk full")

        self.dispatcher.launcher._backup = broken
        self.approve()
        result = self.dispatch()
        self.assertEqual(result.outcome, "launched")
        self.assertEqual(len(warnings), 1)
        self.assertIn("backup failed", warnings[0])

    def test_repeat_dispatch_reports_the_record_even_if_github_is_down(self):
        self.approve()
        self.dispatch()
        self.base.answer = BaseUnreadable("gh is down")
        self.assertEqual(self.dispatch().outcome, "already-dispatched")


class SqliteDispatchTests(DispatchCase):
    """Restart: a fresh Dispatcher on the same durable ledger."""

    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.chmod(tmp.name, 0o700)
        self.home = Path(tmp.name) / "home" / ".software-factory"
        store = SqliteLedgerStore(self.home / "ledger.db")
        self._stores = [store]
        self.addCleanup(lambda: [s.close() for s in self._stores])
        return store

    def restart(self):
        self.store.close()
        self.store = SqliteLedgerStore(self.home / "ledger.db")
        self._stores.append(self.store)
        self.build()

    def test_ac2_repeat_after_restart_sends_nothing_more(self):
        self.approve()
        self.assertEqual(self.dispatch().outcome, "launched")
        self.restart()
        self.now += timedelta(minutes=1)
        again = self.dispatch()
        self.assertEqual(again.outcome, "already-dispatched")
        self.assertEqual(again.status.state, State.RUNNING)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac2_crash_mid_send_then_restart_is_unknown_and_not_resent(self):
        self.approve()
        self.gate.reserve(self.task, self.digest, NOW)  # a process died here
        self.restart()
        self.now = NOW + timedelta(minutes=1)
        young = self.dispatch()
        self.assertEqual(young.outcome, "already-dispatched")
        self.assertEqual(young.status.state, State.DISPATCHING)
        self.now = INTERRUPTED
        later = self.dispatch()
        self.assertEqual(later.outcome, "already-dispatched")
        self.assertEqual(later.status.state, State.UNKNOWN)
        self.assertEqual(self.adapter.requests, [])

    def test_ac6_start_key_not_in_the_ledger_file(self):
        self.approve()
        self.dispatch()
        self.store.close()
        self._stores.remove(self.store)
        raw = (self.home / "ledger.db").read_bytes()
        self.assertNotIn(START_KEY.encode(), raw)
        self.store = SqliteLedgerStore(self.home / "ledger.db")
        self._stores.append(self.store)


class GatePreconditionTests(unittest.TestCase):
    """ac9: the precondition hook on AttemptGate.reserve."""

    def setUp(self):
        self.store = MemoryLedger()
        self.gate = AttemptGate(self.store)
        self.task = TaskId("pilot-1")
        self.digest = ContractDigest.of(b"contract")
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))
        self.calls = []

    def kinds(self, kind):
        return [s.event for s in self.store.events() if s.event.kind == kind]

    def test_ac9_blocks_from_the_hook_refuse_and_are_recorded(self):
        def hook(run):
            self.calls.append(run)
            return [Notice("approval-revoked", "withdrawn")]

        with self.assertRaises(DispatchRefused) as cm:
            self.gate.reserve(self.task, self.digest, NOW, precondition=hook)
        self.assertEqual([b.code for b in cm.exception.decision.blocks], ["approval-revoked"])
        self.assertEqual(self.calls, [RunId(AttemptId(self.task, 1), 1)])
        (refusal,) = self.kinds(ev.DISPATCH_REFUSED)
        self.assertEqual([b["code"] for b in refusal.data["blocks"]], ["approval-revoked"])
        self.assertEqual(self.kinds(ev.FIRE_INTENT), [])
        self.assertEqual(self.kinds(ev.ATTEMPT_RESERVED), [])

    def test_ac9_hook_runs_under_the_writer_lock(self):
        def hook(run):
            with self.assertRaises(LedgerLocked):
                with self.store.writer_lock():
                    pass
            self.calls.append(run)
            return []

        run = self.gate.reserve(self.task, self.digest, NOW, precondition=hook)
        self.assertEqual(self.calls, [run])
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac9_hook_not_called_when_the_gate_refuses(self):
        self.gate.hold("manual", "stop", NOW)

        def hook(run):
            self.calls.append(run)
            return []

        with self.assertRaises(DispatchRefused) as cm:
            self.gate.reserve(self.task, self.digest, NOW, precondition=hook)
        self.assertEqual([b.code for b in cm.exception.decision.blocks], ["hold"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.kinds(ev.DISPATCH_REFUSED)), 1)


SHA = "a" * 40
REPO = "rNavarrete/factory-pilot-demo"


class GhBaseCheckTests(unittest.TestCase):
    """ac10: GhBaseCheck reads ``gh api .../compare`` through an injected run."""

    def check(self, returncode=0, stdout="", stderr="", raises=None):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        return GhBaseCheck(run=run, gh="gh"), calls

    def status(self, status):
        return json.dumps({"status": status, "ahead_by": 0})

    def test_ac10_identical_and_behind_are_on_main(self):
        for status in ("identical", "behind"):
            c, _ = self.check(stdout=self.status(status))
            self.assertTrue(c.on_branch(REPO, SHA, "main"), status)

    def test_ac10_ahead_and_diverged_are_not_on_main(self):
        for status in ("ahead", "diverged"):
            c, _ = self.check(stdout=self.status(status))
            self.assertFalse(c.on_branch(REPO, SHA, "main"), status)

    def test_ac10_unknown_commit_is_not_on_main(self):
        c, _ = self.check(1, stderr="No commit found for SHA: " + SHA)
        self.assertFalse(c.on_branch(REPO, SHA, "main"))

    def test_ac10_a_bare_404_is_unreadable_not_a_verdict(self):
        # gh also says 404 for a repo it can't see (logged out, no access).
        c, _ = self.check(1, stderr="gh: Not Found (HTTP 404)")
        with self.assertRaises(BaseUnreadable):
            c.on_branch(REPO, SHA, "main")

    def test_ac10_other_failures_are_unreadable(self):
        cases = [
            self.check(1, stderr="HTTP 502: Bad Gateway"),
            self.check(stdout="not json"),
            self.check(stdout=json.dumps({"no": "status"})),
            self.check(stdout=json.dumps(["identical"])),
            self.check(raises=FileNotFoundError("gh")),
            self.check(raises=subprocess.TimeoutExpired("gh", 60)),
        ]
        for c, _ in cases:
            with self.assertRaises(BaseUnreadable):
                c.on_branch(REPO, SHA, "main")

    def test_ac10_bad_input_is_refused_before_running_gh(self):
        bad = [
            ("not a repo", SHA, "main"),
            ("owner/name/extra", SHA, "main"),
            (REPO, "abc123", "main"),
            (REPO, "A" * 40, "main"),
            (REPO, SHA + "0", "main"),
            (REPO, SHA, "../main"),
            (REPO, SHA, "ma in"),
        ]
        for repo, sha, branch in bad:
            c, calls = self.check(stdout=self.status("identical"))
            with self.assertRaises(ValueError, msg=(repo, sha, branch)):
                c.on_branch(repo, sha, branch)
            self.assertEqual(calls, [])

    def test_ac10_gh_argv(self):
        c, calls = self.check(stdout=self.status("identical"))
        c.on_branch(REPO, SHA, "release/v1")
        ((argv, kwargs),) = calls
        self.assertEqual(
            argv,
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{REPO}/compare/release%2Fv1...{SHA}",
            ],
        )
        self.assertEqual(kwargs["timeout"], GH_TIMEOUT_SECONDS)
        self.assertIs(kwargs["check"], False)
        self.assertIs(kwargs["capture_output"], True)


if __name__ == "__main__":
    unittest.main()
