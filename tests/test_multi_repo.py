"""A second repository (neon-war) beside the pilot.

The repository table in ``controller/targets.py`` decides what onboarding may
name, each attempt records the repository its contract names, every GitHub
read for an attempt goes to that repository, and each repository has its own
rolling weekly cap (docs/limits.md section 3, amended 2026-10-26).
"""

import hashlib
import json
import unittest
from datetime import timedelta
from pathlib import Path

from controller import targets
from controller.attempts import PILOT_LIMITS, AttemptGate, DispatchRefused
from controller.attempts import events as ev
from controller.attempts.policy import REVIEW_JOB_INTENT, LedgerView
from controller.interfaces import AttemptId, ContractDigest, LedgerEvent, RunId, TaskId
from controller.review import codex_job
from controller.review.reviewer import Revision
from controller.service import onboarding
from controller.service.secrets import NAMES
from tests.test_approval import example
from tests.test_attempts import NOW, MemoryLedger, launched, not_launched
from tests.test_dispatch import DispatchCase
from tests.test_recovery import FakeGitHub
from tests.test_review_workflow import Case as ReviewCase
from tests.test_service import TRIG, config
from tests.test_todo_move_approval import AuthorizerCase, resign

PILOT = targets.PILOT_REPO
NEON = targets.NEON_WAR_REPO
NEON_TRIG = "trig_01NeonWarRoutine"
DIGEST = ContractDigest.of(b"contract")


class TargetTableTests(unittest.TestCase):
    def test_both_repositories_are_listed_with_their_own_start_key(self):
        self.assertEqual(targets.REPOSITORIES, frozenset({PILOT, NEON}))
        self.assertEqual(targets.routine_for(PILOT), targets.PILOT_ROUTINE)
        for repo in targets.REPOSITORIES:
            with self.subTest(repo):
                self.assertIn(targets.TOKEN_SECRETS[repo], NAMES)
        self.assertEqual(len(set(targets.TOKEN_SECRETS.values())), len(targets.TOKEN_SECRETS))

    def test_a_routine_finds_its_own_start_key_only(self):
        self.assertEqual(targets.token_secret(targets.PILOT_ROUTINE), "routine-token")
        with self.assertRaises(KeyError):
            targets.token_secret("trig_01Unknown")

    def test_a_repository_without_a_routine_is_not_live(self):
        live = targets.live_routines()
        for repo, trig in targets.ROUTINES.items():
            self.assertEqual(repo in live, trig is not None)


def entry(repo, trig, pid):
    return {
        "linear_project_id": pid,
        "name": pid,
        "repository": repo,
        "routine_id": trig,
        "allowed_actions": ["modify-files", "add-tests"],
        "checks": ["npm test"],
        "max_attempts": 2,
    }


def doc(*entries):
    return json.dumps(
        {"format": onboarding.FORMAT, "intake_enabled": True, "projects": list(entries)}
    ).encode()


class OnboardingTests(unittest.TestCase):
    both = {PILOT: TRIG, NEON: NEON_TRIG}

    def test_two_repositories_each_with_its_own_routine(self):
        parsed = onboarding.parse(
            doc(entry(PILOT, TRIG, "p-pilot"), entry(NEON, NEON_TRIG, "p-neon")), targets=self.both
        )
        self.assertEqual(parsed.project("p-neon").repository, NEON)
        self.assertEqual(parsed.project("p-neon").routine_id, NEON_TRIG)
        self.assertEqual(parsed.project("p-pilot").routine_id, TRIG)

    def test_a_routine_bound_to_the_other_repository_is_refused(self):
        for raw in (doc(entry(NEON, TRIG, "p")), doc(entry(PILOT, NEON_TRIG, "p"))):
            with self.subTest(raw), self.assertRaisesRegex(onboarding.OnboardingError, "routine"):
                onboarding.parse(raw, targets=self.both)

    def test_an_unlisted_repository_is_refused(self):
        with self.assertRaisesRegex(onboarding.OnboardingError, "can only work on"):
            onboarding.parse(doc(entry("someone/else", TRIG, "p")), targets=self.both)

    def test_by_default_only_the_table_counts(self):
        """Without a routine in the table, neon-war can't be onboarded at all;
        the pilot entry still loads with the table's routine."""
        pilot = onboarding.parse(doc(entry(PILOT, targets.PILOT_ROUTINE, "p")))
        self.assertEqual(pilot.project("p").repository, PILOT)
        if targets.NEON_WAR_ROUTINE is None:
            with self.assertRaises(onboarding.OnboardingError):
                onboarding.parse(doc(entry(NEON, NEON_TRIG, "n")))

    def test_the_single_pair_form_still_works(self):
        raw = json.dumps(config()).encode()
        self.assertTrue(onboarding.parse(raw, repository=PILOT, routine_id=TRIG).projects)
        with self.assertRaises(TypeError):
            onboarding.parse(raw, targets=self.both, repository=PILOT, routine_id=TRIG)


class WeeklyCapTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryLedger()
        self.gate = AttemptGate(self.store)
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))
        self.seeded = 0

    def seed(self, count, repository, at=NOW - timedelta(days=1)):
        for _ in range(count):
            self.seeded += 1
            task = TaskId(f"seed-{self.seeded}")
            run = self.gate.reserve(task, DIGEST, at, repository=repository)
            self.gate.record_launch(run, not_launched(), at)

    def codes(self, task, repository):
        decision = self.gate.decide(task, DIGEST, NOW, repository=repository)
        return {b.code for b in decision.blocks}, {a.code for a in decision.alerts}

    def test_the_caps_are_the_signed_numbers(self):
        self.assertEqual(PILOT_LIMITS.window_cap(PILOT), (12, 9))
        self.assertEqual(PILOT_LIMITS.window_cap(NEON), (24, 18))
        self.assertEqual(PILOT_LIMITS.window_cap("someone/else"), (12, 9))

    def test_the_amendment_matches_its_signed_hash(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "limits.md").read_text()
        start = text.index("## Amendment 2026-10-26")
        body, _, signed = text[start:].partition("- Version signed: `")
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), signed[:64])
        self.assertIn("**24 for neon-war** (alert at 18)", body)

    def test_the_reservation_records_its_repository(self):
        self.gate.reserve(TaskId("t"), DIGEST, NOW, repository=NEON)
        reserved = [s.event for s in self.store.events() if s.event.kind == ev.ATTEMPT_RESERVED]
        self.assertEqual(reserved[0].data["repository"], NEON)
        self.assertEqual(LedgerView.build(self.store.events()).task_repository(TaskId("t")), NEON)

    def test_a_full_pilot_week_does_not_block_neon_war(self):
        self.seed(12, PILOT)
        self.assertIn("window-fire-cap", self.codes(TaskId("p"), PILOT)[0])
        self.assertNotIn("window-fire-cap", self.codes(TaskId("n"), NEON)[0])
        self.gate.reserve(TaskId("n"), DIGEST, NOW, repository=NEON)

    def test_neon_war_stops_at_24_and_alerts_at_18(self):
        self.seed(17, NEON)
        self.assertNotIn("window-fires-high", self.codes(TaskId("n"), NEON)[1])
        self.seed(1, NEON)
        self.assertIn("window-fires-high", self.codes(TaskId("n"), NEON)[1])
        self.seed(6, NEON)
        blocks, _ = self.codes(TaskId("n"), NEON)
        self.assertIn("window-fire-cap", blocks)
        self.assertNotIn("window-fire-cap", self.codes(TaskId("p"), PILOT)[0])
        with self.assertRaises(DispatchRefused) as cm:
            self.gate.reserve(TaskId("n"), DIGEST, NOW, repository=NEON)
        detail = next(b.detail for b in cm.exception.decision.blocks if b.code == "window-fire-cap")
        self.assertIn("24 of 24", detail)
        self.assertIn(NEON, detail)

    def test_the_neon_war_window_rolls(self):
        self.seed(24, NEON, at=NOW - timedelta(days=7, minutes=1))
        self.gate.reserve(TaskId("n"), DIGEST, NOW, repository=NEON)

    def test_reservations_from_before_repositories_are_the_pilots(self):
        with self.store.writer_lock():
            for i in range(12):
                a = AttemptId(TaskId(f"old-{i}"), 1)
                self.store.append(ev.attempt_reserved(a, DIGEST, NOW - timedelta(days=1)))
                self.store.append(
                    LedgerEvent(ev.FIRE_INTENT, NOW - timedelta(days=1), a.task, a, RunId(a, 1), {})
                )
        self.assertIn("window-fire-cap", self.codes(TaskId("p"), PILOT)[0])
        self.assertNotIn("window-fire-cap", self.codes(TaskId("n"), NEON)[0])

    def test_without_a_repository_the_tasks_own_attempts_decide(self):
        first = self.gate.reserve(TaskId("n"), DIGEST, NOW - timedelta(days=8), repository=NEON)
        self.gate.record_launch(first, not_launched(), NOW - timedelta(days=8))
        self.seed(24, NEON)
        self.assertIn("window-fire-cap", self.codes(TaskId("n"), None)[0])
        self.assertNotIn("window-fire-cap", self.codes(TaskId("fresh"), None)[0])

    def test_reviews_count_in_the_reviewed_attempts_repository(self):
        self.seed(23, NEON)
        attempt = AttemptId(TaskId("seed-1"), 1)
        with self.store.writer_lock():
            self.store.append(
                LedgerEvent(
                    REVIEW_JOB_INTENT, NOW - timedelta(hours=1), attempt.task, attempt, data={}
                )
            )
        self.assertIn("window-fire-cap", self.codes(TaskId("n"), NEON)[0])
        self.assertNotIn("window-fire-cap", self.codes(TaskId("p"), PILOT)[0])

    def test_a_review_of_an_unknown_attempt_counts_everywhere(self):
        self.seed(11, PILOT)
        self.seed(23, NEON)
        stray = AttemptId(TaskId("nobody"), 1)
        with self.store.writer_lock():
            self.store.append(
                LedgerEvent(REVIEW_JOB_INTENT, NOW - timedelta(hours=1), stray.task, stray, data={})
            )
        self.assertIn("window-fire-cap", self.codes(TaskId("p"), PILOT)[0])
        self.assertIn("window-fire-cap", self.codes(TaskId("n"), NEON)[0])


class RecordingGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.repos = []

    def branch_head(self, repo, branch):
        self.repos.append(repo)
        return super().branch_head(repo, branch)

    def recent_pulls(self, repo):
        self.repos.append(repo)
        return super().recent_pulls(repo)

    def pulls_for_branch(self, repo, branch):
        self.repos.append(repo)
        return super().pulls_for_branch(repo, branch)


class TwoRepoDispatchTests(DispatchCase):
    def build(self):
        super().build()
        from controller.dispatch import Dispatcher
        from controller.recovery import Recovery
        from tests.test_dispatch import BOT

        self.github = RecordingGitHub()
        self.recovery = Recovery(
            self.store,
            self.approvals,
            self.github,
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
            routines={PILOT: TRIG, NEON: NEON_TRIG},
            adapter=self.make_adapter,
            start_key=self.start_key,
            model_config_version="test",
            now=self.clock,
            backup=self.backups.append,
            sleep=lambda seconds: None,
        )

    def test_each_contract_fires_its_own_repositorys_routine(self):
        neon = example(repository=NEON, task_id="neon-1")
        self.adapter.answers = [launched(1), launched(2)]
        self.dispatch(neon)
        self.assertEqual(self.adapters_made[-1][0], NEON_TRIG)
        self.assertEqual(self.keys_fetched[-1], NEON_TRIG)
        self.assertEqual(self.base.calls[-1][0], NEON)
        reserved = self.kinds(ev.ATTEMPT_RESERVED, TaskId("neon-1"))
        self.assertEqual(reserved[0].data["repository"], NEON)

    def test_the_pilot_contract_still_fires_the_pilot_routine(self):
        self.dispatch()
        self.assertEqual(self.adapters_made[-1][0], TRIG)
        self.assertEqual(self.base.calls[-1][0], PILOT)
        self.assertEqual(self.kinds(ev.ATTEMPT_RESERVED)[0].data["repository"], PILOT)

    def test_an_unlisted_repository_is_refused_before_anything(self):
        other = example(repository="someone/elsewhere")
        self.assertEqual(self.refused(other), {"wrong-repository"})
        self.assertEqual(self.base.calls, [])
        self.assert_nothing_sent()

    def test_recovery_reads_the_attempts_own_repository(self):
        neon = example(repository=NEON, task_id="neon-1")
        self.dispatch(neon)
        self.github.repos.clear()
        self.recovery.reconcile(AttemptId(TaskId("neon-1"), 1), NOW + timedelta(minutes=1))
        self.assertTrue(self.github.repos)
        self.assertEqual(set(self.github.repos), {NEON})


class SourceRoutineBindingTests(AuthorizerCase):
    """Approvals given routines by repository: a Todo-move record counts for
    a contract only if it names that contract's repository's routine."""

    def test_the_record_counts_under_its_own_repository(self):
        self.append(self.authorize(self.move()))
        self.assertTrue(self.check(routine={PILOT: TRIG, NEON: NEON_TRIG}).approved)

    def test_a_record_naming_another_repositorys_routine_does_not_count(self):
        event = self.authorize(self.move())
        self.append(resign(event, routine_id=NEON_TRIG))
        self.assertFalse(self.check(routine={PILOT: TRIG, NEON: NEON_TRIG}).approved)

    def test_a_contract_for_a_repository_without_a_routine_gets_nothing(self):
        self.append(self.authorize(self.move()))
        self.assertFalse(self.check(routine={NEON: TRIG}).approved)


class ReviewRequestTests(ReviewCase):
    def rekeyed(self, repository):
        self.started()
        data = json.loads(self.text())
        data["repository"] = repository
        rev = Revision(
            repository,
            data["pr"],
            data["contract_digest"],
            data["head"],
            data["base"],
            data["merge_base"],
        )
        data["key"] = rev.key()
        return json.dumps(data), data["key"]

    def test_a_neon_war_review_request_is_accepted(self):
        text, key = self.rekeyed(NEON)
        self.assertEqual(codex_job.check_request(text, key)["repository"], NEON)

    def test_an_unlisted_repository_is_refused_even_with_a_matching_key(self):
        text, key = self.rekeyed("someone/else")
        with self.assertRaisesRegex(codex_job.BadRequest, "repository"):
            codex_job.check_request(text, key)


del DispatchCase, AuthorizerCase, ReviewCase

if __name__ == "__main__":
    unittest.main()
