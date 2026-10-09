"""Recovery after controller crashes, lost responses and restarts (ENG-153).

An independent suite written from the recovery API spec and the ticket's
acceptance criteria only, not from the implementation. Test names start with
the criterion they prove (``ac1`` .. ``ac8``); ``edge`` tests probe the rules
Rolando likes to check by hand (lookalike URLs, foreign PRs, blank notes,
naive times, restarts).

``RecoverySpecTests`` runs on the in-memory ledger. ``SqliteRecoverySpecTests``
reruns every test on the durable ledger, with ``restart`` closing and
reopening the file, and adds crash tests with a real child process.

Nothing here reads the real clock: every call gets an explicit aware time.
"""

import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.adapter.fake import FakeRuntimeAdapter, FakeStep
from controller.approval import ApprovalRefused, Approvals, StaticKey
from controller.attempts import AttemptGate, DispatchRefused
from controller.attempts import events as ev
from controller.interfaces import (
    AttemptId,
    LaunchOutcome,
    LaunchRequest,
    LedgerEvent,
    LedgerLocked,
    RunId,
    TaskId,
)
from controller.ledger import InvalidEvent, SqliteLedgerStore, records
from controller.ledger import kinds as ledger_kinds
from controller.recovery import (
    FINISHED,
    INTERRUPTED_AFTER,
    LATE_FIRE_RESULT,
    LAUNCH_RECONCILED,
    PR_OBSERVED,
    RELEASE_STATUS,
    Finding,
    GitHubReader,
    GitHubUnreadable,
    PullRequest,
    Recovery,
    RecoveryRefused,
    State,
)
from controller.recovery import events as rev_events
from tests.test_approval import example, yes
from tests.test_attempts import LOST, MemoryLedger, launched, not_launched

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"k" * 32)
REPO = "rNavarrete/factory-pilot-demo"
BOT = "rnavarrete-factory-bot"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_MERGE = "c" * 40
URL1 = "https://claude.ai/code/cse_1"
URL2 = "https://claude.ai/code/cse_2"
STALE = NOW + INTERRUPTED_AFTER + timedelta(minutes=1)
"""Past the point where a fire-intent without a result counts as interrupted."""
YOUNG = NOW + INTERRUPTED_AFTER - timedelta(seconds=1)

REFUSED = (RecoveryRefused, ApprovalRefused)
"""A clearing can be refused by recovery's 6.1 checks or by approval's own."""
BAD_INPUT = (RecoveryRefused, ValueError, TypeError)


def no(summary, code):
    return False


class Recorder:
    """A confirm that types the right code and remembers what it was asked."""

    def __init__(self):
        self.codes = []

    def __call__(self, summary, code):
        self.codes.append(code)
        return True


class FakeGitHub:
    """A read-only GitHubReader. Tests set ``branches``, ``pulls`` and the push answer."""

    def __init__(self):
        self.branches: dict[str, str] = {}
        self.pulls: list[PullRequest] = []
        self.push_allowed = True
        self.push_error: Exception | None = None
        self.push_checks: list[tuple[str, str]] = []
        self.read_error: Exception | None = None

    def branch_head(self, repo, branch):
        if self.read_error is not None:
            raise self.read_error
        return self.branches.get(branch) if repo == REPO else None

    def pulls_for_branch(self, repo, branch):
        return [p for p in self.pulls if p.head_branch == branch]

    def recent_pulls(self, repo):
        return list(reversed(self.pulls))[:100]

    def can_push(self, repo, login):
        self.push_checks.append((repo, login))
        if self.push_error is not None:
            raise self.push_error
        return self.push_allowed


class RecoverySpecTests(unittest.TestCase):
    def make_store(self):
        return MemoryLedger()

    def restart(self, store):
        return store

    def setUp(self):
        self.store = self.make_store()
        self.github = FakeGitHub()
        self.contract = example()
        self.task = TaskId(self.contract["task_id"])
        self.digest = contracts.digest(self.contract)
        self.a1 = AttemptId(self.task, 1)
        self.a2 = AttemptId(self.task, 2)
        self.build()
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))
        self.approvals.approve(self.contract, NOW - timedelta(minutes=30))

    # --- helpers ---

    def build(self):
        """Fresh gate, approvals and recovery on the current store (a new process)."""
        self.gate = AttemptGate(self.store)
        self.approvals = Approvals(self.store, KEY, confirm=yes, os_user="rolando")
        self.recovery = self.make_recovery()

    def make_recovery(self, github="default", confirm=yes, approvals=None):
        return Recovery(
            self.store,
            approvals or self.approvals,
            self.github if github == "default" else github,
            repo=REPO,
            worker_logins=frozenset({BOT}),
            gate=self.gate,
            confirm=confirm,
        )

    def reopen(self):
        self.store = self.restart(self.store)
        self.build()

    def dispatch(self, result=None, at=NOW):
        """approvals.check -> gate.reserve -> gate.record_launch, as dispatch does."""
        run = self.reserve(at)
        self.gate.record_launch(run, result or launched(1), at)
        return run

    def reserve(self, at=NOW):
        verdict = self.approvals.check(self.contract, at)
        self.assertTrue(verdict.approved, verdict.blocks)
        run = self.gate.reserve(self.task, self.digest, at)
        self.assertEqual(run, verdict.run)
        return run

    def append(self, *events):
        with self.store.writer_lock():
            return self.store.append(*events)

    def kinds(self, kind):
        return [s.event for s in self.store.events() if s.event.kind == kind]

    def seq(self):
        return len(self.store.events())

    @contextmanager
    def writes_nothing(self):
        before = list(self.store.events())
        yield
        self.assertEqual(list(self.store.events()), before)

    def status(self, at, attempt=None):
        attempt = attempt or self.a1
        task = self.recovery.status(self.task, at)
        found = [a for a in task.attempts if a.attempt == attempt]
        self.assertEqual(len(found), 1, f"{attempt} not in status: {task}")
        return found[0]

    def block_codes(self, at):
        return {b.code for b in self.recovery.blocks(at)}

    def assert_replacement_refused(self, at):
        """Attempt 2, with Rolando's repair go-ahead, still may not start."""
        if not any(e.attempt == self.a2 for e in self.kinds(ev.REPAIR_AUTHORIZED)):
            self.approvals.authorize_repair(self.contract, 2, "attempt 1 lost", at)
        verdict = self.approvals.check(self.contract, at)
        self.assertFalse(verdict.approved)
        self.assertIn("unresolved-attempt", {b.code for b in verdict.blocks})
        decision = self.gate.decide(self.task, self.digest, at)
        self.assertFalse(decision.allowed)
        self.assertIn("unresolved-attempt", {b.code for b in decision.blocks})

    def assert_replacement_allowed(self, at):
        if not any(e.attempt == self.a2 for e in self.kinds(ev.REPAIR_AUTHORIZED)):
            self.approvals.authorize_repair(self.contract, 2, "attempt 1 failed", at)
        run = self.reserve(at)
        self.assertEqual(run, RunId(self.a2, 1))

    def pr(self, number=7, attempt=None, **changes):
        attempt = attempt or self.a1
        fields = dict(
            number=number,
            url=f"https://github.com/{REPO}/pull/{number}",
            title=f"{attempt.pr_title_marker(self.digest)} Filter books by status",
            body=f"What changed: a filter.\n\n{self.digest.pr_body_line}\n",
            head_branch=attempt.branch,
            head_sha=SHA_A,
            head_repo=REPO,
            base_repo=REPO,
            base_branch="main",
            author=BOT,
            state="open",
            draft=False,
            merged=False,
            merge_commit=None,
        )
        fields.update(changes)
        return PullRequest(**fields)

    def publish(self, *pulls, branch_sha=SHA_A, attempt=None):
        attempt = attempt or self.a1
        self.github.branches[attempt.branch] = branch_sha
        self.github.pulls = list(pulls)

    def checks(self, run, sha, *conclusions, at=NOW):
        results = [{"name": f"check-{i}", "conclusion": c} for i, c in enumerate(conclusions)]
        self.append(records.checks(run, sha, results, at))

    def candidates_for_pr(self, number):
        return [e for e in self.kinds(ledger_kinds.CANDIDATE) if e.data.get("pr_number") == number]

    def lost_launch_with_session(self, at=NOW):
        """A launch whose response was lost although the session really started."""
        adapter = FakeRuntimeAdapter([FakeStep.lost_response(session_created=True)])
        run = self.reserve(at)
        result = adapter.launch(LaunchRequest(run, self.digest, "payload"))
        self.gate.record_launch(run, result, at)
        return run, adapter

    # --- sanity ---

    def test_fake_github_satisfies_the_protocol(self):
        reader: GitHubReader = self.github
        self.assertIsNone(reader.branch_head(REPO, "claude/nothing-a1"))

    def test_task_with_no_attempt_is_ready(self):
        task = self.recovery.status(self.task, NOW)
        self.assertEqual(task.state, State.READY)
        self.assertEqual(task.attempts, ())
        self.assertEqual(task.release, "not recorded")

    def test_finished_states_are_exactly_failed_canceled_merged(self):
        self.assertEqual(FINISHED, frozenset({State.FAILED, State.CANCELED, State.MERGED}))
        self.assertEqual(State.UNKNOWN.value, "launch-outcome-unknown")
        self.assertEqual(State.MERGED.value, "accepted-merged")

    # --- AC1 / AC8: controller crash between reserve and result ---

    def test_ac1_intent_is_durable_before_the_network_call(self):
        run = self.reserve()
        intents = [e for e in self.kinds(ev.FIRE_INTENT) if e.run == run]
        self.assertEqual(len(intents), 1)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 1)
        self.assertEqual(self.status(NOW).state, State.DISPATCHING)

    def test_ac1_ac8_crash_between_reserve_and_result_is_not_redispatched_after_restart(self):
        run = self.reserve()
        self.reopen()
        st = self.status(NOW + timedelta(minutes=5))
        self.assertEqual(st.state, State.DISPATCHING)
        self.assertEqual(st.latest_run, run)
        self.assertFalse(st.writer_cleared)
        self.assert_replacement_refused(NOW + timedelta(minutes=5))
        with self.assertRaises(DispatchRefused):
            self.gate.reserve(self.task, self.digest, NOW + timedelta(minutes=5))
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac1_recover_leaves_a_young_intent_alone(self):
        self.reserve()
        self.reopen()
        with self.writes_nothing():
            self.assertEqual(self.recovery.recover(YOUNG), [])
        self.assertNotIn("launch-interrupted", self.block_codes(YOUNG))
        self.assertEqual(self.status(YOUNG).state, State.DISPATCHING)
        self.assertEqual(self.kinds(ev.FIRE_RESULT), [])

    def test_ac1_recover_marks_a_stale_intent_unknown_exactly_once(self):
        run = self.reserve()
        self.reopen()
        self.assertIn("launch-interrupted", self.block_codes(STALE))
        self.assertEqual(self.status(STALE).state, State.UNKNOWN)
        self.assertEqual(self.recovery.recover(STALE), [run])
        results = [e for e in self.kinds(ev.FIRE_RESULT) if e.run == run]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].data["outcome"], LaunchOutcome.OUTCOME_UNKNOWN.value)
        self.assertTrue(results[0].data["detail"].strip())
        with self.writes_nothing():
            self.assertEqual(self.recovery.recover(STALE + timedelta(hours=1)), [])
        self.assertNotIn("launch-interrupted", self.block_codes(STALE))
        st = self.status(STALE)
        self.assertEqual(st.state, State.UNKNOWN)
        self.assertFalse(st.writer_cleared)
        self.assertEqual(st.session_urls, ())
        self.assert_replacement_refused(STALE)

    def test_ac1_recover_never_fires(self):
        self.reserve()
        self.recovery.recover(STALE)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 1)

    def test_ac1_interrupted_run_cannot_be_refired(self):
        run = self.reserve()
        self.recovery.recover(STALE)
        with self.assertRaises(DispatchRefused) as caught:
            self.gate.reserve(self.task, self.digest, STALE, refire_of=run)
        self.assertIn("refire-not-allowed", {b.code for b in caught.exception.decision.blocks})

    def test_ac1_late_result_after_recover_adds_its_session_but_gate_still_sees_unknown(self):
        run = self.reserve()
        self.recovery.recover(STALE)
        with self.assertRaises(ValueError):
            self.gate.record_launch(run, launched(5), STALE)
        self.recovery.record_late_result(run, launched(5), STALE + timedelta(minutes=1))
        self.assertEqual(len(self.kinds(LATE_FIRE_RESULT)), 1)
        results = [e for e in self.kinds(ev.FIRE_RESULT) if e.run == run]
        self.assertEqual([r.data["outcome"] for r in results], ["launch-outcome-unknown"])
        st = self.status(STALE + timedelta(minutes=2))
        self.assertIn("https://claude.ai/code/cse_5", st.session_urls)
        self.assertFalse(st.writer_cleared)
        self.assert_replacement_refused(STALE + timedelta(minutes=2))
        # The late URL is on record, so a completed clearing naming it is valid.
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.COMPLETED,
            STALE + timedelta(minutes=3),
            session_urls=("https://claude.ai/code/cse_5",),
        )
        self.assertTrue(self.status(STALE + timedelta(minutes=3)).writer_cleared)

    # --- AC2: lost response after a real launch ---

    def test_ac2_lost_response_after_real_launch_is_unknown_and_blocks_replacement(self):
        run, adapter = self.lost_launch_with_session()
        self.assertEqual(len(adapter.sessions_created), 1)  # the hidden truth
        st = self.status(NOW + timedelta(minutes=1))
        self.assertEqual(st.state, State.UNKNOWN)
        self.assertFalse(st.writer_cleared)
        self.assertEqual(st.latest_run, run)
        self.assert_replacement_refused(NOW + timedelta(minutes=1))
        # A NOT_FOUND finding is not proof that nothing started.
        self.recovery.record_launch_finding(
            run, Finding.NOT_FOUND, (), "run list empty at 12:05", NOW + timedelta(minutes=5)
        )
        self.assertEqual(self.status(NOW + timedelta(minutes=6)).state, State.UNKNOWN)
        self.assert_replacement_refused(NOW + timedelta(minutes=6))
        with self.assertRaises(RecoveryRefused):
            self.recovery.clear(
                self.a1,
                ev.ClearingBasis.COMPLETED,
                NOW + timedelta(minutes=6),
                session_urls=(adapter.sessions_created[0].url,),
            )
        self.assert_replacement_refused(NOW + timedelta(minutes=7))

    def test_ac2_lost_response_then_session_found_and_completed_unblocks(self):
        run, adapter = self.lost_launch_with_session()
        url = adapter.sessions_created[0].url
        self.recovery.record_launch_finding(
            run, Finding.SESSION_FOUND, (url,), "routine run list", NOW + timedelta(minutes=5)
        )
        st = self.status(NOW + timedelta(minutes=5))
        self.assertEqual(st.state, State.RUNNING)
        self.assertEqual(st.session_urls, (url,))
        self.assertFalse(st.writer_cleared)
        self.assert_replacement_refused(NOW + timedelta(minutes=5))
        self.recovery.clear(
            self.a1, ev.ClearingBasis.COMPLETED, NOW + timedelta(hours=1), session_urls=(url,)
        )
        self.assertTrue(self.status(NOW + timedelta(hours=1)).writer_cleared)
        self.assertEqual(self.block_codes(NOW + timedelta(hours=1)), set())
        self.assert_replacement_allowed(NOW + timedelta(hours=1))

    def test_ac2_lost_response_with_200_and_no_session_id_is_unknown(self):
        adapter = FakeRuntimeAdapter([FakeStep.lost_response(session_created=True, status=200)])
        run = self.reserve()
        self.gate.record_launch(run, adapter.launch(LaunchRequest(run, self.digest, "p")), NOW)
        self.assertEqual(self.status(NOW).state, State.UNKNOWN)
        self.assertEqual(self.status(NOW).session_urls, ())

    # --- AC3: operator reconciles and confirms the writer stopped ---

    def test_ac3_operator_reconciles_records_evidence_then_clears_before_replacement(self):
        run, adapter = self.lost_launch_with_session()
        url = adapter.sessions_created[0].url
        self.publish(self.pr(7, draft=True))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=10))
        self.assertEqual(rec.status.pull_requests, (7,))
        self.assertEqual(len(self.candidates_for_pr(7)), 1)
        candidate = self.candidates_for_pr(7)[0]
        self.assertEqual(candidate.run, run)
        self.assertEqual(candidate.data["candidate_commit"], SHA_A)
        self.assertEqual(candidate.data["branch"], self.a1.branch)
        self.assertGreaterEqual(len(self.kinds(PR_OBSERVED)), 1)
        # A PR on GitHub is not a stop.
        self.assertFalse(rec.status.writer_cleared)
        self.assert_replacement_refused(NOW + timedelta(minutes=10))
        self.recovery.record_launch_finding(
            run, Finding.SESSION_FOUND, (url,), "run list", NOW + timedelta(minutes=11)
        )
        findings = self.kinds(LAUNCH_RECONCILED)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].run, run)
        self.assert_replacement_refused(NOW + timedelta(minutes=12))
        stored = self.recovery.clear(
            self.a1, ev.ClearingBasis.TERMINATED, NOW + timedelta(minutes=13), session_urls=(url,)
        )
        self.assertEqual(stored.event.kind, ev.ATTEMPT_CLEARED)
        self.assertEqual(stored.event.run, run)
        self.assertEqual(stored.event.data["basis"], "terminated")
        self.assertTrue(self.status(NOW + timedelta(minutes=13)).writer_cleared)
        self.assert_replacement_allowed(NOW + timedelta(minutes=14))

    def test_ac3_finding_is_confirmed_at_the_terminal_with_the_run_as_code(self):
        run = self.dispatch(LOST)
        confirm = Recorder()
        recovery = self.make_recovery(confirm=confirm)
        recovery.record_launch_finding(run, Finding.SESSION_FOUND, (URL1,), "run list", NOW)
        self.assertEqual(confirm.codes, [str(run)])

    def test_ac3_finding_needs_the_right_number_of_urls(self):
        run = self.dispatch(LOST)
        cases = [
            (Finding.SESSION_FOUND, ()),
            (Finding.SESSION_FOUND, (URL1, URL2)),
            (Finding.DUPLICATES, (URL1,)),
            (Finding.DUPLICATES, ()),
            (Finding.NOT_FOUND, (URL1,)),
            (Finding.DUPLICATES, (URL1, URL1)),
        ]
        for finding, urls in cases:
            with self.subTest(finding=finding, urls=urls), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.record_launch_finding(run, finding, urls, "run list", NOW)

    def test_ac3_finding_only_for_a_run_whose_result_is_unknown(self):
        launched_run = self.dispatch(launched(1))
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.record_launch_finding(
                launched_run, Finding.SESSION_FOUND, (URL1,), "run list", NOW
            )
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.record_launch_finding(
                RunId(self.a2, 1), Finding.NOT_FOUND, (), "run list", NOW
            )

    def test_ac3_finding_refused_while_launch_is_still_in_flight(self):
        run = self.reserve()
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list", NOW)

    def test_ac3_finding_refused_for_not_launched_run(self):
        run = self.dispatch(not_launched(400))
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list", NOW)

    def test_ac3_duplicates_list_every_url_and_clearing_must_name_all(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(
            run, Finding.DUPLICATES, (URL1, URL2), "two sessions in run list", NOW
        )
        st = self.status(NOW)
        self.assertEqual(st.state, State.RUNNING)
        self.assertEqual(set(st.session_urls), {URL1, URL2})
        self.assertEqual(len(st.session_urls), 2)
        for urls in [(URL1,), (URL2,), (URL1, URL2, "https://claude.ai/code/cse_3")]:
            with self.subTest(urls=urls), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=urls)
        self.assert_replacement_refused(NOW)
        self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1, URL2))
        self.assertTrue(self.status(NOW).writer_cleared)
        self.assert_replacement_allowed(NOW + timedelta(minutes=1))

    def test_ac3_write_access_removed_is_checked_on_github(self):
        self.dispatch(launched(1))
        self.github.push_allowed = False
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.WRITE_ACCESS_REMOVED,
            NOW,
            note="gh api repos/.../collaborators shows the bot removed",
        )
        self.assertIn((REPO, BOT), self.github.push_checks)
        self.assertEqual(len(self.kinds(ev.ATTEMPT_CLEARED)), 1)
        self.assert_replacement_allowed(NOW + timedelta(minutes=1))

    # --- clear: every RecoveryRefused case in the spec ---

    def test_clear_refused_for_definite_not_launched(self):
        self.dispatch(not_launched(400))
        for basis, kw in [
            (ev.ClearingBasis.COMPLETED, {"session_urls": (URL1,)}),
            (ev.ClearingBasis.UNRESOLVED_ACCEPTED, {"note": "nothing"}),
        ]:
            with self.subTest(basis=basis), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(self.a1, basis, NOW, **kw)

    def test_clear_refused_for_never_reserved_attempt(self):
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))
        self.dispatch(launched(1))
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a2, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))

    def test_clear_refused_while_latest_fire_has_no_result(self):
        self.reserve()
        self.github.push_allowed = False
        for basis, kw in [
            (ev.ClearingBasis.UNRESOLVED_ACCEPTED, {"note": "gave up"}),
            (ev.ClearingBasis.WRITE_ACCESS_REMOVED, {"note": "bot removed"}),
            (ev.ClearingBasis.COMPLETED, {"session_urls": (URL1,)}),
        ]:
            with self.subTest(basis=basis), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(self.a1, basis, YOUNG, **kw)

    def test_clear_completed_refused_without_a_session_url_on_record(self):
        self.dispatch(LOST)
        for basis in (ev.ClearingBasis.COMPLETED, ev.ClearingBasis.TERMINATED):
            for urls in [(), (URL1,)]:
                with self.subTest(basis=basis, urls=urls), self.writes_nothing():
                    with self.assertRaises(RecoveryRefused):
                        self.recovery.clear(self.a1, basis, NOW, session_urls=urls)

    def test_clear_completed_refused_when_urls_differ_from_record(self):
        self.dispatch(launched(1))
        for basis in (ev.ClearingBasis.COMPLETED, ev.ClearingBasis.TERMINATED):
            for urls in [(), (URL2,), (URL1, URL2)]:
                with self.subTest(basis=basis, urls=urls), self.writes_nothing():
                    with self.assertRaises(RecoveryRefused):
                        self.recovery.clear(self.a1, basis, NOW, session_urls=urls)

    def test_clear_write_access_removed_refused_without_a_note(self):
        self.dispatch(launched(1))
        self.github.push_allowed = False
        for note in ["", "   ", "\n\t"]:
            with self.subTest(note=note), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(
                        self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note=note
                    )

    def test_clear_write_access_removed_refused_without_a_github_reader(self):
        self.dispatch(launched(1))
        recovery = self.make_recovery(github=None)
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            recovery.clear(self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note="removed")

    def test_clear_write_access_removed_refused_when_github_unreadable(self):
        self.dispatch(launched(1))
        self.github.push_error = GitHubUnreadable("gh: HTTP 502")
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note="gone")

    def test_clear_write_access_removed_refused_when_bot_can_still_push(self):
        self.dispatch(launched(1))
        self.github.push_allowed = True
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note="gone")
        self.assert_replacement_refused(NOW)

    def test_clear_unresolved_accepted_refused_when_a_session_url_is_on_record(self):
        self.dispatch(launched(1))
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(
                self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="accept the risk"
            )

    def test_clear_unresolved_accepted_refused_after_session_found(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.SESSION_FOUND, (URL1,), "run list", NOW)
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="accept")

    def test_clear_unresolved_accepted_refused_without_a_not_found_finding(self):
        self.dispatch(LOST)
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.clear(self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="accept")

    def test_clear_unresolved_accepted_refused_without_a_note(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        for note in ["", "  "]:
            with self.subTest(note=note), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(
                        self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note=note
                    )

    def test_clear_unresolved_accepted_after_not_found_with_note(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.UNRESOLVED_ACCEPTED,
            NOW + timedelta(minutes=1),
            note="ADR 0001 s4 exception: accept that it may exist",
        )
        self.assertEqual(self.block_codes(NOW + timedelta(minutes=1)), set())
        self.assert_replacement_allowed(NOW + timedelta(minutes=2))

    # --- NOT_FOUND never clears ---

    def test_not_found_finding_never_clears(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(
            run, Finding.NOT_FOUND, (), "run list empty", NOW + timedelta(minutes=1)
        )
        for at in (NOW + timedelta(minutes=2), NOW + timedelta(days=2)):
            st = self.status(at)
            self.assertEqual(st.state, State.UNKNOWN)
            self.assertFalse(st.writer_cleared)
            self.assertEqual(st.session_urls, ())
            self.assert_replacement_refused(at)
        self.reopen()
        self.assertEqual(self.status(NOW + timedelta(days=2)).state, State.UNKNOWN)
        self.assert_replacement_refused(NOW + timedelta(days=2))

    # --- clearings written around recovery's rules ---

    def test_direct_clearing_with_wrong_session_url_is_clearing_invalid(self):
        self.dispatch(launched(1))
        self.approvals.record_clearing(
            self.a1, ev.ClearingBasis.COMPLETED, URL2, NOW + timedelta(minutes=1)
        )
        self.assertIn("clearing-invalid", self.block_codes(NOW + timedelta(minutes=1)))
        self.assertFalse(self.status(NOW + timedelta(minutes=1)).writer_cleared)
        self.reopen()
        self.assertIn("clearing-invalid", self.block_codes(NOW + timedelta(minutes=2)))

    def test_direct_clearing_with_lookalike_session_url_is_clearing_invalid(self):
        for i, lookalike in enumerate(
            [
                "https://claude.ai/code/cse_1x",
                "https://claude.ai/code/cse_1/x",
                "https://claude.ai/code/cse_10",
                "https://claude.ai.evil.com/code/cse_1",
            ]
        ):
            with self.subTest(lookalike=lookalike):
                self.setUp()
                self.dispatch(launched(1))
                at = NOW + timedelta(minutes=i + 1)
                self.approvals.record_clearing(self.a1, ev.ClearingBasis.COMPLETED, lookalike, at)
                self.assertIn("clearing-invalid", self.block_codes(at))
                self.assertFalse(self.status(at).writer_cleared)

    def test_direct_unresolved_accepted_on_a_launched_attempt_is_clearing_invalid(self):
        self.dispatch(launched(1))
        self.approvals.record_clearing(
            self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, "accept", NOW + timedelta(minutes=1)
        )
        self.assertIn("clearing-invalid", self.block_codes(NOW + timedelta(minutes=1)))

    def test_direct_completed_clearing_after_not_found_is_clearing_invalid(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        self.approvals.record_clearing(self.a1, ev.ClearingBasis.COMPLETED, URL1, NOW)
        self.assertIn("clearing-invalid", self.block_codes(NOW))
        self.assertFalse(self.status(NOW).writer_cleared)

    def test_valid_clearing_is_not_reported(self):
        self.dispatch(launched(1))
        self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))
        self.assertNotIn("clearing-invalid", self.block_codes(NOW))
        st = self.status(NOW)
        self.assertTrue(st.writer_cleared)
        self.assertEqual(st.session_urls, (URL1,))

    # --- writer_cleared, attempt_status and unexplained work (spec update) ---

    def test_writer_cleared_after_write_access_removed(self):
        self.dispatch(launched(1))
        self.github.push_allowed = False
        self.recovery.clear(self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note="removed")
        self.assertTrue(self.status(NOW).writer_cleared)

    def test_writer_cleared_after_unresolved_accepted_exception(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        self.assertFalse(self.status(NOW).writer_cleared)
        self.recovery.clear(self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="exception")
        self.assertTrue(self.status(NOW).writer_cleared)

    def test_writer_cleared_for_definite_not_launched(self):
        self.dispatch(not_launched(400))
        st = self.status(NOW)
        self.assertEqual(st.state, State.AWAITING_HUMAN)
        self.assertTrue(st.writer_cleared)

    def test_attempt_status_matches_task_status(self):
        self.dispatch(launched(1))
        self.assertEqual(self.recovery.attempt_status(self.a1, NOW), self.status(NOW))

    def test_reconcile_reports_how_many_events_it_recorded(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7))
        before = self.seq()
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertGreater(rec.recorded, 0)
        self.assertEqual(rec.recorded, self.seq() - before)
        self.assertEqual(self.recovery.reconcile(self.a1, NOW + timedelta(minutes=31)).recorded, 0)

    def test_unexplained_work_for_a_not_launched_attempt_is_a_block(self):
        self.dispatch(not_launched(400))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertIn("unexplained-work", self.block_codes(NOW + timedelta(minutes=30)))

    def test_plain_abandoned_record_counts_as_failed(self):
        self.dispatch(launched(1))
        self.append(records.attempt_abandoned(self.a1, "gave up", "Rolando", NOW))
        st = self.status(NOW)
        self.assertEqual(st.state, State.FAILED)
        self.assertFalse(st.writer_cleared)

    # --- restoring the bot's write access ---

    def test_may_restore_bot_access_only_after_a_completed_clearing(self):
        self.dispatch(launched(1))
        ok, reasons = self.recovery.may_restore_bot_access(NOW)
        self.assertFalse(ok)
        self.assertTrue(reasons)
        self.github.push_allowed = False
        self.recovery.clear(
            self.a1, ev.ClearingBasis.WRITE_ACCESS_REMOVED, NOW, note="collaborator removed"
        )
        ok, reasons = self.recovery.may_restore_bot_access(NOW + timedelta(minutes=1))
        self.assertFalse(ok)
        self.assertTrue(reasons)
        self.assertTrue(any(str(self.a1) in r for r in reasons), reasons)
        self.reopen()
        self.assertFalse(self.recovery.may_restore_bot_access(NOW + timedelta(minutes=1))[0])
        self.recovery.clear(
            self.a1, ev.ClearingBasis.COMPLETED, NOW + timedelta(hours=1), session_urls=(URL1,)
        )
        ok, _ = self.recovery.may_restore_bot_access(NOW + timedelta(hours=1))
        self.assertTrue(ok)

    def test_may_restore_bot_access_false_with_an_unresolved_accepted_attempt(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        self.recovery.clear(self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="exception")
        self.assertFalse(self.recovery.may_restore_bot_access(NOW)[0])

    def test_may_restore_bot_access_ignores_attempts_that_never_launched(self):
        self.dispatch(not_launched(400))
        self.assertTrue(self.recovery.may_restore_bot_access(NOW)[0])

    def test_may_restore_bot_access_false_with_an_invalid_completed_clearing(self):
        self.dispatch(launched(1))
        self.approvals.record_clearing(self.a1, ev.ClearingBasis.COMPLETED, URL2, NOW)
        self.assertFalse(self.recovery.may_restore_bot_access(NOW)[0])

    # --- AC4: PR created but the candidate was never saved ---

    def test_ac4_reconcile_records_the_existing_pr_once(self):
        run = self.dispatch(launched(1))
        self.publish(self.pr(7))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.pull_requests, (7,))
        found = self.candidates_for_pr(7)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].run, run)
        self.assertEqual(found[0].data["pr_url"], f"https://github.com/{REPO}/pull/7")
        self.assertEqual(found[0].data["candidate_commit"], SHA_A)
        with self.writes_nothing():
            again = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=31))
        self.assertEqual(again.status.pull_requests, (7,))
        self.reopen()
        with self.writes_nothing():
            self.recovery.reconcile(self.a1, NOW + timedelta(minutes=32))
        self.assertEqual(len(self.candidates_for_pr(7)), 1)

    def test_ac4_reconcile_records_a_new_head_commit_as_a_new_candidate(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.publish(self.pr(7, head_sha=SHA_B), branch_sha=SHA_B)
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=40))
        commits = [e.data["candidate_commit"] for e in self.candidates_for_pr(7)]
        self.assertEqual(commits, [SHA_A, SHA_B])

    def test_ac4_branch_without_a_pr_is_recorded_without_inventing_one(self):
        self.dispatch(launched(1))
        self.github.branches[self.a1.branch] = SHA_A
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.pull_requests, ())
        cands = self.kinds(ledger_kinds.CANDIDATE)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].data["candidate_commit"], SHA_A)
        self.assertNotIn("pr_number", cands[0].data)
        with self.writes_nothing():
            self.recovery.reconcile(self.a1, NOW + timedelta(minutes=31))

    def test_ac4_reconcile_on_empty_github_records_nothing(self):
        self.dispatch(launched(1))
        with self.writes_nothing():
            rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.state, State.RUNNING)
        self.assertEqual(rec.status.pull_requests, ())

    # --- edge: PRs that must not count as the attempt's candidate ---

    def assert_foreign_pr_ignored(self, pull, *, branch_sha=None):
        self.dispatch(launched(1))
        self.github.pulls = [pull]
        if branch_sha:
            self.github.branches[self.a1.branch] = branch_sha
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.pull_requests, ())
        self.assertTrue(rec.warnings, "a lookalike PR must be reported as a warning")
        self.assertEqual(self.candidates_for_pr(pull.number), [])
        self.assertNotIn(rec.status.state, FINISHED)
        self.assertNotEqual(self.recovery.status(self.task, NOW).state, State.MERGED)

    def test_edge_pr_from_wrong_author_is_a_warning_not_a_candidate(self):
        self.assert_foreign_pr_ignored(
            self.pr(8, author="mallory", merged=True, state="closed", merge_commit=SHA_MERGE)
        )

    def test_edge_pr_with_wrong_title_digest_is_a_warning_not_a_candidate(self):
        title = f"[{self.task} a1 {'0' * 12}] Filter books by status"
        self.assert_foreign_pr_ignored(
            self.pr(9, title=title, merged=True, state="closed", merge_commit=SHA_MERGE)
        )

    def test_edge_pr_from_a_fork_is_a_warning_not_a_candidate(self):
        self.assert_foreign_pr_ignored(
            self.pr(
                10,
                head_repo="mallory/factory-pilot-demo",
                merged=True,
                state="closed",
                merge_commit=SHA_MERGE,
            )
        )

    def test_edge_pr_from_a_deleted_fork_is_a_warning_not_a_candidate(self):
        self.assert_foreign_pr_ignored(self.pr(11, head_repo=None))

    def test_edge_pr_with_attempt_marker_on_another_branch_is_a_warning(self):
        self.assert_foreign_pr_ignored(
            self.pr(12, head_branch="claude/random-xyz", merged=True, state="closed")
        )

    def test_edge_pr_for_another_attempt_on_this_branch_is_a_warning(self):
        title = f"{self.a2.pr_title_marker(self.digest)} Filter books by status"
        self.assert_foreign_pr_ignored(self.pr(13, title=title))

    def test_edge_pr_without_marker_on_this_branch_is_a_warning(self):
        self.assert_foreign_pr_ignored(self.pr(14, title="Filter books by status"))

    def test_edge_marker_not_at_the_start_of_the_title_does_not_match(self):
        title = f"Fix: {self.a1.pr_title_marker(self.digest)} Filter books by status"
        self.assert_foreign_pr_ignored(self.pr(15, title=title))

    def test_edge_matching_pr_counts_beside_a_foreign_one(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7), self.pr(8, author="mallory"))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.pull_requests, (7,))
        self.assertTrue(rec.warnings)
        self.assertEqual(self.candidates_for_pr(8), [])

    # --- AC5: open, draft, failing and closed PRs stay unfinished ---

    def test_ac5_draft_pr_is_verifying_not_finished(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, draft=True))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.state, State.VERIFYING)
        self.assertNotIn(rec.status.state, FINISHED)
        self.assertFalse(rec.status.writer_cleared)
        self.assertEqual(self.recovery.status(self.task, NOW).state, State.VERIFYING)

    def test_ac5_open_pr_with_pending_checks_stays_verifying(self):
        run = self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.checks(run, SHA_A, "success", "pending")
        self.assertEqual(self.status(NOW + timedelta(minutes=31)).state, State.VERIFYING)

    def test_ac5_open_pr_with_failing_checks_awaits_a_human_and_is_not_finished(self):
        run = self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.checks(run, SHA_A, "success", "failure")
        st = self.status(NOW + timedelta(minutes=31))
        self.assertEqual(st.state, State.AWAITING_HUMAN)
        self.assertNotIn(st.state, FINISHED)
        self.assertFalse(st.writer_cleared)

    def test_ac5_checks_for_an_older_commit_do_not_count(self):
        run = self.dispatch(launched(1))
        self.publish(self.pr(7, head_sha=SHA_B), branch_sha=SHA_B)
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.checks(run, SHA_A, "failure")
        self.assertEqual(self.status(NOW + timedelta(minutes=31)).state, State.VERIFYING)

    def test_ac5_green_checks_are_not_a_merge(self):
        run = self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.checks(run, SHA_A, "success", "success")
        st = self.status(NOW + timedelta(minutes=31))
        self.assertEqual(st.state, State.AWAITING_HUMAN)
        self.assertNotIn(st.state, FINISHED)

    def test_ac5_closed_unmerged_pr_awaits_an_explicit_decision(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, state="closed"))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.state, State.AWAITING_HUMAN)
        self.assertNotIn(rec.status.state, FINISHED)
        for later in (NOW + timedelta(hours=5), NOW + timedelta(days=5)):
            self.assertEqual(self.status(later).state, State.AWAITING_HUMAN)
        self.recovery.close_attempt(
            self.a1, State.FAILED, "PR closed unmerged: wrong approach", NOW + timedelta(hours=6)
        )
        abandoned = self.kinds(ledger_kinds.ATTEMPT_ABANDONED)
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].data["outcome"], "failed")
        st = self.status(NOW + timedelta(hours=6))
        self.assertEqual(st.state, State.FAILED)
        self.assertFalse(st.writer_cleared)  # closing does not clear the writer
        self.assert_replacement_refused(NOW + timedelta(hours=6))

    def test_ac5_close_attempt_canceled(self):
        self.dispatch(launched(1))
        self.recovery.close_attempt(self.a1, State.CANCELED, "no longer needed", NOW)
        self.assertEqual(self.recovery.status(self.task, NOW).state, State.CANCELED)
        self.assertEqual(self.kinds(ledger_kinds.ATTEMPT_ABANDONED)[0].data["outcome"], "canceled")

    def test_ac5_close_attempt_refuses_other_outcomes_and_blank_reasons(self):
        self.dispatch(launched(1))
        for outcome in (State.MERGED, State.RUNNING, State.AWAITING_HUMAN, State.READY):
            with self.subTest(outcome=outcome), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.close_attempt(self.a1, outcome, "done", NOW)
        for reason in ("", "   "):
            with self.subTest(reason=reason), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.close_attempt(self.a1, State.FAILED, reason, NOW)

    def test_ac5_close_attempt_confirms_at_the_terminal(self):
        self.dispatch(launched(1))
        confirm = Recorder()
        self.make_recovery(confirm=confirm).close_attempt(self.a1, State.FAILED, "bad", NOW)
        self.assertEqual(len(confirm.codes), 1)

    def test_ac5_merged_pr_is_accepted_merged(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, state="closed", merged=True, merge_commit=SHA_MERGE))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.state, State.MERGED)
        task = self.recovery.status(self.task, NOW + timedelta(minutes=31))
        self.assertEqual(task.state, State.MERGED)
        # Reading it is not enough: only a confirmation from a fresh read frees the lane.
        self.assertFalse(rec.status.writer_cleared)
        with self.writes_nothing(), self.assertRaises(RecoveryRefused):
            self.recovery.close_attempt(self.a1, State.FAILED, "oops", NOW + timedelta(hours=1))

    def test_ac5_merge_wins_over_an_earlier_close_decision(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, state="closed"))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.recovery.close_attempt(self.a1, State.CANCELED, "closed", NOW + timedelta(hours=1))
        self.assertEqual(self.status(NOW + timedelta(hours=1)).state, State.CANCELED)
        self.publish(self.pr(7, state="closed", merged=True, merge_commit=SHA_MERGE))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(hours=2))
        self.assertEqual(rec.status.state, State.MERGED)

    def test_ac5_merged_pr_wins_even_over_an_unknown_launch(self):
        self.dispatch(LOST)
        self.publish(self.pr(7, state="closed", merged=True, merge_commit=SHA_MERGE))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(rec.status.state, State.MERGED)
        self.assertFalse(rec.status.writer_cleared)
        at = NOW + timedelta(minutes=31)
        self.assertEqual(self.recovery.confirm_idle(self.a1, at), "PR #7 was merged")
        self.assertTrue(self.status(at).writer_cleared)
        self.assert_replacement_allowed(at)

    # --- Rolando's 2026-10-09 rule: the worker's PR frees the lane ------------

    def test_an_open_pr_frees_the_lane_after_30_quiet_minutes(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.assertIsNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=34)))
        self.assert_replacement_refused(NOW + timedelta(minutes=34))
        at = NOW + timedelta(minutes=35)
        reason = self.recovery.confirm_idle(self.a1, at)
        self.assertEqual(reason, "PR #7 has had no new commit for 30 minutes")
        st = self.status(at)
        self.assertTrue(st.writer_cleared)
        self.assertIn(reason, st.writer)
        self.assert_replacement_allowed(at)

    def test_merged_or_closed_frees_the_lane_at_once(self):
        for state, merged, words in (("closed", True, "merged"), ("closed", False, "closed")):
            with self.subTest(words=words):
                self.setUp()
                self.dispatch(launched(1))
                self.publish(self.pr(7, state=state, merged=merged, merge_commit=SHA_MERGE))
                at = NOW + timedelta(minutes=3)
                self.assertEqual(self.recovery.confirm_idle(self.a1, at), f"PR #7 was {words}")
                self.assert_replacement_allowed(at)

    def test_a_confirmation_goes_stale(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, state="closed", merged=True, merge_commit=SHA_MERGE))
        self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=1))
        self.assertTrue(self.status(NOW + timedelta(minutes=11)).writer_cleared)
        self.assertFalse(self.status(NOW + timedelta(minutes=12)).writer_cleared)
        self.assert_replacement_refused(NOW + timedelta(minutes=12))

    def test_a_failed_github_read_keeps_the_lane_held(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.github.read_error = GitHubUnreadable("gh: HTTP 502")
        at = NOW + timedelta(minutes=36)
        self.assertIsNone(self.recovery.confirm_idle(self.a1, at))
        self.assertEqual(self.kinds(rev_events.WORKER_IDLE), [])
        self.assertFalse(self.status(at).writer_cleared)
        self.assert_replacement_refused(at)

    def test_a_new_push_holds_the_lane_again(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.assertIsNotNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=40)))
        self.publish(self.pr(7, head_sha=SHA_B), branch_sha=SHA_B)
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=41))
        self.assertFalse(self.status(NOW + timedelta(minutes=42)).writer_cleared)
        self.assert_replacement_refused(NOW + timedelta(minutes=42))
        self.assertIsNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=70)))
        self.assertIsNotNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=71)))

    def test_a_push_after_the_pr_closed_holds_the_lane(self):
        for merged in (True, False):
            with self.subTest(merged=merged):
                self.setUp()
                self.dispatch(launched(1))
                closed = self.pr(7, state="closed", merged=merged, merge_commit=SHA_MERGE)
                self.publish(closed)
                self.assertIsNotNone(
                    self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=2))
                )
                # The worker pushes to its branch after the PR closed.
                self.publish(closed, branch_sha=SHA_B)
                at = NOW + timedelta(minutes=4)
                self.assertIsNone(self.recovery.confirm_idle(self.a1, at))
                self.assertFalse(self.status(at).writer_cleared)
                self.assert_replacement_refused(at)
                later = NOW + timedelta(minutes=34)
                self.assertIn("no new commit", self.recovery.confirm_idle(self.a1, later))

    def test_an_old_closed_pr_does_not_hide_an_active_one(self):
        self.dispatch(launched(1))
        old = self.pr(7, state="closed")
        self.publish(old, self.pr(8, head_sha=SHA_B), branch_sha=SHA_B)
        at = NOW + timedelta(minutes=10)
        self.assertIsNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(minutes=1)))
        self.assertIsNone(self.recovery.confirm_idle(self.a1, at))
        self.assert_replacement_refused(at)

    def test_no_pr_never_frees_the_lane_by_itself(self):
        self.dispatch(launched(1))
        self.publish()
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.assertIsNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(days=3)))
        self.assert_replacement_refused(NOW + timedelta(days=3))

    def test_a_pr_without_the_markers_does_not_free_the_lane(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, title="Filter books by status", state="closed"))
        self.assertIsNone(self.recovery.confirm_idle(self.a1, NOW + timedelta(hours=2)))
        self.assertFalse(self.status(NOW + timedelta(hours=2)).writer_cleared)

    def test_ac5_pr_state_changes_are_observed_once_each(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, draft=True))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=10))
        n1 = len(self.kinds(PR_OBSERVED))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=11))
        self.assertEqual(len(self.kinds(PR_OBSERVED)), n1)
        self.publish(self.pr(7, draft=False))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=12))
        self.assertEqual(len(self.kinds(PR_OBSERVED)), n1 + 1)

    # --- AC6 / AC8: silence is not termination; restart with a running worker ---

    def test_ac6_ac8_still_running_worker_stays_running_through_silence_and_restart(self):
        run = self.dispatch(launched(1))
        self.github.branches[self.a1.branch] = SHA_A
        times = [NOW + timedelta(hours=h) for h in (1, 6, 30, 24 * 9)]
        before = {}
        for at in times:
            st = self.status(at)
            self.assertEqual(st.state, State.RUNNING, at)
            self.assertFalse(st.writer_cleared, at)
            self.assertEqual(st.latest_run, run)
            self.assertEqual(st.session_urls, (URL1,))
            with self.writes_nothing():
                self.assertEqual(self.recovery.recover(at), [])
            self.assertNotIn("launch-interrupted", self.block_codes(at))
            before[at] = self.recovery.status(self.task, at)
        self.reopen()
        for at in times:
            self.assertEqual(self.recovery.status(self.task, at), before[at])
        self.assert_replacement_refused(times[-1])

    def test_ac6_branch_pushes_and_quiet_branch_do_not_stop_the_writer(self):
        self.dispatch(launched(1))
        self.github.branches[self.a1.branch] = SHA_A
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.github.branches[self.a1.branch] = SHA_B
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=20))
        rec = self.recovery.reconcile(self.a1, NOW + timedelta(hours=12))
        self.assertEqual(rec.status.state, State.RUNNING)
        self.assertFalse(rec.status.writer_cleared)

    def test_ac6_long_unknown_stays_unknown(self):
        self.dispatch(LOST)
        st = self.status(NOW + timedelta(days=30))
        self.assertEqual(st.state, State.UNKNOWN)
        self.assertFalse(st.writer_cleared)

    # --- AC8: repeated launch request ---

    def test_ac8_repeated_launch_request_is_refused_and_status_keeps_the_record(self):
        run = self.dispatch(launched(1))
        before = self.recovery.status(self.task, NOW + timedelta(minutes=1))
        verdict = self.approvals.check(self.contract, NOW + timedelta(minutes=1))
        self.assertFalse(verdict.approved)
        with self.assertRaises(DispatchRefused):
            self.gate.reserve(self.task, self.digest, NOW + timedelta(minutes=1))
        after = self.recovery.status(self.task, NOW + timedelta(minutes=1))
        self.assertEqual(after, before)
        self.assertEqual(after.state, State.RUNNING)
        self.assertEqual(after.attempts[-1].latest_run, run)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    def test_ac8_repeated_launch_request_while_dispatching_is_refused(self):
        run = self.reserve()
        with self.assertRaises(DispatchRefused):
            self.gate.reserve(self.task, self.digest, NOW + timedelta(seconds=5))
        st = self.status(NOW + timedelta(seconds=5))
        self.assertEqual(st.state, State.DISPATCHING)
        self.assertEqual(st.latest_run, run)
        self.assertEqual(len(self.kinds(ev.FIRE_INTENT)), 1)

    # --- AC7: everything survives a restart; nothing depends on memory ---

    def test_ac7_status_is_identical_before_and_after_restart(self):
        run1 = self.dispatch(LOST)
        self.recovery.record_launch_finding(run1, Finding.NOT_FOUND, (), "run list empty", NOW)
        self.recovery.clear(self.a1, ev.ClearingBasis.UNRESOLVED_ACCEPTED, NOW, note="exception")
        self.approvals.authorize_repair(self.contract, 2, "attempt 1 lost", NOW)
        run2 = self.dispatch(launched(2), at=NOW + timedelta(minutes=1))
        self.publish(self.pr(21, attempt=self.a2, draft=True), attempt=self.a2)
        self.recovery.reconcile(self.a2, NOW + timedelta(minutes=30))
        at = NOW + timedelta(hours=1)
        before = self.recovery.status(self.task, at)
        blocks_before = self.recovery.blocks(at)
        restore_before = self.recovery.may_restore_bot_access(at)
        self.reopen()
        self.assertEqual(self.recovery.status(self.task, at), before)
        self.assertEqual(self.recovery.blocks(at), blocks_before)
        self.assertEqual(self.recovery.may_restore_bot_access(at), restore_before)
        self.assertEqual(before.state, State.VERIFYING)
        self.assertEqual([a.attempt for a in before.attempts], [self.a1, self.a2])
        self.assertEqual(before.attempts[-1].latest_run, run2)
        self.assertEqual(before.attempts[-1].pull_requests, (21,))

    def test_ac7_approval_counters_and_decisions_survive_restart(self):
        self.dispatch(launched(1))
        self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))
        self.reopen()
        self.assertTrue(self.status(NOW).writer_cleared)
        self.approvals.authorize_repair(self.contract, 2, "CI red", NOW + timedelta(minutes=1))
        self.reopen()
        self.assert_replacement_allowed(NOW + timedelta(minutes=2))
        self.assertEqual(len(self.kinds(ev.ATTEMPT_RESERVED)), 2)

    def test_ac7_recovery_holds_no_state_between_calls(self):
        self.dispatch(launched(1))
        fresh = self.make_recovery()
        self.publish(self.pr(7))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        self.assertEqual(
            fresh.status(self.task, NOW + timedelta(minutes=31)),
            self.recovery.status(self.task, NOW + timedelta(minutes=31)),
        )

    # --- release status is separate from the task's state ---

    def test_release_status_is_separate_from_state(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, state="closed", merged=True, merge_commit=SHA_MERGE))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        task = self.recovery.status(self.task, NOW + timedelta(hours=1))
        self.assertEqual(task.release, "not recorded")
        self.recovery.record_release(
            self.task, "released", "Release run 123 approved", NOW + timedelta(hours=2)
        )
        task = self.recovery.status(self.task, NOW + timedelta(hours=2))
        self.assertEqual(task.state, State.MERGED)
        self.assertIn("released", task.release)
        self.assertNotIn("release-failed", task.release)
        self.recovery.record_release(
            self.task, "release-failed", "Pages deploy failed", NOW + timedelta(hours=3)
        )
        task = self.recovery.status(self.task, NOW + timedelta(hours=3))
        self.assertEqual(task.state, State.MERGED)
        self.assertIn("release-failed", task.release)
        self.assertEqual(len(self.kinds(RELEASE_STATUS)), 2)
        self.reopen()
        self.assertEqual(self.recovery.status(self.task, NOW + timedelta(hours=3)), task)

    def test_release_does_not_make_an_unmerged_task_merged(self):
        self.dispatch(launched(1))
        self.publish(self.pr(7, draft=True))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=30))
        try:
            self.recovery.record_release(self.task, "released", "x", NOW + timedelta(hours=1))
        except RecoveryRefused:
            pass
        self.assertEqual(
            self.recovery.status(self.task, NOW + timedelta(hours=1)).state, State.VERIFYING
        )

    def test_release_refuses_unknown_status_and_blank_evidence(self):
        self.dispatch(launched(1))
        for status, evidence in [("deployed", "x"), ("", "x"), ("released", "  ")]:
            with self.subTest(status=status, evidence=evidence), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.record_release(self.task, status, evidence, NOW)

    # --- edge: lookalike session URLs ---

    def test_edge_lookalike_urls_never_match_the_recorded_session(self):
        self.dispatch(launched(1))
        for urls in [
            ("https://claude.ai/code/cse_1x",),
            ("https://claude.ai/code/cse_1/",),
            ("https://claude.ai/code/cse_1 ",),
            (" https://claude.ai/code/cse_1",),
            ("https://claude.ai/code/cse_1 trailing text",),
            ("http://claude.ai/code/cse_1",),
            ("https://claude.ai/code/CSE_1",),
            (URL1, URL1),
        ]:
            with self.subTest(urls=urls), self.writes_nothing():
                with self.assertRaises(RecoveryRefused):
                    self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=urls)
        self.recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))

    def test_edge_lookalike_urls_in_a_finding_are_refused(self):
        run = self.dispatch(LOST)
        for url in [
            "https://claude.ai/code/cse_1/x",
            "https://claude.ai/code/cse_1 trailing",
            "https://claude.ai/code/",
            "https://evil.example/code/cse_1",
            "claude.ai/code/cse_1",
        ]:
            with self.subTest(url=url), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.record_launch_finding(
                        run, Finding.SESSION_FOUND, (url,), "run list", NOW
                    )

    def test_edge_finding_needs_how_it_was_checked(self):
        run = self.dispatch(LOST)
        for how in ("", "   "):
            with self.subTest(how=how), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    self.recovery.record_launch_finding(run, Finding.NOT_FOUND, (), how, NOW)

    # --- edge: confirm at the terminal ---

    def test_edge_declined_confirm_writes_nothing(self):
        run = self.dispatch(LOST)
        declined = self.make_recovery(confirm=no)
        with self.writes_nothing(), self.assertRaises(REFUSED):
            declined.record_launch_finding(run, Finding.NOT_FOUND, (), "run list empty", NOW)
        with self.writes_nothing(), self.assertRaises(REFUSED):
            declined.close_attempt(self.a1, State.FAILED, "lost", NOW)

    def test_edge_declined_approval_confirm_writes_no_clearing(self):
        self.dispatch(launched(1))
        approvals = Approvals(self.store, KEY, confirm=no, os_user="rolando")
        recovery = self.make_recovery(approvals=approvals)
        with self.writes_nothing(), self.assertRaises(REFUSED):
            recovery.clear(self.a1, ev.ClearingBasis.COMPLETED, NOW, session_urls=(URL1,))
        self.assertFalse(self.status(NOW).writer_cleared)

    # --- edge: naive datetimes ---

    def test_edge_naive_datetimes_are_refused_and_write_nothing(self):
        run = self.dispatch(LOST)
        self.publish(self.pr(7))
        naive = NOW.replace(tzinfo=None)
        calls = {
            "recover": lambda: self.recovery.recover(naive),
            "status": lambda: self.recovery.status(self.task, naive),
            "blocks": lambda: self.recovery.blocks(naive),
            "reconcile": lambda: self.recovery.reconcile(self.a1, naive),
            "finding": lambda: self.recovery.record_launch_finding(
                run, Finding.NOT_FOUND, (), "run list", naive
            ),
            "close": lambda: self.recovery.close_attempt(self.a1, State.FAILED, "x", naive),
            "release": lambda: self.recovery.record_release(self.task, "released", "x", naive),
            "restore": lambda: self.recovery.may_restore_bot_access(naive),
            "late": lambda: self.recovery.record_late_result(run, launched(5), naive),
        }
        for name, call in calls.items():
            with self.subTest(call=name), self.writes_nothing():
                with self.assertRaises(BAD_INPUT):
                    call()

    def test_edge_naive_clear_is_refused(self):
        self.dispatch(launched(1))
        with self.writes_nothing(), self.assertRaises((*BAD_INPUT, ApprovalRefused)):
            self.recovery.clear(
                self.a1, ev.ClearingBasis.COMPLETED, NOW.replace(tzinfo=None), session_urls=(URL1,)
            )

    # --- edge: late result rules ---

    def test_edge_late_result_refused_when_run_has_no_unknown_result(self):
        run = self.dispatch(launched(1))
        with self.writes_nothing(), self.assertRaises(BAD_INPUT):
            self.recovery.record_late_result(run, launched(5), NOW)
        young = RunId(self.a1, 1)
        self.setUp()
        self.reserve()
        with self.writes_nothing(), self.assertRaises(BAD_INPUT):
            self.recovery.record_late_result(young, launched(5), NOW)

    def test_edge_late_not_launched_result_never_clears(self):
        run = self.reserve()
        self.recovery.recover(STALE)
        self.recovery.record_late_result(run, not_launched(400), STALE)
        st = self.status(STALE)
        self.assertFalse(st.writer_cleared)
        self.assertEqual(st.state, State.UNKNOWN)
        self.assert_replacement_refused(STALE)

    # --- regressions from the adversarial review ---

    def test_review_editing_the_pr_title_does_not_hide_unexplained_work(self):
        self.dispatch(not_launched(400))
        self.publish(self.pr())
        self.recovery.reconcile(self.a1, NOW)
        self.assertIn("unexplained-work", self.block_codes(NOW))
        self.publish(self.pr(title="Filter books by status"))
        self.recovery.reconcile(self.a1, NOW)
        st = self.status(NOW)
        self.assertFalse(st.writer_cleared)
        self.assertIn("unexplained-work", self.block_codes(NOW))

    def test_review_a_url_in_the_clearing_note_is_not_vouched_for(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.SESSION_FOUND, [URL1], "run list", NOW)
        self.recovery.clear(
            self.a1,
            ev.ClearingBasis.COMPLETED,
            NOW,
            session_urls=[URL1],
            note=f"have not checked {URL2}, may be running",
        )
        self.recovery.record_launch_finding(
            run, Finding.DUPLICATES, [URL1, URL2], "run list again", NOW
        )
        st = self.status(NOW)
        self.assertFalse(st.writer_cleared)
        self.assertIn("clearing-invalid", self.block_codes(NOW))
        self.assertFalse(self.recovery.may_restore_bot_access(NOW)[0])

    def test_review_malformed_checks_record_does_not_crash_status(self):
        run = self.dispatch()
        self.publish(self.pr())
        self.recovery.reconcile(self.a1, NOW)
        bad = LedgerEvent(
            ledger_kinds.CHECKS, NOW, self.task, self.a1, run, {"revision": SHA_A, "results": ["x"]}
        )
        try:
            self.append(bad)
        except InvalidEvent:
            return  # the durable ledger refuses it outright
        self.assertEqual(self.status(NOW).state, State.VERIFYING)

    def test_review_malformed_pr_observation_does_not_break_reconcile(self):
        run = self.dispatch()
        self.append(LedgerEvent(PR_OBSERVED, NOW, self.task, self.a1, run, {"number": "x"}))
        self.publish(self.pr())
        result = self.recovery.reconcile(self.a1, NOW)
        self.assertGreater(result.recorded, 0)
        self.assertEqual(self.status(NOW).pull_requests, (7,))

    def test_review_a_merged_pr_stays_merged_when_its_title_is_edited(self):
        self.dispatch()
        merged = {"state": "closed", "merged": True, "merge_commit": "c" * 40}
        self.publish(self.pr(**merged))
        self.recovery.reconcile(self.a1, NOW)
        self.assertEqual(self.status(NOW).state, State.MERGED)
        self.publish(self.pr(title="x", **merged))
        self.recovery.reconcile(self.a1, NOW)
        self.assertEqual(self.status(NOW).state, State.MERGED)
        with self.assertRaises(RecoveryRefused):
            self.recovery.close_attempt(self.a1, State.FAILED, "no", NOW)

    # --- regressions from Rolando's review ---

    def test_review_a_merge_into_a_side_branch_is_not_accepted(self):
        self.dispatch()
        merged = {"state": "closed", "merged": True, "merge_commit": "c" * 40}
        for base in ({"base_branch": "side"}, {"base_repo": "someone/else"}):
            with self.subTest(base=base):
                self.publish(self.pr(**merged, **base))
                result = self.recovery.reconcile(self.a1, NOW)
                st = self.status(NOW)
                self.assertNotEqual(st.state, State.MERGED)
                self.assertEqual(st.pull_requests, ())
                self.assertTrue(any("targets" in w for w in result.warnings), result.warnings)
                self.assertEqual(self.candidates_for_pr(7), [])

    def test_review_an_older_github_read_cannot_undo_a_newer_merge(self):
        self.dispatch()
        merged = self.pr(state="closed", merged=True, merge_commit="c" * 40)
        newer = FakeGitHub()
        newer.branches[self.a1.branch] = SHA_A
        newer.pulls = [merged]
        other_process = self.make_recovery(github=newer)
        test = self

        class SlowGitHub(FakeGitHub):
            """Returns an open PR, but another reconcile records the merge meanwhile."""

            reads = 0

            def recent_pulls(self, repo):
                SlowGitHub.reads += 1
                if SlowGitHub.reads == 1:
                    other_process.reconcile(test.a1, NOW)
                    return [test.pr()]
                return [merged]

            def pulls_for_branch(self, repo, branch):
                return []

        slow = SlowGitHub()
        slow.branches[self.a1.branch] = SHA_A
        self.make_recovery(github=slow).reconcile(self.a1, NOW)
        self.assertEqual(SlowGitHub.reads, 2)
        self.assertEqual(self.status(NOW).state, State.MERGED)
        opened = [e for e in self.kinds(PR_OBSERVED) if e.data["state"] == "open"]
        self.assertEqual(opened, [])

    def test_review_reconcile_gives_up_when_records_keep_changing(self):
        run = self.dispatch()
        test = self

        class Busy(FakeGitHub):
            def recent_pulls(self, repo):
                test.append(
                    records.failure(
                        "probe", "another writer", NOW, run=run, task=test.task, attempt=test.a1
                    )
                )
                return [test.pr()]

        before = len(self.kinds(PR_OBSERVED))
        with self.assertRaises(RecoveryRefused):
            self.make_recovery(github=Busy()).reconcile(self.a1, NOW)
        self.assertEqual(len(self.kinds(PR_OBSERVED)), before)

    def test_review_a_recorded_merge_outlasts_a_later_open_snapshot(self):
        run = self.dispatch()
        merged = self.pr(state="closed", merged=True, merge_commit="c" * 40)
        self.publish(merged)
        self.recovery.reconcile(self.a1, NOW)
        stale = LedgerEvent(
            PR_OBSERVED,
            NOW,
            self.task,
            self.a1,
            run,
            dict(self.kinds(PR_OBSERVED)[-1].data) | {"state": "open", "merged": False},
        )
        self.append(stale)
        self.assertEqual(self.status(NOW).state, State.MERGED)


class SqliteRecoverySpecTests(RecoverySpecTests):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SqliteLedgerStore(Path(tmp.name) / "ledger.db")
        self.addCleanup(store.close)
        return store

    def restart(self, store):
        path = store.path
        store.close()
        reopened = SqliteLedgerStore(path)
        self.addCleanup(reopened.close)
        return reopened

    def test_every_recovery_record_passes_the_ledger_rules(self):
        run = self.dispatch(LOST)
        self.recovery.record_launch_finding(run, Finding.SESSION_FOUND, (URL1,), "run list", NOW)
        self.publish(self.pr(7, state="closed"))
        self.recovery.reconcile(self.a1, NOW + timedelta(minutes=5))
        self.recovery.close_attempt(self.a1, State.FAILED, "closed", NOW + timedelta(minutes=6))
        self.recovery.clear(
            self.a1, ev.ClearingBasis.COMPLETED, NOW + timedelta(minutes=7), session_urls=(URL1,)
        )
        for s in self.store.events():
            ledger_kinds.check(s.event)

    def test_sqlite_crash_after_reserve_in_a_child_process(self):
        """A real controller process reserves, then dies before recording a result."""
        child = (
            "import os, sys\n"
            "from datetime import datetime\n"
            "from controller.attempts import AttemptGate\n"
            "from controller.interfaces import ContractDigest, TaskId\n"
            "from controller.ledger import SqliteLedgerStore\n"
            "store = SqliteLedgerStore(sys.argv[1])\n"
            "run = AttemptGate(store).reserve(\n"
            "    TaskId(sys.argv[2]), ContractDigest(sys.argv[3]),\n"
            "    datetime.fromisoformat(sys.argv[4]))\n"
            "print(run, flush=True)\n"
            "os._exit(1)\n"
        )
        path = self.store.path
        done = subprocess.run(
            [sys.executable, "-c", child, str(path), str(self.task), self.digest.value]
            + [NOW.isoformat()],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        run = RunId(self.a1, 1)
        self.assertEqual(done.stdout.strip(), str(run))
        self.reopen()
        # The dead process's writer lock is free.
        with self.store.writer_lock():
            pass
        self.assertEqual(self.status(NOW + timedelta(minutes=1)).state, State.DISPATCHING)
        with self.assertRaises(DispatchRefused):
            self.gate.reserve(self.task, self.digest, NOW + timedelta(minutes=1))
        self.assertIn("launch-interrupted", self.block_codes(STALE))
        self.assertEqual(self.recovery.recover(STALE), [run])
        results = [e for e in self.kinds(ev.FIRE_RESULT) if e.run == run]
        self.assertEqual([r.data["outcome"] for r in results], ["launch-outcome-unknown"])
        self.assertEqual(self.status(STALE).state, State.UNKNOWN)
        self.assert_replacement_refused(STALE)

    def test_sqlite_killed_lock_holder_leaves_the_lock_free(self):
        """Stale lock: a controller killed while holding the writer lock."""
        child = (
            "import sys, time\n"
            "from controller.ledger import SqliteLedgerStore\n"
            "store = SqliteLedgerStore(sys.argv[1])\n"
            "with store.writer_lock():\n"
            "    print('held', flush=True)\n"
            "    time.sleep(120)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child, str(self.store.path)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        with self.assertRaises(LedgerLocked):
            with self.store.writer_lock():
                pass
        before = self.seq()
        with self.assertRaises(LedgerLocked):
            self.gate.reserve(self.task, self.digest, NOW)
        self.assertEqual(self.seq(), before)
        proc.kill()
        proc.wait(timeout=30)
        self.reopen()
        with self.store.writer_lock():
            pass
        run = self.dispatch(launched(1))
        self.assertEqual(run, RunId(self.a1, 1))
        self.assertEqual(self.status(NOW).state, State.RUNNING)


if __name__ == "__main__":
    unittest.main()
