"""ENG-156: the automatic independent review.

Runs on the fake GitHub in github_world, a real SQLite ledger in a temporary
folder and a fake review runtime that records what it was asked to launch.
Nothing touches the network or a real start endpoint.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller.adapter.routine import LaunchInterrupted
from controller.attempts import AttemptGate, policy
from controller.attempts import events as aev
from controller.interfaces import (
    AttemptId,
    LaunchOutcome,
    LaunchResult,
    LedgerEvent,
    LedgerLocked,
    TaskId,
)
from controller.ledger import SqliteLedgerStore
from controller.loop.collect import NotFound
from controller.review import events as rev
from controller.review.__main__ import AsOpen, dry_run, qualify_identity
from controller.review.reviewer import (
    AutoReviewer,
    ReviewPolicy,
    ReviewState,
    Revision,
    ledger_contracts,
)
from controller.review.runtime import ENVELOPE, ReviewJob, ReviewTooLarge, review_text
from controller.service.seams import PullRequestRef
from redteam import fixtures as fx
from tests.github_world import ATTEMPT, NUMBER, RUN_ID, World, control_change, evidence, zipped
from verify.review import review_block

T0 = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
REVIEWER = fx.MAPPER
POLICY = ReviewPolicy(reviewers=frozenset({REVIEWER}))
PR = PullRequestRef(ATTEMPT, NUMBER)


class FakeRuntime:
    """Answers each launch from a script; the default is launched."""

    def __init__(self, *answers):
        self.texts: list[str] = []
        self.answers = list(answers)
        self.during = None

    def launch(self, text):
        self.texts.append(text)
        if self.during is not None:
            self.during()
        answer = self.answers.pop(0) if self.answers else "launched"
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, LaunchResult):
            return answer
        if answer == "launched":
            n = len(self.texts)
            return LaunchResult(
                LaunchOutcome.LAUNCHED, 200, f"cse_{n}", f"https://claude.ai/code/cse_{n}"
            )
        if answer == "rejected":
            return LaunchResult(LaunchOutcome.NOT_LAUNCHED, 403, detail="forbidden")
        return LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, None, detail="timeout")


def reviewed(world: World, login: str = REVIEWER) -> World:
    world.comments[0]["user"]["login"] = login
    return world


def unreviewed() -> World:
    w = World()
    w.comments = []
    return w


def pushed(world: World, head: str) -> World:
    """The same PR after a new push to ``head``, with CI finished on it."""
    old = world.pr["head"]["sha"]
    world.pr["head"]["sha"] = head
    for r in world.runs:
        r["head_sha"] = head
    world.artifacts[RUN_ID] = [
        {"id": 1, "name": f"check-evidence-{head}", "expired": False},
        {"id": 2, "name": f"control-change-{head}", "expired": False},
    ]
    world.blobs = {
        1: zipped("check-evidence", evidence(commit=head)),
        2: zipped("control-change", control_change(head=head)),
    }
    world.contents.update({(p, head): v for (p, c), v in list(world.contents.items()) if c == old})
    return world


def review_for(head: str, login: str = REVIEWER, **block) -> dict:
    """A review comment by ``login`` for the revision at ``head``."""
    text = "Mapping.\n\n" + review_block(
        str(fx.DIGEST), fx.candidate(head_commit=head), **_honest_args(**block)
    )
    return {
        "id": 901,
        "user": {"login": login},
        "html_url": f"{World().pr['html_url']}#issuecomment-901",
        "body": text,
        "created_at": "2026-10-08T17:00:00Z",
        "updated_at": "2026-10-08T17:00:00Z",
    }


def _honest_args(**changes):
    links = [
        {
            "criterion": cid,
            "path": fx.TEST_FILE,
            "test": test,
            "assertion": assertion,
            "why": f"Checks {cid}'s statement directly on filterByStatus.",
        }
        for cid, test, assertion in (
            ("ac1", fx.AC1_TEST, fx.AC1_ASSERT),
            ("ac2", fx.AC2_TEST, fx.AC2_ASSERT),
        )
    ]
    proofs = [
        {
            "criterion": cid,
            "path": fx.TEST_FILE,
            "test": test,
            "outcome": "failed-error",
            "output_excerpt": "TypeError: filterByStatus is not a function",
        }
        for cid, test in (("ac1", fx.AC1_TEST), ("ac2", fx.AC2_TEST))
    ]
    args = dict(links=links, proofs=proofs, limits=[])
    args.update(changes)
    return args


def his_answers(attempt, digest, cand):
    """Rolando's recorded observation of ac3 and his clearance of the test change."""
    return (fx.observation(),), (fx.clearance(),)


def his_answers_for(head):
    def answers(attempt, digest, cand):
        return (fx.observation(commit=head),), (fx.clearance(commit=head),)

    return answers


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.db"
        self.store = self.open_store()
        self.now = T0
        with self.store.writer_lock():
            self.store.append(aev.attempt_reserved(ATTEMPT, fx.DIGEST, T0 - timedelta(hours=1)))
            self.store.append(snapshot(T0 - timedelta(hours=1)))
        self.world = unreviewed()
        self.runtime = FakeRuntime()

    def open_store(self):
        store = SqliteLedgerStore(self.path)
        self.addCleanup(store.close)
        return store

    def reviewer(self, store=None, world=None, runtime=None, policy=POLICY, **kw):
        store = store or self.store
        return AutoReviewer(
            store,
            world or self.world,
            runtime or self.runtime,
            ledger_contracts(store, lambda d: fx.CONTRACT),
            policy=policy,
            clock=lambda: self.now,
            **kw,
        )

    def kinds(self, kind, store=None):
        return [s.event for s in (store or self.store).events() if s.event.kind == kind]

    def later(self, **delta):
        self.now = self.now + timedelta(**delta)


def snapshot(at, weekly=10):
    return LedgerEvent(
        aev.USAGE_SNAPSHOT,
        at,
        data={
            "taken_at": at.isoformat(),
            "session_pct": 0,
            "weekly_pct": weekly,
            "credits_spent": 0,
        },
    )


# --- one review per request key ---------------------------------------------------


class RepeatedRequestTests(Base):
    def test_first_start_launches_one_full_pass_for_the_exact_revision(self):
        note = self.reviewer().start(PR, "req-1")
        self.assertIn("has started (full pass)", note)
        self.assertEqual(len(self.runtime.texts), 1)
        job = json.loads(self.runtime.texts[0])
        self.assertEqual(job["envelope"], ENVELOPE)
        self.assertEqual(job["pass"], "full")
        self.assertEqual(job["head"], fx.HEAD)
        self.assertEqual(job["base"], fx.MAIN)
        self.assertEqual(job["merge_base"], fx.BASE)
        self.assertEqual(job["contract_digest"], fx.DIGEST.value)
        self.assertEqual(job["reviewer"], REVIEWER)
        self.assertEqual(job["pr"], NUMBER)

    def test_repeated_start_and_check_launch_nothing_more(self):
        r = self.reviewer()
        r.start(PR, "req-1")
        r.start(PR, "req-1")
        r.start(PR, "req-1")
        for _ in range(3):
            self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        self.assertEqual(len(self.runtime.texts), 1)
        self.assertEqual(len(self.kinds(rev.REVIEW_WATCH)), 1)
        self.assertEqual(len(self.kinds(rev.REVIEW_JOB_INTENT)), 1)

    def test_a_restarted_reviewer_reuses_the_same_review(self):
        self.reviewer().start(PR, "req-1")
        again = self.reviewer(store=self.open_store(), runtime=FakeRuntime())
        again.start(PR, "req-1")
        self.assertIs(again.check(ATTEMPT).state, ReviewState.RUNNING)
        self.assertEqual(len(self.kinds(rev.REVIEW_JOB_INTENT)), 1)
        self.assertEqual(again._runtime.texts, [])

    def test_same_revision_gives_the_same_key_and_any_change_a_new_one(self):
        r = Revision(fx.REPO, 7, fx.DIGEST.value, fx.HEAD, fx.MAIN, fx.BASE)
        self.assertEqual(r.key(), replace(r).key())
        for change in (
            {"head": fx.NEW_HEAD},
            {"base": fx.NEW_MAIN},
            {"merge_base": fx.MAIN},
            {"digest": "0" * 64},
            {"pr": 8},
            {"repository": fx.FORK},
        ):
            self.assertNotEqual(r.key(), replace(r, **change).key(), change)
        self.assertNotEqual(r.key("1"), r.key("2"))

    def test_verdict_is_recorded_once_while_nothing_changes(self):
        reviewed(self.world_with_review())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        for _ in range(4):
            self.assertIs(r.check(ATTEMPT).state, ReviewState.PASSED)
        self.assertEqual(len(self.kinds(rev.REVIEW_VERDICT)), 1)
        self.assertEqual(self.runtime.texts, [])  # the review was already posted

    def world_with_review(self):
        self.world = reviewed(World())
        return self.world


class RaceTests(Base):
    def test_two_reviewers_on_one_ledger_launch_once(self):
        other_store = self.open_store()
        other_runtime = FakeRuntime()
        other = self.reviewer(store=other_store, runtime=other_runtime)
        seen = []

        def second_reviewer_checks_mid_launch():
            seen.append(other.check(ATTEMPT))

        self.runtime.during = second_reviewer_checks_mid_launch
        first = self.reviewer()
        first.start(PR, "req-1")
        self.assertEqual(len(self.runtime.texts), 1)
        self.assertEqual(other_runtime.texts, [])
        self.assertIs(seen[0].state, ReviewState.RUNNING)
        self.assertEqual(len(self.kinds(rev.REVIEW_JOB_INTENT)), 1)

    def test_a_held_writer_lock_never_launches_without_a_claim(self):
        other_store = self.open_store()
        r = self.reviewer()
        r.start(PR, "req-1")  # watch written, job launched
        self.runtime.texts.clear()
        second = self.reviewer(store=other_store, runtime=FakeRuntime())
        with self.store.writer_lock():
            # Another process holds the lock: nothing can be claimed, so nothing launches.
            w = unreviewed()
            w.pr["head"]["sha"] = fx.NEW_HEAD  # a new revision that would need a job
            second._api = pushed(w, fx.NEW_HEAD)
            with self.assertRaises(LedgerLocked):
                second.check(ATTEMPT)
        self.assertEqual(second._runtime.texts, [])

    def test_launch_answer_written_later_when_the_lock_was_busy(self):
        other_store = self.open_store()
        r = self.reviewer(store=other_store)

        def hold_lock_elsewhere():
            # The other process takes the lock while the launch is in flight.
            self.lock = self.store.writer_lock()
            self.lock.__enter__()

        self.runtime.during = hold_lock_elsewhere
        r.start(PR, "req-1")
        self.lock.__exit__(None, None, None)
        self.assertEqual(self.kinds(rev.REVIEW_JOB_LAUNCHED), [])
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        launched = self.kinds(rev.REVIEW_JOB_LAUNCHED)
        self.assertEqual([e.data["outcome"] for e in launched], ["launched"])
        self.assertEqual(len(self.runtime.texts), 1)


class RestartTests(Base):
    def test_crash_between_claim_and_answer_is_never_relaunched(self):
        self.runtime = FakeRuntime(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.reviewer().start(PR, "req-1")
        self.assertEqual(len(self.kinds(rev.REVIEW_JOB_INTENT)), 1)
        fresh = FakeRuntime()
        r = self.reviewer(store=self.open_store(), runtime=fresh)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)  # may still be in flight
        self.later(minutes=10)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.UNKNOWN)
        self.assertIn("unclear whether", status.note)
        self.later(days=1)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.UNKNOWN)
        self.assertEqual(fresh.texts, [])
        outcomes = [e.data["outcome"] for e in self.kinds(rev.REVIEW_JOB_LAUNCHED)]
        self.assertEqual(outcomes, ["launch-outcome-unknown"])

    def test_lost_answer_is_unknown_and_never_relaunched(self):
        self.runtime = FakeRuntime("lost")
        r = self.reviewer()
        self.assertIn("unclear whether", r.start(PR, "req-1"))
        for _ in range(3):
            self.later(hours=1)
            self.assertIs(r.check(ATTEMPT).state, ReviewState.UNKNOWN)
        self.assertEqual(len(self.runtime.texts), 1)

    def test_network_error_is_unknown_not_a_retry(self):
        self.runtime = FakeRuntime(OSError("connection reset"))
        r = self.reviewer()
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.UNKNOWN)
        self.assertEqual(len(self.runtime.texts), 1)

    def test_a_result_posted_after_a_lost_answer_is_still_read(self):
        self.runtime = FakeRuntime("lost")
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        r._api = reviewed(World())
        self.assertIs(r.check(ATTEMPT).state, ReviewState.PASSED)

    def test_definite_rejection_is_tried_again_within_its_allowance(self):
        self.runtime = FakeRuntime("rejected", "rejected", "launched")
        r = self.reviewer()
        self.assertIn("did not start", r.start(PR, "req-1"))
        self.assertIs(r.check(ATTEMPT).state, ReviewState.BLOCKED)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.BLOCKED)
        self.assertIn("already tried 2 times", status.note)
        self.assertEqual(len(self.runtime.texts), 2)

    def test_refused_before_sending_counts_as_not_launched(self):
        self.runtime = FakeRuntime(LookupError("no start key in the Keychain"))
        r = self.reviewer()
        self.assertIn("did not start", r.start(PR, "req-1"))
        launched = self.kinds(rev.REVIEW_JOB_LAUNCHED)
        self.assertEqual([e.data["outcome"] for e in launched], ["not-launched"])

    def test_a_launched_job_that_never_posts_times_out_once(self):
        r = self.reviewer()
        r.start(PR, "req-1")
        self.later(hours=1)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        self.later(hours=2)
        for _ in range(3):
            status = r.check(ATTEMPT)
            self.assertIs(status.state, ReviewState.UNKNOWN)
            self.assertIn("hasn't posted a result", status.note)
        verdicts = [e.data["verdict"] for e in self.kinds(rev.REVIEW_VERDICT)]
        self.assertEqual(verdicts, ["unknown"])
        self.assertEqual(len(self.runtime.texts), 1)


# --- stale evidence -----------------------------------------------------------------


class StaleCommitTests(Base):
    def test_a_new_push_needs_a_new_review_and_the_old_verdict_names_its_commit(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        first = r.check(ATTEMPT)
        self.assertIs(first.state, ReviewState.PASSED)
        self.assertEqual(first.reviewed_commit, fx.HEAD)
        r._api = pushed(self.world, fx.NEW_HEAD)
        r._decisions = his_answers_for(fx.NEW_HEAD)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.RUNNING)
        self.assertNotEqual(status.key, first.key)
        self.assertEqual(status.reviewed_commit, "")
        self.assertEqual(json.loads(self.runtime.texts[0])["head"], fx.NEW_HEAD)
        self.assertEqual(json.loads(self.runtime.texts[0])["pass"], "verify")
        # The ledger's latest verdict still names the commit it covered.
        evidence = r.evidence(ATTEMPT)
        self.assertIs(evidence.state, ReviewState.PASSED)
        self.assertEqual(evidence.reviewed_commit, fx.HEAD)

    def test_the_old_comment_never_counts_for_the_new_head(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        r._api = pushed(self.world, fx.NEW_HEAD)
        r._decisions = his_answers_for(fx.NEW_HEAD)
        self.assertIsNone(r.check(ATTEMPT).review_url)

    def test_a_review_for_the_new_head_passes_it(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        w = pushed(self.world, fx.NEW_HEAD)
        w.comments.append(review_for(fx.NEW_HEAD))
        r._api = w
        r._decisions = his_answers_for(fx.NEW_HEAD)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.PASSED, status.note)
        self.assertEqual(status.reviewed_commit, fx.NEW_HEAD)
        self.assertEqual(r.evidence(ATTEMPT).reviewed_commit, fx.NEW_HEAD)

    def test_a_moved_base_is_a_new_review(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        first = r.check(ATTEMPT)
        self.world.pr["base"]["sha"] = fx.NEW_MAIN
        for run in self.world.runs:
            run["pull_requests"][0]["base"]["sha"] = fx.NEW_MAIN
        status = r.check(ATTEMPT)
        self.assertNotEqual(status.key, first.key)
        self.assertIsNot(status.state, ReviewState.PASSED)

    def test_ci_still_running_on_the_new_head_waits_without_a_launch(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        self.world.pr["head"]["sha"] = fx.NEW_HEAD  # pushed, CI not started yet
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.WAITING_CI)
        self.assertEqual(self.runtime.texts, [])

    def test_a_deleted_review_comment_withdraws_the_verdict(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.PASSED)
        self.world.comments = []
        for _ in range(2):
            status = r.check(ATTEMPT)
            self.assertIs(status.state, ReviewState.UNKNOWN)
            self.assertIn("no longer stands", status.note)
        self.assertIs(r.evidence(ATTEMPT).state, ReviewState.UNKNOWN)
        self.assertEqual(self.runtime.texts, [])

    def test_a_closed_pr_is_reported_before_any_job(self):
        self.world.pr["state"] = "closed"
        r = self.reviewer()
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.NOT_REVIEWABLE)
        self.assertTrue(status.for_rolando())
        self.assertEqual(self.runtime.texts, [])

    def test_a_retargeted_pr_is_reported_before_any_job(self):
        self.world.pr["base"]["ref"] = "release"
        r = self.reviewer()
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.NOT_REVIEWABLE)
        self.assertEqual(self.runtime.texts, [])

    def test_a_merged_pr_keeps_the_verdict_for_its_last_commit(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.PASSED)
        self.world.pr["state"] = "closed"
        self.world.pr["merged"] = True
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.PASSED)
        self.assertEqual(status.reviewed_commit, fx.HEAD)


# --- who may review -----------------------------------------------------------------


class SpoofingTests(Base):
    def assert_not_counted(self, world):
        r = self.reviewer(world=world, decisions=his_answers)
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIsNot(status.state, ReviewState.PASSED, status.note)
        self.assertIsNone(status.review_url)
        self.assertEqual(self.kinds(rev.REVIEW_VERDICT), [])
        return status

    def test_the_workers_comment_does_not_count(self):
        self.assert_not_counted(reviewed(World(), fx.WORKER))

    def test_the_workers_bot_spelling_does_not_count(self):
        self.assert_not_counted(reviewed(World(), fx.WORKER.upper() + "[bot]"))

    def test_rolandos_own_comment_is_not_the_independent_review(self):
        self.assert_not_counted(reviewed(World(), fx.REVIEWER))

    def test_an_unlisted_account_does_not_count(self):
        self.assert_not_counted(reviewed(World(), "someone-else"))

    def test_a_look_alike_login_does_not_count(self):
        self.assert_not_counted(reviewed(World(), "factory-verifler"))
        self.assert_not_counted(reviewed(World(), REVIEWER + "-2"))

    def test_an_edited_review_comment_does_not_count(self):
        w = reviewed(World())
        w.comments[0]["updated_at"] = "2026-10-08T15:30:00Z"
        self.assert_not_counted(w)

    def test_a_review_for_another_revision_does_not_count(self):
        w = World()
        w.comments = [review_for(fx.OLD_HEAD)]
        self.assert_not_counted(w)

    def test_a_review_claiming_another_contract_does_not_count(self):
        w = World()
        w.comments = [review_for(fx.HEAD)]
        w.comments[0]["body"] = w.comments[0]["body"].replace(fx.DIGEST.value, "0" * 64)
        self.assert_not_counted(w)

    def test_no_reviewer_account_means_no_launch_and_nothing_counts(self):
        w = reviewed(World(), "rNavarrete")
        r = self.reviewer(world=w, policy=ReviewPolicy(), decisions=his_answers)
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.BLOCKED)
        self.assertIn("No reviewer account", status.note)
        self.assertEqual(self.runtime.texts, [])

    def test_the_reviewer_list_refuses_the_worker_rolando_and_bad_logins(self):
        for login in (fx.WORKER, fx.WORKER + "[bot]", fx.REVIEWER, "rnavarrete", "a b", "",
                      "-x", "x" * 40, "github-actions[bot]"):  # fmt: skip
            with self.assertRaises(ValueError, msg=login):
                ReviewPolicy(reviewers=frozenset({login}))

    def test_allowances_must_be_positive(self):
        for kw in ({"passes_per_cycle": 0}, {"extra_passes": -1}, {"launches_per_job": 0}):
            with self.assertRaises(ValueError):
                ReviewPolicy(reviewers=frozenset({REVIEWER}), **kw)

    def test_the_contract_comes_from_the_ledger_not_the_pr(self):
        w = reviewed(World())
        w.pr["body"] = "Contract-Digest: sha256:" + "0" * 64
        r = self.reviewer(world=w, decisions=his_answers)
        r.start(PR, "req-1")
        self.assertIsNot(r.check(ATTEMPT).state, ReviewState.PASSED)

    def test_an_attempt_the_gate_never_reserved_has_no_contract(self):
        other = AttemptId(TaskId("not-reserved"), 1)
        r = self.reviewer()
        note = r.start(PullRequestRef(other, NUMBER), "req-x")
        self.assertIn("queued", note)
        self.assertEqual(self.runtime.texts, [])


# --- verdicts and findings ------------------------------------------------------------


class VerdictTests(Base):
    def test_without_his_answers_the_review_needs_rolando_with_nothing_for_repair(self):
        self.world = reviewed(World())
        r = self.reviewer()
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.NEEDS_ROLANDO)
        self.assertEqual(status.reviewed_commit, fx.HEAD)
        self.assertEqual(status.for_repair(), ())
        cats = sorted(f.category for f in status.for_rolando())
        self.assertEqual(cats, ["flag-changed-test", "needs-observation"])
        for f in status.findings:
            self.assertEqual(f.commit, fx.HEAD)
            self.assertTrue(f.id.startswith("F-"))

    def test_failed_ci_fails_at_once_without_a_review_job(self):
        self.world.jobs[RUN_ID][1]["conclusion"] = "failure"
        r = self.reviewer()
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.FAILED)
        self.assertEqual(self.runtime.texts, [])
        self.assertEqual(status.reviewed_commit, fx.HEAD)

    def test_out_of_scope_change_fails_at_once_and_goes_to_repair(self):
        self.world.files.append({"filename": "package.json", "status": "modified"})
        self.world.pr["changed_files"] = 4
        self.world.contents[("package.json", fx.HEAD)] = b"{}"
        self.world.contents[("package.json", fx.BASE)] = b"{}"
        r = self.reviewer()
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.FAILED)
        self.assertIn("scope", [f.category for f in status.for_repair()])
        self.assertEqual(self.runtime.texts, [])

    def test_reviewer_findings_route_by_category(self):
        w = World()
        w.comments = [
            review_for(
                fx.HEAD,
                findings=[
                    {
                        "category": "code",
                        "severity": "blocking",
                        "summary": "filterByStatus mutates its input",
                        "evidence": "src/books.ts:12",
                        "suggested_action": "Return a new array.",
                    },
                    {
                        "category": "product",
                        "severity": "advisory",
                        "summary": "The filter label may read oddly",
                        "evidence": "src/main.ts:4",
                        "suggested_action": "Rolando decides on the wording.",
                    },
                ],
            )
        ]
        r = self.reviewer(world=w, decisions=his_answers)
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.FAILED)
        repair = status.for_repair()
        self.assertEqual([f.category for f in repair], ["review-code"])
        self.assertEqual(repair[0].source, REVIEWER)
        self.assertEqual([f.category for f in status.for_rolando()], ["review-product"])

    def test_a_fixed_finding_is_marked_resolved_on_the_new_revision(self):
        w = World()
        code = {
            "category": "code",
            "severity": "blocking",
            "summary": "filterByStatus mutates its input",
            "evidence": "src/books.ts:12",
            "suggested_action": "Return a new array.",
        }
        w.comments = [review_for(fx.HEAD, findings=[code])]
        r = self.reviewer(world=w, decisions=his_answers)
        r.start(PR, "req-1")
        first = r.check(ATTEMPT)
        self.assertIs(first.state, ReviewState.FAILED)
        # The repair pushes a new revision; the verification pass finds it fixed.
        w = pushed(w, fx.NEW_HEAD)
        r._api = w
        r._decisions = his_answers_for(fx.NEW_HEAD)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        job = json.loads(self.runtime.texts[-1])
        self.assertEqual(job["pass"], "verify")
        self.assertEqual([f["id"] for f in job["previous_findings"]], [first.for_repair()[0].id])
        w.comments.append(review_for(fx.NEW_HEAD))
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.PASSED, status.note)
        fixed = [f for f in status.findings if f.resolved]
        self.assertEqual([f.id for f in fixed], [first.for_repair()[0].id])

    def test_passes_beyond_one_full_and_one_verification_need_an_allowance(self):
        r = self.reviewer()
        r.start(PR, "req-1")
        r._api = pushed(unreviewed(), fx.NEW_HEAD)
        r.check(ATTEMPT)
        r._api = pushed(unreviewed(), fx.OLD_HEAD)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.BLOCKED)
        self.assertIn("review passes are used", status.note)
        self.assertEqual(len(self.runtime.texts), 2)
        more = self.reviewer(policy=replace(POLICY, extra_passes=1))
        more._api = pushed(unreviewed(), "f" * 40)
        self.assertIs(more.check(ATTEMPT).state, ReviewState.RUNNING)
        self.assertEqual(len(self.runtime.texts), 3)


# --- budgets ------------------------------------------------------------------------


class BudgetTests(Base):
    def add(self, *events):
        with self.store.writer_lock():
            self.store.append(*events)

    def test_a_hold_stops_review_jobs(self):
        self.add(LedgerEvent(aev.HOLD_SET, T0, data={"reason": "manual", "note": "pause"}))
        r = self.reviewer()
        self.assertIn("can't start now", r.start(PR, "req-1"))
        self.assertEqual(self.runtime.texts, [])
        self.assertEqual(self.kinds(rev.REVIEW_JOB_INTENT), [])

    def test_high_usage_stops_review_jobs(self):
        self.add(snapshot(T0, weekly=80))
        r = self.reviewer()
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.BLOCKED)
        self.assertEqual(self.runtime.texts, [])

    def test_a_stale_usage_reading_stops_review_jobs(self):
        self.later(days=8)
        r = self.reviewer()
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.BLOCKED)
        self.assertEqual(self.runtime.texts, [])

    def test_review_launches_count_in_the_weekly_fire_allowance(self):
        self.assertEqual(policy.REVIEW_JOB_INTENT, rev.REVIEW_JOB_INTENT)
        events = [
            rev.intent(
                AttemptId(TaskId(f"t{i}"), 1),
                key=f"rv-{i}",
                cycle=f"t{i}:x",
                pass_kind="full",
                number=1,
                pr=i,
                head=fx.HEAD,
                base=fx.MAIN,
                merge_base=fx.BASE,
                digest=fx.DIGEST.value,
                now=T0 - timedelta(days=1),
            )
            for i in range(12)
        ]
        self.add(*events)
        # Workers can't fire...
        decision = AttemptGate(self.store).decide(TaskId("next"), fx.DIGEST, T0)
        self.assertIn("window-fire-cap", {b.code for b in decision.blocks})
        # ...and neither can another review job.
        r = self.reviewer()
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.BLOCKED)
        self.assertIn("12 of 12 fires", status.note)

    def test_review_launches_older_than_the_window_do_not_count(self):
        self.add(
            *[
                rev.intent(
                    AttemptId(TaskId(f"t{i}"), 1),
                    key=f"rv-{i}",
                    cycle="c",
                    pass_kind="full",
                    number=1,
                    pr=i,
                    head=fx.HEAD,
                    base=fx.MAIN,
                    merge_base=fx.BASE,
                    digest=fx.DIGEST.value,
                    now=T0 - timedelta(days=8),
                )
                for i in range(12)
            ]
        )
        decision = AttemptGate(self.store).decide(TaskId("next"), fx.DIGEST, T0)
        self.assertNotIn("window-fire-cap", {b.code for b in decision.blocks})

    def test_a_rejected_launch_still_counts(self):
        self.runtime = FakeRuntime("rejected")
        self.reviewer().start(PR, "req-1")
        view = policy.LedgerView.build(self.store.events())
        self.assertEqual(len(view.review_fires), 1)


class EnvelopeTests(unittest.TestCase):
    def job(self, **changes):
        j = ReviewJob(
            key="rv-1",
            pass_kind="full",
            repository=fx.REPO,
            pr=7,
            pr_url="https://github.com/x/y/pull/7",
            contract_digest=fx.DIGEST.value,
            head=fx.HEAD,
            base=fx.MAIN,
            merge_base=fx.BASE,
            reviewer=REVIEWER,
            contract=fx.CONTRACT,
        )
        return replace(j, **changes)

    def test_frozen_contract_round_trips(self):
        data = json.loads(review_text(self.job()))
        self.assertEqual(data["contract"]["task_id"], fx.CONTRACT["task_id"])

    def test_long_previous_findings_are_shortened_then_refused(self):
        big = [{"id": f"F-{i}", "summary": "s", "evidence": "e" * 5000} for i in range(20)]
        text = review_text(self.job(previous_findings=big))
        self.assertNotIn("evidence", json.loads(text)["previous_findings"][0])
        huge = [{"id": "F-1", "summary": "s" * 70000}]
        with self.assertRaises(ReviewTooLarge):
            review_text(self.job(previous_findings=huge))


if __name__ == "__main__":
    unittest.main()


class DryRunTests(unittest.TestCase):
    def test_dry_run_claims_one_job_sends_nothing_and_keeps_the_key(self):
        out = dry_run(unreviewed(), fx.EXAMPLE.read_text(), NUMBER, reviewer=REVIEWER, now=T0)
        self.assertEqual(out["state"], "running")
        self.assertEqual(out["head"], fx.HEAD)
        self.assertTrue(out["same_key_on_second_check"])
        self.assertEqual(out["review_jobs_claimed"], 1)
        self.assertEqual(out["review_jobs_sent"], 0)
        self.assertEqual(out["job_pass"], "full")

    def test_without_a_reviewer_account_nothing_is_claimed(self):
        out = dry_run(unreviewed(), fx.EXAMPLE.read_text(), NUMBER, now=T0)
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(out["review_jobs_claimed"], 0)

    def test_a_merged_sample_replays_as_open(self):
        w = reviewed(World())
        w.pr["state"] = "closed"
        w.runs[0]["pull_requests"] = []  # GitHub drops the link at merge
        closed = dry_run(w, fx.EXAMPLE.read_text(), NUMBER, reviewer=REVIEWER, now=T0)
        self.assertEqual(closed["state"], "not-reviewable")
        replay = dry_run(
            AsOpen(w, NUMBER, fx.REPO), fx.EXAMPLE.read_text(), NUMBER, reviewer=REVIEWER, now=T0
        )
        self.assertEqual(replay["state"], "needs-rolando")
        self.assertEqual(replay["reviewed_commit"], fx.HEAD)
        self.assertEqual(replay["review_jobs_claimed"], 0)


class FakeUsers:
    def __init__(self, users, permissions):
        self.users, self.permissions = users, permissions

    def json(self, path):
        if path.startswith("users/"):
            login = path.split("/", 1)[1]
            if login not in self.users:
                raise NotFound(path)
            return self.users[login]
        login = path.split("/")[-2]
        if login not in self.permissions:
            raise NotFound(path)
        return {"permission": self.permissions[login]}

    def raw(self, path):
        raise NotFound(path)


class IdentityTests(unittest.TestCase):
    def api(self, permission="read", kind="User"):
        return FakeUsers({REVIEWER: {"login": REVIEWER, "type": kind}}, {REVIEWER: permission})

    def test_a_user_with_read_or_no_access_qualifies(self):
        self.assertEqual(qualify_identity(self.api("read"), REVIEWER), [])
        no_access = FakeUsers({REVIEWER: {"login": REVIEWER, "type": "User"}}, {})
        self.assertEqual(qualify_identity(no_access, REVIEWER), [])

    def test_write_access_disqualifies(self):
        for level in ("write", "maintain", "admin"):
            (problem,) = qualify_identity(self.api(level), REVIEWER)
            self.assertIn("access", problem)

    def test_an_organization_or_missing_account_disqualifies(self):
        self.assertTrue(qualify_identity(self.api(kind="Organization"), REVIEWER))
        self.assertTrue(qualify_identity(FakeUsers({}, {}), REVIEWER))

    def test_the_worker_and_rolando_never_qualify(self):
        for login in (fx.WORKER, fx.REVIEWER, "rnavarrete-factory-bot[bot]"):
            self.assertTrue(qualify_identity(self.api(), login), login)


CODE_FINDING = {
    "category": "code",
    "severity": "blocking",
    "summary": "filterByStatus mutates its input",
    "evidence": "src/books.ts:12",
    "suggested_action": "Return a new array.",
}


class FindingsSurviveTests(Base):
    """A finding stays open until a review of a later revision no longer raises it."""

    def failed_review(self):
        w = World()
        w.comments = [review_for(fx.HEAD, findings=[CODE_FINDING])]
        r = self.reviewer(world=w, decisions=his_answers)
        r.start(PR, "req-1")
        first = r.check(ATTEMPT)
        self.assertIs(first.state, ReviewState.FAILED)
        return r, w, first.for_repair()[0].id

    def test_a_push_with_failing_ci_does_not_resolve_review_findings(self):
        r, w, code = self.failed_review()
        w = pushed(w, fx.NEW_HEAD)
        w.jobs[RUN_ID][1]["conclusion"] = "failure"
        r._api = w
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.FAILED)
        open_ids = {f.id for f in status.findings if not f.resolved}
        self.assertIn(code, open_ids)
        # The next green push gets a verification job that still asks about it.
        w = pushed(w, "f" * 40)
        w.jobs[RUN_ID][1]["conclusion"] = "success"
        r._api = w
        r._decisions = his_answers_for("f" * 40)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        job = json.loads(self.runtime.texts[-1])
        self.assertIn(code, [f["id"] for f in job["previous_findings"]])

    def test_ci_still_running_does_not_resolve_review_findings(self):
        r, w, code = self.failed_review()
        w.pr["head"]["sha"] = fx.NEW_HEAD
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.WAITING_CI)
        self.assertIn(code, {f.id for f in status.findings if not f.resolved})

    def test_a_timed_out_job_does_not_resolve_review_findings(self):
        r, w, code = self.failed_review()
        r._api = pushed(w, fx.NEW_HEAD)
        r._decisions = his_answers_for(fx.NEW_HEAD)
        r.check(ATTEMPT)
        self.later(hours=3)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.UNKNOWN)
        self.assertIn(code, {f.id for f in status.findings if not f.resolved})

    def test_a_reworded_finding_keeps_its_id_when_the_reviewer_names_it(self):
        r, w, code = self.failed_review()
        w = pushed(w, fx.NEW_HEAD)
        reworded = {**CODE_FINDING, "summary": "filterByStatus still changes its input", "id": code}
        w.comments.append(review_for(fx.NEW_HEAD, findings=[reworded]))
        r._api = w
        r._decisions = his_answers_for(fx.NEW_HEAD)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.FAILED)
        self.assertEqual([f.id for f in status.for_repair()], [code])
        self.assertEqual([f for f in status.findings if f.resolved], [])

    def test_a_claimed_id_of_another_category_is_ignored(self):
        r, w, code = self.failed_review()
        w = pushed(w, fx.NEW_HEAD)
        other = {**CODE_FINDING, "category": "test", "summary": "weak test", "id": code}
        w.comments.append(review_for(fx.NEW_HEAD, findings=[other]))
        r._api = w
        r._decisions = his_answers_for(fx.NEW_HEAD)
        status = r.check(ATTEMPT)
        self.assertNotIn(code, [f.id for f in status.for_repair()])
        self.assertIn(code, [f.id for f in status.findings if f.resolved])


class LedgerRobustnessTests(Base):
    def add(self, *events):
        with self.store.writer_lock():
            self.store.append(*events)

    def bad_verdict(self, **data):
        base = {
            "key": "rv-x",
            "cycle": rev.cycle_of(ATTEMPT.task, fx.DIGEST.value),
            "verdict": "passed",
            "head": fx.HEAD,
            "base": fx.MAIN,
            "merge_base": fx.BASE,
            "digest": fx.DIGEST.value,
            "pr": NUMBER,
            "review_url": None,
            "findings": [],
        }
        base.update(data)
        return LedgerEvent(rev.REVIEW_VERDICT, T0, ATTEMPT.task, ATTEMPT, data=base)

    def test_malformed_verdict_records_grant_nothing_and_break_nothing(self):
        r = self.reviewer()
        r.start(PR, "req-1")
        self.add(
            self.bad_verdict(verdict="bogus"),
            self.bad_verdict(findings=[{"id": "F-1"}]),
            self.bad_verdict(findings="oops"),
            self.bad_verdict(review_url={"x": 1}),
        )
        self.assertIsNone(r.evidence(ATTEMPT))
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)

    def test_a_malformed_intent_does_not_count(self):
        bad = LedgerEvent(rev.REVIEW_JOB_INTENT, T0, ATTEMPT.task, ATTEMPT, data={"key": "k"})
        self.add(bad)
        self.assertEqual(rev.ReviewView.build(self.store.events()).intents_at, [])


class RateLimitTests(Base):
    def test_a_429_makes_reviews_and_workers_wait(self):
        self.runtime = FakeRuntime(
            LaunchResult(LaunchOutcome.NOT_LAUNCHED, 429, retry_after_seconds=3600)
        )
        r = self.reviewer()
        r.start(PR, "req-1")
        self.assertEqual(len(self.kinds(aev.RATE_LIMIT_WAIT)), 1)
        self.later(minutes=1)
        status = r.check(ATTEMPT)
        self.assertIs(status.state, ReviewState.BLOCKED)
        self.assertEqual(len(self.runtime.texts), 1)
        decision = AttemptGate(self.store).decide(TaskId("next"), fx.DIGEST, self.now)
        self.assertIn("rate-limited", {b.code for b in decision.blocks})
        # After the wait it is tried again: a 429 doesn't use up the job's tries.
        self.later(hours=1)
        self.assertIs(r.check(ATTEMPT).state, ReviewState.RUNNING)
        self.assertEqual(len(self.runtime.texts), 2)

    def test_used_up_usage_holds_the_factory(self):
        self.runtime = FakeRuntime(
            LaunchResult(LaunchOutcome.NOT_LAUNCHED, 403, response_body="weekly usage limit")
        )
        r = self.reviewer()
        r.start(PR, "req-1")
        holds = self.kinds(aev.HOLD_SET)
        self.assertEqual([h.data["reason"] for h in holds], [aev.SUBSCRIPTION_EXHAUSTED])
        decision = AttemptGate(self.store).decide(TaskId("next"), fx.DIGEST, self.now)
        self.assertIn("hold", {b.code for b in decision.blocks})
        self.assertIs(r.check(ATTEMPT).state, ReviewState.BLOCKED)
        self.assertEqual(len(self.runtime.texts), 1)

    def test_ctrl_c_mid_launch_records_the_unknown_answer(self):
        lost = LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, detail="interrupted")
        self.runtime = FakeRuntime(LaunchInterrupted(lost))
        with self.assertRaises(LaunchInterrupted):
            self.reviewer().start(PR, "req-1")
        outcomes = [e.data["outcome"] for e in self.kinds(rev.REVIEW_JOB_LAUNCHED)]
        self.assertEqual(outcomes, ["launch-outcome-unknown"])


class SupersededEvidenceTests(Base):
    def passed(self):
        self.world = reviewed(World())
        r = self.reviewer(decisions=his_answers)
        r.start(PR, "req-1")
        self.assertIs(r.check(ATTEMPT).state, ReviewState.PASSED)
        return r

    def test_a_moved_base_with_ci_pending_withdraws_the_pass_from_evidence(self):
        r = self.passed()
        self.world.pr["base"]["sha"] = fx.NEW_MAIN
        self.world.runs = []
        self.assertIs(r.check(ATTEMPT).state, ReviewState.WAITING_CI)
        evidence = r.evidence(ATTEMPT)
        self.assertFalse(evidence.passed)
        self.assertEqual(evidence.reviewed_commit, "")

    def test_closed_without_merging_is_not_a_pass(self):
        r = self.passed()
        self.world.pr["state"] = "closed"
        self.world.pr["merged"] = False
        self.assertIs(r.check(ATTEMPT).state, ReviewState.NOT_REVIEWABLE)
        self.assertFalse(r.evidence(ATTEMPT).passed)

    def test_retargeted_after_a_pass_withdraws_it(self):
        r = self.passed()
        self.world.pr["base"]["ref"] = "release"
        self.assertIs(r.check(ATTEMPT).state, ReviewState.NOT_REVIEWABLE)
        self.assertFalse(r.evidence(ATTEMPT).passed)

    def test_an_untrusted_contract_goes_to_rolando_not_repair(self):
        changed = {**json.loads(fx.EXAMPLE.read_text()), "goal": "something else"}
        self.world = reviewed(World())
        r = AutoReviewer(
            self.store,
            self.world,
            self.runtime,
            lambda a: (changed, fx.DIGEST),
            policy=POLICY,
            clock=lambda: self.now,
        )
        r.start(PR, "req-1")
        status = r.check(ATTEMPT)
        self.assertIsNot(status.state, ReviewState.FAILED)
        self.assertEqual(status.for_repair(), ())
        self.assertTrue(status.for_rolando())
