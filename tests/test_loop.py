"""The ENG-145 loop: assess, Rolando's signed review inputs, and Loop end to end.

Everything runs on fakes: the in-memory ledger, a StaticKey, a scripted
routine adapter, recovery's fake GitHub reader and the fake GitHub in
github_world for the collector. Nothing touches the network, ``gh``, the
Keychain or a real start endpoint.
"""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval import ApprovalRefused, Approvals, StaticKey
from controller.approval.approval import _sign
from controller.attempts import AttemptGate
from controller.attempts.events import ClearingBasis
from controller.dispatch import Dispatcher
from controller.interfaces import AttemptId, LedgerEvent, TaskId
from controller.ledger import SqliteLedgerStore, kinds
from controller.loop import cli as loop_cli
from controller.loop.check import CHECK_NAME, CI_CHECK_NAME, assess
from controller.loop.collect import collect
from controller.loop.decisions import SCOPE, ReviewDecisions, Timer, time_entries
from controller.loop.loop import EXIT_READY, EXIT_STOPPED, EXIT_WAITING, Asker, Loop
from controller.recovery import PullRequest, Recovery
from redteam import fixtures as fx
from tests.github_world import ATTEMPT, NUMBER, PR_URL, REPO, RUN_ID, World
from tests.test_attempts import MemoryLedger, launched
from tests.test_dispatch import BOT, KEY, NOW, START_KEY, TRIG, FakeBase, ScriptedAdapter
from tests.test_recovery import FakeGitHub
from verify.criteria import Verdict

OTHER_KEY = StaticKey(b"o" * 32)
SESSION = "https://claude.ai/code/cse_1"


def collected(world=None):
    return collect(world or World(), fx.CONTRACT, fx.DIGEST, ATTEMPT, NUMBER)


# --- assess ---------------------------------------------------------------------


class AssessTests(unittest.TestCase):
    def test_honest_world_with_observation_and_clearance_is_ready(self):
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(),
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )
        self.assertTrue(a.ready, a.blockers)
        self.assertEqual(a.blockers, ())
        self.assertEqual(a.conclusion(), "success")
        self.assertFalse(a.waiting_on_review)
        self.assertEqual(a.owed_observations(fx.CONTRACT), ())
        self.assertEqual(a.open_flags(), ())

    def test_without_rolandos_inputs_is_action_required(self):
        a = assess(fx.CONTRACT, fx.DIGEST, collected())
        self.assertFalse(a.ready)
        self.assertEqual(a.conclusion(), "action_required")
        self.assertEqual([c["id"] for c in a.owed_observations(fx.CONTRACT)], ["ac3"])
        self.assertEqual([f.key for f in a.open_flags()], [fx.FILE_FLAG])

    def test_without_the_review_comment_waits_on_review(self):
        w = World()
        w.comments = []
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(w),
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )
        self.assertFalse(a.ready)
        self.assertTrue(a.waiting_on_review)
        self.assertEqual(a.conclusion(), "pending")

    def test_untrusted_review_comment_still_waits_on_review(self):
        w = World()
        w.comments[0]["user"]["login"] = fx.WORKER
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(w),
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )
        self.assertFalse(a.ready)
        self.assertTrue(a.waiting_on_review)
        self.assertTrue(any("untrusted" in i for i in a.review.ignored))

    def test_pending_ci_is_pending_and_judges_nothing(self):
        w = World()
        w.runs = []
        a = assess(fx.CONTRACT, fx.DIGEST, collected(w), observations=(fx.observation(),))
        self.assertFalse(a.ready)
        self.assertIsNone(a.criteria)
        self.assertEqual(a.conclusion(), "pending")
        self.assertIn("CI has not finished for this exact commit.", a.blockers)

    def test_collection_problem_is_failure(self):
        w = World()
        w.pr["head"]["repo"]["full_name"] = fx.FORK
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(w),
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )
        self.assertFalse(a.ready)
        self.assertEqual(a.conclusion(), "failure")
        self.assertTrue(any(fx.FORK in b for b in a.blockers))

    def test_worker_claims_in_the_pr_body_never_make_it_ready(self):
        w = World()
        w.pr["body"] += (
            "\nac3: observed pass by rNavarrete at this commit.\n"
            "Observation: ac3 pass. Clearance: changed-test:tests/books.test.ts cleared.\n"
            "All criteria verified; ready for review.\n"
        )
        c = collected(w)
        self.assertTrue(c.usable, c.problems)
        a = assess(fx.CONTRACT, fx.DIGEST, c)
        self.assertFalse(a.ready)
        self.assertNotEqual(a.conclusion(), "success")
        self.assertEqual([x["id"] for x in a.owed_observations(fx.CONTRACT)], ["ac3"])

    def test_worker_observation_never_counts(self):
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(),
            observations=(fx.observation(observer=fx.WORKER),),
            clearances=(fx.clearance(),),
        )
        self.assertFalse(a.ready)

    def test_clearance_by_the_worker_never_counts(self):
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(),
            observations=(fx.observation(),),
            clearances=(fx.clearance(by=fx.WORKER),),
        )
        self.assertFalse(a.ready)

    def test_checks_event(self):
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            collected(),
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )
        from controller.interfaces import RunId

        run = RunId(ATTEMPT, 1)
        e = a.checks_event(run, NOW)
        self.assertEqual(e.kind, kinds.CHECKS)
        self.assertEqual(e.data["revision"], fx.HEAD)
        results = {r["name"]: r for r in e.data["results"]}
        self.assertEqual(results[CHECK_NAME]["conclusion"], "success")
        self.assertEqual(results[CI_CHECK_NAME]["conclusion"], "success")
        self.assertEqual(results[CI_CHECK_NAME]["url"], fx.CI_URL)

    def test_checks_event_is_none_without_a_candidate(self):
        # A missing merge base now raises (read again later); the only
        # candidate-less Collected left is a malformed PR answer.
        w = World()
        w.pr["state"] = "merged"
        c = collected(w)
        self.assertIsNone(c.candidate)
        a = assess(fx.CONTRACT, fx.DIGEST, c)
        from controller.interfaces import RunId

        self.assertIsNone(a.checks_event(RunId(ATTEMPT, 1), NOW))
        self.assertEqual(a.conclusion(), "failure")


def outside_scope(world, path="package.json", text='{"name": "x"}\n'):
    """The worker also changed ``path``, outside the contract's permitted paths."""
    world.files.append({"filename": path, "status": "modified"})
    world.pr["changed_files"] = len(world.files)
    world.contents[(path, fx.HEAD)] = text.encode()
    world.contents[(path, fx.BASE)] = b"{}\n"


def changes_ci(world):
    """The worker changed the trusted workflow, and CI flagged it."""
    from tests.github_world import control_change, zipped

    outside_scope(world, ".github/workflows/ci.yml", "on: pull_request\njobs: {}\n")
    world.blobs[2] = zipped(
        "control-change",
        control_change(flagged=True, reasons=["changes .github/workflows/ci.yml"]),
    )


class ConclusionTests(unittest.TestCase):
    def assess(self, world, **kw):
        return assess(fx.CONTRACT, fx.DIGEST, collected(world), **kw)

    def test_only_rolandos_answers_missing_is_action_required(self):
        a = self.assess(World())
        self.assertTrue(a.only_rolando_missing)
        self.assertEqual(a.conclusion(), "action_required")

    def test_only_the_flag_missing_is_action_required(self):
        a = self.assess(World(), observations=(fx.observation(),))
        self.assertTrue(a.only_rolando_missing)
        self.assertEqual(a.conclusion(), "action_required")

    def test_only_the_observation_missing_is_action_required(self):
        a = self.assess(World(), clearances=(fx.clearance(),))
        self.assertEqual(a.conclusion(), "action_required")

    def test_scope_violation_is_failure_even_with_open_flags(self):
        w = World()
        outside_scope(w)
        a = self.assess(w)
        self.assertTrue(a.collected.usable, a.collected.problems)
        self.assertTrue(a.open_flags())
        self.assertFalse(a.only_rolando_missing)
        self.assertEqual(a.conclusion(), "failure")

    def test_scope_violation_stays_failure_with_all_his_answers(self):
        w = World()
        outside_scope(w)
        a = self.assess(
            w,
            observations=(fx.observation(),),
            clearances=tuple(fx.clearance(f.key) for f in self.assess(w).open_flags()),
        )
        self.assertFalse(a.ready)
        self.assertEqual(a.conclusion(), "failure")

    def test_ci_workflow_change_is_failure(self):
        w = World()
        changes_ci(w)
        a = self.assess(w)
        self.assertFalse(a.only_rolando_missing)
        self.assertEqual(a.conclusion(), "failure")

    def test_observed_fail_is_failure(self):
        a = self.assess(World(), observations=(fx.observation(verdict=Verdict.FAIL),))
        self.assertFalse(a.only_rolando_missing)
        self.assertEqual(a.conclusion(), "failure")

    def test_uncovered_criterion_is_failure_not_action_required(self):
        from tests.github_world import honest_review

        w = World()
        w.comments[0]["body"] = honest_review(links=[], proofs=[])
        a = self.assess(w)
        self.assertIsNotNone(a.review.url)
        self.assertEqual(a.conclusion(), "failure")

    def test_edited_review_is_waiting_on_review_not_ready(self):
        w = World()
        w.comments[0]["updated_at"] = "2026-10-08T15:30:00Z"
        a = self.assess(w, observations=(fx.observation(),), clearances=(fx.clearance(),))
        self.assertFalse(a.ready)
        self.assertTrue(a.waiting_on_review)
        self.assertFalse(a.only_rolando_missing)
        self.assertTrue(any(i.startswith("edited:") for i in a.review.ignored))


# --- ReviewDecisions and Timer ----------------------------------------------------


class DecisionsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.decisions = ReviewDecisions(self.store, KEY, os_user="rolando")
        self.cand = fx.candidate()

    def observe(self, cand=None, attempt=ATTEMPT, digest=fx.DIGEST, verdict=Verdict.PASS):
        self.decisions.observe(
            attempt, digest, cand or self.cand, "ac3", verdict, "saw it", "one browser", NOW
        )

    def clear(self, cand=None, attempt=ATTEMPT, digest=fx.DIGEST):
        self.decisions.clear(
            attempt, digest, cand or self.cand, fx.FILE_FLAG, "read books.test.ts", NOW
        )

    def test_round_trip_for_the_same_revision(self):
        self.observe()
        self.clear()
        (o,) = self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand)
        self.assertEqual((o.criterion, o.commit, o.base_commit), ("ac3", fx.HEAD, fx.MAIN))
        self.assertEqual(o.verdict, Verdict.PASS)
        self.assertEqual(o.observer, "rNavarrete")
        self.assertEqual((o.seen, o.limitations), ("saw it", "one browser"))
        (c,) = self.decisions.clearances(ATTEMPT, fx.DIGEST, self.cand)
        self.assertEqual((c.flag, c.commit, c.base_commit), (fx.FILE_FLAG, fx.HEAD, fx.MAIN))
        self.assertEqual(c.by, "rNavarrete")
        self.assertEqual(c.contract_digest, str(fx.DIGEST))
        self.assertEqual(c.note, "read books.test.ts")

    def test_records_are_signed_candidate_review_decisions(self):
        self.observe()
        (s,) = self.store.events()
        self.assertEqual(s.event.kind, kinds.HUMAN_DECISION)
        self.assertEqual(s.event.data["scope"], SCOPE)
        self.assertEqual(s.event.data["key_id"], KEY.key_id)
        self.assertIn("mac", s.event.data)

    def test_round_trip_makes_the_honest_world_ready(self):
        self.observe()
        self.clear()
        c = collected()
        a = assess(
            fx.CONTRACT,
            fx.DIGEST,
            c,
            observations=self.decisions.observations(ATTEMPT, fx.DIGEST, c.candidate),
            clearances=self.decisions.clearances(ATTEMPT, fx.DIGEST, c.candidate),
        )
        self.assertTrue(a.ready, a.blockers)

    def test_fail_observation_reads_back_as_fail(self):
        self.observe(verdict=Verdict.FAIL)
        (o,) = self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand)
        self.assertEqual(o.verdict, Verdict.FAIL)

    def test_unknown_verdict_is_refused(self):
        with self.assertRaises(ValueError):
            self.observe(verdict=Verdict.UNKNOWN)
        self.assertEqual(self.store.events(), [])

    def test_not_read_for_another_revision_attempt_or_digest(self):
        self.observe()
        self.clear()
        others = {
            "new push": (ATTEMPT, fx.DIGEST, replace(self.cand, head_commit=fx.NEW_HEAD)),
            "new base": (ATTEMPT, fx.DIGEST, replace(self.cand, base_commit=fx.NEW_MAIN)),
            "other repo": (ATTEMPT, fx.DIGEST, replace(self.cand, repository=fx.FORK)),
            "other attempt": (AttemptId(ATTEMPT.task, 2), fx.DIGEST, self.cand),
            "other digest": (ATTEMPT, contracts.digest(dict(fx.CONTRACT, goal="x")), self.cand),
        }
        for label, (attempt, digest, cand) in others.items():
            with self.subTest(label):
                self.assertEqual(self.decisions.observations(attempt, digest, cand), ())
                self.assertEqual(self.decisions.clearances(attempt, digest, cand), ())

    def test_new_push_makes_them_stale_in_assess(self):
        self.observe()
        self.clear()
        w = World()
        w.pr["head"]["sha"] = fx.NEW_HEAD
        cand = replace(self.cand, head_commit=fx.NEW_HEAD)
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, cand), ())
        self.assertEqual(self.decisions.clearances(ATTEMPT, fx.DIGEST, cand), ())

    def append(self, event):
        with self.store.writer_lock():
            self.store.append(event)

    def signed_record(self, key=KEY, commit=fx.HEAD):
        """A signed observation as ReviewDecisions writes it, for commit."""
        other = MemoryLedger()
        ReviewDecisions(other, key).observe(
            ATTEMPT,
            fx.DIGEST,
            replace(self.cand, head_commit=commit),
            "ac3",
            Verdict.PASS,
            "saw it",
            "one browser",
            NOW,
        )
        (s,) = other.events()
        return s.event

    def test_tampered_record_is_ignored(self):
        e = self.signed_record(commit=fx.OLD_HEAD)
        data = dict(e.data)
        data["binding"] = {**dict(e.data["binding"]), "commit": fx.HEAD}
        self.append(LedgerEvent(e.kind, e.at, e.task, e.attempt, e.run, data))
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_tampered_verdict_is_ignored(self):
        e = self.signed_record()
        data = dict(e.data)
        data["binding"] = {**dict(e.data["binding"]), "verdict": "fail"}
        self.append(LedgerEvent(e.kind, e.at, e.task, e.attempt, e.run, data))
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_untampered_copy_counts(self):
        # Control for the two tests above: the same path, no edit, counts.
        self.append(self.signed_record())
        self.assertEqual(len(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand)), 1)

    def test_record_signed_with_another_key_is_ignored(self):
        self.append(self.signed_record(key=OTHER_KEY))
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_unsigned_record_is_ignored(self):
        e = self.signed_record()
        data = {k: v for k, v in e.data.items() if k != "mac"}
        self.append(LedgerEvent(e.kind, e.at, e.task, e.attempt, e.run, data))
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_another_identity_is_ignored(self):
        other = ReviewDecisions(self.store, KEY, identity="someone")
        other.observe(ATTEMPT, fx.DIGEST, self.cand, "ac3", Verdict.PASS, "s", "l", NOW)
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_dispatch_approval_signed_record_is_not_an_observation(self):
        e = self.signed_record()
        data = dict(e.data)
        data.pop("mac")
        data["scope"] = "contract-dispatch"
        self.append(_sign(LedgerEvent(e.kind, e.at, e.task, e.attempt, e.run, data), KEY))
        self.assertEqual(self.decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())

    def test_review_decisions_are_not_a_dispatch_approval(self):
        self.observe()
        self.clear()
        asked = []
        approvals = Approvals(
            self.store, KEY, confirm=lambda s, c: asked.append(c) or False, os_user="rolando"
        )
        verdict = approvals.check(fx.CONTRACT, NOW)
        self.assertFalse(verdict.approved)
        self.assertIn("approval-missing", {b.code for b in verdict.blocks})
        self.assertEqual(asked, [])

    def test_secret_looking_text_the_ledger_redacts_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            store = SqliteLedgerStore(Path(d) / "ledger.db")
            self.addCleanup(store.close)
            decisions = ReviewDecisions(store, KEY)
            secret = "sk-ant-oat01-" + "S3cretStartKey_" * 4
            with self.assertRaises(ApprovalRefused):
                decisions.observe(
                    ATTEMPT, fx.DIGEST, self.cand, "ac3", Verdict.PASS, secret, "l", NOW
                )
            self.assertEqual(decisions.observations(ATTEMPT, fx.DIGEST, self.cand), ())


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.now = NOW

        def clock():
            t = self.now
            self.now += timedelta(seconds=90)
            return t

        self.timer = Timer(self.store, clock, task=TaskId("filter-by-status"))

    def times(self):
        return [s.event for s in self.store.events() if s.event.kind == kinds.HUMAN_TIME]

    def test_confirm_records_time_when_answered(self):
        confirm = self.timer.confirm(lambda summary, code: True)
        self.assertTrue(confirm("Approve dispatch\nmore", "abc123"))
        (e,) = self.times()
        self.assertEqual(e.data["minutes"], 1.5)
        self.assertEqual(e.data["activity"], "typed a code: Approve dispatch")
        self.assertEqual(e.data["entered_by"], "controller (timed prompt)")
        self.assertEqual(e.task, TaskId("filter-by-status"))

    def test_confirm_records_time_when_declined(self):
        confirm = self.timer.confirm(lambda summary, code: False)
        self.assertFalse(confirm("Approve", "abc"))
        self.assertEqual(len(self.times()), 1)

    def test_confirm_records_time_when_the_prompt_raises(self):
        def boom(summary, code):
            raise KeyboardInterrupt

        confirm = self.timer.confirm(boom)
        with self.assertRaises(KeyboardInterrupt):
            confirm("Approve", "abc")
        self.assertEqual(len(self.times()), 1)

    def test_confirm_passes_keywords_through(self):
        seen = {}

        def inner(summary, code, **kw):
            seen.update(kw)
            return True

        self.timer.confirm(inner)("s", "c", extra=1)
        self.assertEqual(seen, {"extra": 1})

    def test_zero_time_is_at_least_the_minimum(self):
        timer = Timer(self.store, lambda: NOW)
        timer.timed(lambda: None, "glance")
        (e,) = self.times()
        self.assertGreater(e.data["minutes"], 0)

    def test_not_interactive_records_nothing(self):
        timer = Timer(self.store, lambda: NOW, interactive=lambda: False)
        self.assertEqual(timer.timed(lambda: "answer", "a prompt"), "answer")
        self.assertTrue(timer.confirm(lambda s, c: True)("Approve", "abc"))
        self.assertEqual(self.times(), [])

    def test_not_interactive_still_raises_from_the_prompt(self):
        timer = Timer(self.store, lambda: NOW, interactive=lambda: False)

        def boom():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            timer.timed(boom, "a prompt")
        self.assertEqual(self.times(), [])

    def test_interactive_is_the_default(self):
        Timer(self.store, lambda: NOW).timed(lambda: None, "a prompt")
        self.assertEqual(len(self.times()), 1)

    def test_time_entries_csv(self):
        self.timer.record(5, "read the PR", "worker stuck", entered_by="Rolando")
        lines = time_entries(self.store)
        self.assertEqual(len(lines), 2)
        self.assertIn("read the PR", lines[1])
        self.assertIn("worker stuck", lines[1])
        self.assertIn("Rolando", lines[1])


# --- Loop end to end --------------------------------------------------------------


class TtyIn(io.StringIO):
    """Rolando's terminal: scripted lines, and it says it is a terminal."""

    def isatty(self):
        return True


class SpyRecovery(Recovery):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clears = []

    def clear(self, attempt, basis, now, **kw):
        self.clears.append((attempt, basis, tuple(kw.get("session_urls", ()))))
        return super().clear(attempt, basis, now, **kw)


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.now = NOW
        self.world = World()
        self.gh = FakeGitHub()
        self.adapter = ScriptedAdapter([launched(1)])
        self.said = []
        self.backups = []
        self.slept = 0
        self.on_sleep = []
        """Callables run one per sleep, to change the world between polls."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.reports = Path(tmp.name) / "reports"
        self.contract = fx.CONTRACT
        self.task = TaskId(self.contract["task_id"])
        AttemptGate(self.store).record_snapshot(
            NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1)
        )
        self.loop = self.build("")

    def clock(self):
        return self.now

    def back_up(self, now):
        """Records the last ledger entry each backup holds."""
        self.backups.append(max((s.seq for s in self.store.events()), default=0))

    def sleep(self, seconds):
        self.slept += 1
        self.now += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep.pop(0)()

    def build(self, answers: str):
        """A fresh controller on the same ledger, as a new process would make."""
        self.stdin = TtyIn(answers)
        timer = Timer(self.store, self.clock)
        confirm = timer.confirm(lambda summary, code: True)
        gate = AttemptGate(self.store)
        approvals = Approvals(self.store, KEY, confirm=confirm, os_user="rolando")
        self.recovery = SpyRecovery(
            self.store,
            approvals,
            self.gh,
            worker_logins=frozenset({BOT}),
            gate=gate,
            confirm=confirm,
        )
        dispatcher = Dispatcher(
            self.store,
            approvals,
            self.recovery,
            gate,
            FakeBase(),
            routine_id=TRIG,
            adapter=lambda trig, key: self.adapter,
            start_key=lambda trig: START_KEY,
            model_config_version="test",
            now=self.clock,
            sleep=lambda s: None,
        )
        return Loop(
            store=self.store,
            recovery=self.recovery,
            dispatcher=dispatcher,
            api=self.world,
            decisions=ReviewDecisions(self.store, KEY, os_user="rolando"),
            timer=timer,
            asker=Asker(self.stdin, io.StringIO()),
            reports=self.reports,
            now=self.clock,
            sleep=self.sleep,
            say=self.said.append,
            backup=self.back_up,
        )

    def run_loop(self, answers=None, wait=90):
        if answers is not None:
            self.loop = self.build(answers)
        return self.loop.run(self.contract, wait_minutes=wait)

    # --- world changes ---

    def open_pr(self, merged=False):
        pr = self.world.pr
        self.gh.pulls = [
            PullRequest(
                number=NUMBER,
                url=PR_URL,
                title=pr["title"],
                body=pr["body"],
                head_branch=pr["head"]["ref"],
                head_sha=pr["head"]["sha"],
                head_repo=REPO,
                base_repo=REPO,
                base_branch="main",
                author=BOT,
                state="closed" if merged else "open",
                draft=pr["draft"],
                merged=merged,
                merge_commit="9" * 40 if merged else None,
            )
        ]
        self.gh.branches[ATTEMPT.branch] = pr["head"]["sha"]
        if merged:
            self.world.pr["state"] = "closed"
            self.world.pr["merged_at"] = "2026-10-08T16:00:00Z"

    def text(self):
        return "\n".join(self.said)

    def events(self, kind):
        return [s.event for s in self.store.events() if s.event.kind == kind]

    def checks(self):
        return [e for e in self.events(kinds.CHECKS) if e.data.get("revision") == fx.HEAD]


class LoopTests(LoopCase):
    def setUp(self):
        super().setUp()
        self.saved_runs, self.saved_comments = self.world.runs, self.world.comments
        self.world.runs, self.world.comments = [], []

    def ci_done(self):
        self.world.runs = self.saved_runs

    def review_posted(self):
        self.world.comments = self.saved_comments

    def test_a_launch_to_ready_then_b_rerun_fires_nothing(self):
        self.on_sleep = [
            self.open_pr,  # the worker opens its PR; CI not started
            self.ci_done,  # CI finished; no review yet
            lambda: None,  # nothing changes: polling records nothing new
            self.review_posted,
        ]
        code = self.run_loop("y\n\nRead books.test.ts: import and new tests only.\n")
        self.assertEqual(code, EXIT_READY, self.text())
        self.assertEqual(self.slept, 4)
        self.assertEqual(len(self.adapter.requests), 1)
        text = self.text()
        self.assertIn("The worker is running; no PR yet.", text)
        self.assertIn("waiting for its CI run to finish", text)
        self.assertIn("waiting for the independent review comment", text)
        self.assertIn(f"Ready for your review: {PR_URL}", text)
        self.assertEqual(text.count("waiting for the independent review comment"), 1)

        # Rolando's answers are signed records for this exact revision.
        cand = collected().candidate
        decisions = ReviewDecisions(self.store, KEY)
        (o,) = decisions.observations(ATTEMPT, fx.DIGEST, cand)
        self.assertEqual(o.verdict, Verdict.PASS)
        self.assertIn("Saw what was expected", o.seen)
        (c,) = decisions.clearances(ATTEMPT, fx.DIGEST, cand)
        self.assertEqual(c.flag, fx.FILE_FLAG)
        self.assertEqual(c.note, "Read books.test.ts: import and new tests only.")

        # One pending record while waiting on review, one success; nothing per poll.
        checks = self.checks()
        conclusions = [
            {r["name"]: r["conclusion"] for r in e.data["results"]}[CHECK_NAME] for e in checks
        ]
        self.assertEqual(conclusions, ["pending", "success"])

        # The report: written, Rolando's only.
        (report,) = list(self.reports.iterdir())
        self.assertEqual(stat.S_IMODE(os.stat(self.reports).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(report).st_mode), 0o600)
        self.assertIn("Ready for your review.", report.read_text())
        self.assertIn(f"Full report: {report}", text)

        # Every prompt he saw was timed.
        activities = [e.data["activity"] for e in self.events(kinds.HUMAN_TIME)]
        self.assertTrue(any("observed ac3" in a for a in activities), activities)
        self.assertTrue(any(fx.FILE_FLAG in a for a in activities), activities)

        # (b) Run again after an interruption: nothing fired, nothing re-asked,
        # nothing new recorded.
        self.said.clear()
        before = len(self.store.events())
        code = self.run_loop("")
        self.assertEqual(code, EXIT_READY, self.text())
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(len(self.store.events()), before)
        self.assertIn(f"Ready for your review: {PR_URL}", self.text())

    def test_b_rerun_mid_wait_fires_nothing(self):
        def interrupt():
            raise KeyboardInterrupt

        self.on_sleep = [interrupt]
        self.assertEqual(self.run_loop(), EXIT_WAITING)
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.said.clear()
        code = self.run_loop("y\n\nRead books.test.ts: only the filterByStatus import changed.\n")
        self.assertEqual(code, EXIT_READY, self.text())
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.events("fire-intent")), 1)

    def test_c_ctrl_c_while_waiting_returns_3(self):
        def interrupt():
            raise KeyboardInterrupt

        self.on_sleep = [interrupt]
        self.assertEqual(self.run_loop(), EXIT_WAITING)
        self.assertIn("Nothing is lost", self.text())
        self.assertEqual(len(self.adapter.requests), 1)

    def test_d_deadline_returns_3(self):
        self.assertEqual(self.run_loop(wait=0), EXIT_WAITING)
        self.assertEqual(self.slept, 0)
        self.assertIn("Still waiting", self.text())
        self.assertEqual(len(self.adapter.requests), 1)

    def test_d_deadline_while_ci_pending_returns_3(self):
        self.open_pr()
        self.assertEqual(self.run_loop(wait=3), EXIT_WAITING)
        self.assertEqual(self.slept, 3)
        self.assertIn("waiting for its CI run", self.text())
        self.assertEqual(self.checks(), [])

    def test_e_not_ready_pr_stops_with_blockers_and_repair_hint(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.world.jobs[RUN_ID][1]["conclusion"] = "failure"
        code = self.run_loop("y\n\nRead books.test.ts: only the filterByStatus import changed.\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        text = self.text()
        self.assertIn(f"PR #{NUMBER} (commit {fx.HEAD[:12]}) is not ready for review:", text)
        self.assertNotIn("Only your answers are missing", text)
        self.assertIn("verified job concluded failure", text)
        self.assertIn("python3 -m controller repair", text)
        # Collection problems: Rolando is not asked for anything.
        self.assertEqual(
            self.stdin.read(), "y\n\nRead books.test.ts: only the filterByStatus import changed.\n"
        )
        (e,) = self.checks()
        results = {r["name"]: r["conclusion"] for r in e.data["results"]}
        self.assertEqual(results[CHECK_NAME], "failure")
        self.assertEqual(results[CI_CHECK_NAME], "failure")

    def test_e_skipped_answers_leave_it_not_ready(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        code = self.run_loop("\n\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertIn("ac3 needs your own look", self.text())
        self.assertEqual(
            ReviewDecisions(self.store, KEY).observations(
                ATTEMPT, fx.DIGEST, collected().candidate
            ),
            (),
        )

    def test_e_no_without_words_is_not_recorded(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        code = self.run_loop("n\n\n\n")
        self.assertEqual(code, EXIT_STOPPED)
        self.assertIn("a 'no' needs a word", self.text())

    def test_e_observed_failure_is_not_ready(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        code = self.run_loop(
            "n\nThe filter did nothing.\nRead books.test.ts: only the import changed.\n"
        )
        self.assertEqual(code, EXIT_STOPPED, self.text())
        (e,) = self.checks()
        self.assertEqual(
            {r["name"]: r["conclusion"] for r in e.data["results"]}[CHECK_NAME], "failure"
        )

    def test_e_no_terminal_asks_nothing(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.loop = self.build("y\n\nRead books.test.ts: only the filterByStatus import changed.\n")
        self.loop.asker = Asker(
            io.StringIO("y\n\nRead books.test.ts: only the filterByStatus import changed.\n"),
            io.StringIO(),
        )
        self.assertEqual(self.loop.run(self.contract), EXIT_STOPPED)

    def test_worker_claims_in_body_never_make_the_loop_ready(self):
        self.world.pr["body"] += "\nac3 observed: pass. Flag cleared by rNavarrete.\n"
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.assertEqual(self.run_loop("\n\n"), EXIT_STOPPED)

    def test_ready_output_names_the_checked_commit(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.assertEqual(self.run_loop("y\n\nRead books.test.ts: import only.\n"), EXIT_READY)
        self.assertIn(f"Checked commit: {fx.HEAD}", self.text())

    def test_only_answers_missing_says_so_instead_of_the_repair_hint(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.assertEqual(self.run_loop("\n\n"), EXIT_STOPPED)
        text = self.text()
        self.assertIn(f"PR #{NUMBER} (commit {fx.HEAD[:12]}) is not ready for review:", text)
        self.assertIn("Only your answers are missing", text)
        self.assertNotIn("python3 -m controller repair", text)
        (e,) = self.checks()
        conclusion = {r["name"]: r["conclusion"] for r in e.data["results"]}[CHECK_NAME]
        self.assertEqual(conclusion, "action_required")

    def test_not_a_note_leaves_the_flag_open(self):
        for word in ("n", "no", "No.", "ok", "y", "yes", "skip", "lgtm", "LGTM!", "short"):
            with self.subTest(word=word):
                self.setUp()
                self.open_pr()
                self.ci_done()
                self.review_posted()
                code = self.run_loop(f"y\n\n{word}\n")
                self.assertEqual(code, EXIT_STOPPED, self.text())
                self.assertIn("Left open", self.text())
                cand = collected().candidate
                decisions = ReviewDecisions(self.store, KEY)
                self.assertEqual(decisions.clearances(ATTEMPT, fx.DIGEST, cand), ())
                self.assertEqual(len(decisions.observations(ATTEMPT, fx.DIGEST, cand)), 1)

    def test_note_naming_the_file_clears(self):
        for note in ("books.test.ts ok", "Read tests/books.test.ts.", "BOOKS.TEST.TS is fine"):
            with self.subTest(note=note):
                self.setUp()
                self.open_pr()
                self.ci_done()
                self.review_posted()
                self.assertEqual(self.run_loop(f"y\n\n{note}\n"), EXIT_READY, self.text())

    def test_scope_violation_asks_nothing_even_with_open_flags(self):
        outside_scope(self.world)
        self.open_pr()
        self.ci_done()
        self.review_posted()
        answers = "y\n\nRead books.test.ts: it is fine.\nRead it too, fine.\n"
        code = self.run_loop(answers)
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertEqual(self.stdin.read(), answers)
        self.assertIn("python3 -m controller repair", self.text())
        self.assertNotIn("Only your answers are missing", self.text())
        self.assertEqual(self.events(kinds.HUMAN_DECISION)[1:], [])
        (e,) = self.checks()
        conclusion = {r["name"]: r["conclusion"] for r in e.data["results"]}[CHECK_NAME]
        self.assertEqual(conclusion, "failure")

    def test_ci_workflow_change_asks_nothing(self):
        changes_ci(self.world)
        self.open_pr()
        self.ci_done()
        self.review_posted()
        answers = "y\n\nRead the workflow change, fine.\n"
        self.assertEqual(self.run_loop(answers), EXIT_STOPPED, self.text())
        self.assertEqual(self.stdin.read(), answers)
        self.assertNotIn("Needs your eyes", self.text())

    def test_worker_editing_rolandos_review_comment_is_never_ready(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        # Same author on GitHub, but edited after it was posted.
        self.world.comments[0]["updated_at"] = "2026-10-08T15:45:00Z"
        code = self.run_loop("y\n\nRead books.test.ts: import only.\n", wait=2)
        self.assertEqual(code, EXIT_WAITING, self.text())
        self.assertIn("waiting for the independent review comment", self.text())
        self.assertNotIn("Ready for your review", self.text())
        self.assertEqual(self.stdin.read(), "y\n\nRead books.test.ts: import only.\n")

    def test_attempt_from_another_contract_version_stops(self):
        self.assertEqual(self.run_loop(wait=0), EXIT_WAITING)
        self.contract = dict(fx.CONTRACT, goal="Something else entirely.")
        self.said.clear()
        self.open_pr()
        self.ci_done()
        self.review_posted()
        code = self.run_loop("y\n\nRead books.test.ts: import only.\n")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        self.assertIn("was started from another version of this contract", self.text())
        self.assertIn(fx.DIGEST.short, self.text())
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(self.checks(), [])

    def test_ctrl_c_during_dispatch_returns_3(self):
        def interrupt(request):
            raise KeyboardInterrupt

        self.adapter.answers = [interrupt]
        self.assertEqual(self.run_loop(), EXIT_WAITING)
        self.assertIn("Whatever was sent is on record", self.text())

    def test_not_interactive_loop_records_no_minutes(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.loop = self.build("y\n\nRead books.test.ts: import only.\n")
        self.loop.timer.interactive = lambda: False
        self.assertEqual(self.loop.run(self.contract), EXIT_READY, self.text())
        timed = [e for e in self.events(kinds.HUMAN_TIME) if e.data["entered_by"] != "Rolando"]
        self.assertEqual(timed, [])

    def test_new_push_after_ready_needs_new_answers(self):
        self.open_pr()
        self.ci_done()
        self.review_posted()
        self.assertEqual(
            self.run_loop("y\n\nRead books.test.ts: only the filterByStatus import changed.\n"),
            EXIT_READY,
        )
        # The worker pushes again: new head, new CI run, and a new review.
        self.world.pr["head"]["sha"] = fx.NEW_HEAD
        self.world.runs[0]["head_sha"] = fx.NEW_HEAD
        for a in self.world.artifacts[RUN_ID]:
            a["name"] = a["name"].replace(fx.HEAD, fx.NEW_HEAD)
        from tests.github_world import control_change, evidence, zipped

        self.world.blobs[1] = zipped("check-evidence", evidence(commit=fx.NEW_HEAD))
        self.world.blobs[2] = zipped("control-change", control_change(head=fx.NEW_HEAD))
        for path in ("src/books.ts", "src/main.ts", fx.TEST_FILE):
            self.world.contents[(path, fx.NEW_HEAD)] = self.world.contents[(path, fx.HEAD)]
        new_cand = replace(fx.candidate(), head_commit=fx.NEW_HEAD)
        from verify.review import review_block

        self.world.comments.append(dict(self.world.comments[0], id=901))
        self.world.comments[1]["html_url"] = f"{PR_URL}#issuecomment-901"
        self.world.comments[1]["created_at"] = "2026-10-08T18:00:00Z"
        self.world.comments[1]["updated_at"] = "2026-10-08T18:00:00Z"
        self.world.comments[1]["body"] = review_block(
            str(fx.DIGEST),
            new_cand,
            links=[
                {"criterion": c, "path": fx.TEST_FILE, "test": t, "assertion": a, "why": "w"}
                for c, t, a in (
                    ("ac1", fx.AC1_TEST, fx.AC1_ASSERT),
                    ("ac2", fx.AC2_TEST, fx.AC2_ASSERT),
                )
            ],
            proofs=[
                {"criterion": c, "path": fx.TEST_FILE, "test": t, "outcome": "failed-error"}
                for c, t in (("ac1", fx.AC1_TEST), ("ac2", fx.AC2_TEST))
            ],
        )
        self.open_pr()
        self.said.clear()
        self.assertEqual(self.run_loop("\n\n"), EXIT_STOPPED, self.text())
        self.assertIn("ac3 needs your own look", self.text())
        self.assertIn(f"(commit {fx.NEW_HEAD[:12]})", self.text())
        self.assertIn("Only your answers are missing", self.text())


class MergedTests(LoopCase):
    def test_f_merged_pr_clears_the_writer_and_records_review_minutes(self):
        self.open_pr(merged=True)
        code = self.run_loop("12\n")
        self.assertEqual(code, EXIT_READY, self.text())
        # The merge itself frees the lane (Rolando's 2026-10-09 rule): no clearing to record.
        self.assertEqual(self.recovery.clears, [])
        status = self.recovery.attempt_status(ATTEMPT, self.now)
        self.assertTrue(status.writer_cleared)
        minutes = [e for e in self.events(kinds.HUMAN_TIME) if e.data["entered_by"] == "Rolando"]
        self.assertEqual(len(minutes), 1)
        self.assertEqual(minutes[0].data["minutes"], 12)
        self.assertIn("reviewed and merged", minutes[0].data["activity"])
        self.assertEqual(minutes[0].task, self.task)
        self.assertIn("The lane is free", self.text())

    def test_f_merged_with_no_minutes_records_none(self):
        self.open_pr(merged=True)
        self.assertEqual(self.run_loop("\n"), EXIT_READY, self.text())
        self.assertEqual(self.recovery.clears, [])
        self.assertEqual(
            [e for e in self.events(kinds.HUMAN_TIME) if e.data["entered_by"] == "Rolando"], []
        )

    def minutes(self):
        return [e for e in self.events(kinds.HUMAN_TIME) if e.data["entered_by"] == "Rolando"]

    def test_f_minutes_bounds(self):
        cases = {
            "480\n": [480],
            "0.5\n": [0.5],
            "nan\n12\n": [12],
            "inf\n7\n": [7],
            "1e9\n30\n": [30],
            "481\n5\n": [5],
            "0\n-3\n": [],
            "nan\ninf\n9\n": [],
            "lots\n\n": [],
        }
        for answers, want in cases.items():
            with self.subTest(answers=answers):
                self.setUp()
                self.open_pr(merged=True)
                self.assertEqual(self.run_loop(answers), EXIT_READY, self.text())
                self.assertEqual([e.data["minutes"] for e in self.minutes()], want)

    def test_f_bad_minutes_are_reasked_once(self):
        self.open_pr(merged=True)
        self.run_loop("nan\ninf\n9\n")
        self.assertEqual(self.stdin.read(), "9\n")
        self.assertEqual(self.text().count("A number of minutes"), 2)

    def test_f_merged_rerun_does_not_clear_twice(self):
        self.open_pr(merged=True)
        self.assertEqual(self.run_loop("\n"), EXIT_READY)
        self.said.clear()
        self.assertEqual(self.run_loop("15\n"), EXIT_READY)
        self.assertEqual(len(self.recovery.clears), 0)  # the rebuilt recovery's spy
        # Minutes were skipped the first time, so the re-run asks once more.
        self.assertEqual([e["minutes"] for e in (m.data for m in self.minutes())], [15])
        self.said.clear()
        self.assertEqual(self.run_loop("20\n"), EXIT_READY)
        self.assertIn("already closed out", self.text())
        self.assertEqual(len(self.minutes()), 1)
        self.assertEqual(self.stdin.read(), "20\n")  # minutes not asked a third time
        cleared = [e for e in self.store.events() if e.event.kind == "attempt-cleared"]
        self.assertLessEqual(len(cleared), 1)


# --- CLI ------------------------------------------------------------------------


class FakeLoop:
    def __init__(self, store):
        self.store = store
        self.timer = Timer(store, lambda: NOW)
        self.runs = []

    def run(self, contract, wait_minutes):
        self.runs.append((contract, wait_minutes))
        return 0


class CliTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.fake = FakeLoop(self.store)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = loop_cli.main(list(argv), make=lambda: self.fake)
        return code, out.getvalue()

    def test_time_records_rolandos_minutes(self):
        code, out = self.main(
            "time", "15", "read PR 7", "--task", "filter-by-status", "--why", "worker stuck"
        )
        self.assertEqual(code, 0)
        self.assertIn("Logged.", out)
        (s,) = self.store.events()
        self.assertEqual(s.event.kind, kinds.HUMAN_TIME)
        self.assertEqual(s.event.data["minutes"], 15)
        self.assertEqual(s.event.data["activity"], "read PR 7")
        self.assertEqual(s.event.data["entered_by"], "Rolando")
        self.assertEqual(s.event.data["intervention_reason"], "worker stuck")
        self.assertEqual(s.event.task, TaskId("filter-by-status"))

    def test_time_without_a_task(self):
        code, _ = self.main("time", "2.5", "glanced at the board")
        self.assertEqual(code, 0)
        (s,) = self.store.events()
        self.assertIsNone(s.event.task)

    def test_time_out_of_range_is_refused(self):
        for minutes in ("0", "-5", "1441"):
            with self.subTest(minutes=minutes):
                code, out = self.main("time", minutes, "x")
                self.assertEqual(code, 2)
                self.assertIn("Minutes must be", out)
        self.assertEqual(self.store.events(), [])

    def test_times_prints_csv(self):
        self.main("time", "15", "read PR 7")
        self.main("time", "3", "approved a contract")
        code, out = self.main("times")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("read PR 7", lines[1])
        self.assertIn("approved a contract", lines[2])

    def test_times_on_an_empty_ledger_prints_the_header(self):
        code, out = self.main("times")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_run_reads_the_contract_and_passes_the_wait(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_bytes(
                contracts.dumps(fx.CONTRACT)
                if hasattr(contracts, "dumps")
                else __import__("json").dumps(dict(fx.CONTRACT), default=dict).encode()
            )
            code, _ = self.main("run", str(path), "--wait", "-4")
        self.assertEqual(code, 0)
        ((contract, wait),) = self.fake.runs
        self.assertEqual(contract["task_id"], "filter-by-status")
        self.assertEqual(wait, 0)

    def test_run_with_an_unreadable_contract(self):
        code, out = self.main("run", "/nonexistent/contract.json")
        self.assertEqual(code, 2)
        self.assertIn("Can't read the contract", out)
        self.assertEqual(self.fake.runs, [])


if __name__ == "__main__":
    unittest.main()


class ReviewIsNotAwaitedForAKnownFailure(LoopCase):
    """Rolando's repro: a forbidden package.json change with no review posted
    must be reported at once, not hidden behind "waiting for the review"."""

    def test_out_of_scope_change_is_reported_without_a_review(self):
        self.world.comments = []
        self.world.files.append({"filename": "package.json", "status": "modified"})
        self.world.pr["changed_files"] += 1
        self.world.contents[("package.json", fx.HEAD)] = b'{"scripts": {}}\n'
        self.world.contents[("package.json", fx.BASE)] = b"{}\n"
        self.on_sleep = [self.open_pr]
        code = self.run_loop("")
        self.assertEqual(code, EXIT_STOPPED, self.text())
        text = self.text()
        self.assertNotIn("waiting for the independent review comment", text)
        self.assertIn("outside the permitted paths: package.json", text)
        self.assertEqual([r["conclusion"] for r in self.checks()[-1].data["results"]][0], "failure")


class BackupTests(LoopCase):
    def test_every_write_of_a_run_is_backed_up(self):
        self.on_sleep = [self.open_pr]
        code = self.run_loop("y\n\nRead books.test.ts: import and new tests only.\n")
        self.assertEqual(code, EXIT_READY, self.text())
        last = max(s.seq for s in self.store.events())
        # The final backup holds the last write (Rolando's answers, the checks
        # record), so a restore loses none of them.
        self.assertTrue(self.backups, "nothing was backed up")
        self.assertEqual(self.backups[-1], last)
        kinds_written = {s.event.kind for s in self.store.events() if s.seq <= self.backups[-1]}
        self.assertIn(kinds.CHECKS, kinds_written)
        self.assertIn(kinds.HUMAN_DECISION, kinds_written)

    def test_a_run_that_writes_nothing_makes_no_extra_backup(self):
        self.on_sleep = [self.open_pr]
        self.run_loop("y\n\nRead books.test.ts: import and new tests only.\n")
        before = len(self.backups)
        self.assertEqual(self.run_loop("", wait=0), EXIT_READY, self.text())
        self.assertEqual(len(self.backups), before)

    def test_a_failing_backup_only_warns(self):
        def broken(now):
            raise OSError("disk full")

        self.loop.backup = broken
        self.on_sleep = [self.open_pr]
        code = self.loop.run(self.contract)
        self.assertIn("the ledger backup failed (OSError: disk full)", self.text())
        self.assertIn(code, (EXIT_READY, EXIT_STOPPED))
