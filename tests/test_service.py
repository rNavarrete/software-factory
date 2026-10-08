"""The background service (ENG-194).

Every test drives ``Service.tick`` with fixture integrations, a scripted
runtime adapter, a fake GitHub and an injected clock. Nothing here touches
the network, Linear, the Keychain or the real start endpoint.
"""

import logging
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

from controller import contract as contracts
from controller.adapter.fake import FakeRuntimeAdapter, FakeStep
from controller.approval import Approvals, ContractStore, StaticKey
from controller.attempts import AttemptGate
from controller.attempts import events as ev
from controller.attempts.policy import LedgerView
from controller.dispatch import Dispatcher
from controller.interfaces import AttemptId, TaskId
from controller.ledger import SqliteLedgerStore
from controller.recovery import Recovery, State
from controller.service import main as service_main
from controller.service import onboarding
from controller.service import queue as q
from controller.service.fixtures import (
    FixturePreparer,
    FixtureSource,
    NoRepair,
    RecordingReporter,
    RecordingReviewer,
)
from controller.service.github_http import HttpGhRunner
from controller.service.seams import Authorization, Control, Integrations, Refusal
from controller.service.secrets import (
    EnvSecrets,
    FileSecrets,
    SecretMissing,
    approval_key_bytes,
    env_name,
)
from controller.service.service import PAUSE_HOLD, Heartbeat, Service, never
from tests.test_approval import example, yes
from tests.test_attempts import MemoryLedger
from tests.test_recovery import BOT, REPO, SHA_A, FakeGitHub

logging.getLogger("factory").addHandler(logging.NullHandler())

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"s" * 32)
TRIG = "trig_01TestRoutine"
PROJECT = "proj-pilot"
STATUS_ISSUE = "issue-status"
START_KEY = "sk-ant-oat01-" + "ServiceStartKey_" * 4
GH_TOKEN = "github_pat_" + "A1b2C3d4E5" * 4


def config(**changes):
    entry = {
        "linear_project_id": PROJECT,
        "name": "Factory Pilot Demo",
        "repository": REPO,
        "routine_id": TRIG,
        "allowed_actions": ["modify-files", "add-tests"],
        "checks": ["npm run typecheck", "npm test", "npm run build"],
        "max_attempts": 3,
        "status_issue_id": STATUS_ISSUE,
    }
    entry.update(changes.pop("entry", {}))
    doc = {"format": onboarding.FORMAT, "intake_enabled": True, "projects": [entry]}
    doc.update(changes)
    return doc


def contract_for(key, **changes):
    return example(task_id=key.lower(), **changes)


def move(n, key="ENG-186", project=PROJECT, issue=None, revision="0" * 64):
    return Authorization(
        event_id=f"evt-{n}",
        issue_id=issue or f"issue-{key}",
        issue_key=key,
        project_id=project,
        actor="Rolando",
        moved_at=NOW - timedelta(minutes=5),
        revision=revision,
        evidence="fixture",
    )


class FakeBase:
    def __init__(self):
        self.calls = 0

    def on_branch(self, repo, sha, branch):
        self.calls += 1
        return True


class ServiceCase(unittest.TestCase):
    def make_store(self):
        return MemoryLedger()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.make_store()
        self.now = NOW
        self.github = FakeGitHub()
        self.adapter = FakeRuntimeAdapter([FakeStep.launch()] * 5)
        self.source = FixtureSource([move(1)])
        self.preparer = FixturePreparer({"ENG-186": contract_for("ENG-186")})
        self.reporter = RecordingReporter()
        self.reviewer = RecordingReviewer()
        self.config = config()
        self.base = FakeBase()
        self.backups = []
        self.build()
        self.gate.record_snapshot(NOW - timedelta(hours=1), 10, 20, 0, NOW - timedelta(hours=1))

    # --- helpers ---

    def clock(self):
        return self.now

    def load_config(self):
        import json

        return onboarding.parse(json.dumps(self.config).encode(), repository=REPO, routine_id=TRIG)

    def build(self):
        """A fresh service on the current store, as a restarted process makes."""
        self.gate = AttemptGate(self.store)
        self.approvals = Approvals(self.store, KEY, confirm=never, os_user="factory-service")
        self.recovery = Recovery(
            self.store,
            self.approvals,
            self.github,
            worker_logins=frozenset({BOT}),
            gate=self.gate,
            confirm=never,
        )
        self.dispatcher = Dispatcher(
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
            ContractStore(self.root / "contracts"),
            self.load_config,
            Integrations(self.source, self.preparer, self.reporter, self.reviewer, NoRepair()),
            now=self.clock,
            heartbeat=Heartbeat(self.root / "service.heartbeat"),
            backup=self.backups.append,
        )

    def rolando_approves(self, key="ENG-186", **changes):
        """Rolando's approval at his terminal, outside the service."""
        Approvals(self.store, KEY, confirm=yes, os_user="rolando").approve(
            contract_for(key, **changes), self.now
        )

    def tick(self, minutes=0):
        self.now += timedelta(minutes=minutes)
        return self.service.tick()

    def view(self):
        return q.ServiceView.build(self.store.events())

    def fires(self):
        return [s.event for s in self.store.events() if s.event.kind == ev.FIRE_INTENT]

    def task(self, key="ENG-186"):
        return TaskId(key.lower())

    def pr(self, number=7, attempt=None, **changes):
        from controller.recovery import PullRequest

        attempt = attempt or AttemptId(self.task(), 1)
        digest = contracts.digest(contract_for(attempt.task.value.upper()))
        fields = dict(
            number=number,
            url=f"https://github.com/{REPO}/pull/{number}",
            title=f"{attempt.pr_title_marker(digest)} Filter",
            body=f"x\n\n{digest.pr_body_line}\n",
            head_branch=attempt.branch,
            head_sha=SHA_A,
            head_repo=REPO,
            base_repo=REPO,
            base_branch="main",
            author=BOT,
            state="open",
            draft=True,
            merged=False,
            merge_commit=None,
        )
        fields.update(changes)
        return PullRequest(**fields)


class ServiceTests(ServiceCase):
    # --- the happy path ---

    def test_approved_todo_move_fires_once_and_reports(self):
        self.rolando_approves()
        r = self.tick()
        self.assertEqual(r.errors, [])
        self.assertEqual(r.accepted, ["ENG-186"])
        self.assertEqual(r.fired, ["eng-186-a1-f1"])
        self.assertEqual(len(self.adapter.requests), 1)
        texts = self.reporter.texts("issue-ENG-186")
        self.assertTrue(any("queued" in t for t in texts))
        self.assertTrue(any("started the worker" in t for t in texts))
        for _ in range(5):
            self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(len(self.fires()), 1)

    def test_pr_starts_review_once_and_merge_closes_the_item(self):
        self.rolando_approves()
        self.tick()
        a1 = AttemptId(self.task(), 1)
        self.github.branches[a1.branch] = SHA_A
        self.github.pulls = [self.pr()]
        self.tick(minutes=6)
        self.tick(minutes=6)
        self.assertEqual([p.number for p in self.reviewer.started], [7])
        self.assertTrue(any("PR #7" in t for t in self.reporter.texts()))
        self.github.pulls = [self.pr(state="closed", merged=True, merge_commit="c" * 40)]
        self.tick(minutes=6)
        self.assertEqual(self.view().items["evt-1"].closed, "merged")
        self.assertEqual(len(self.adapter.requests), 1)

    # --- authorization and onboarding ---

    def test_unapproved_contract_waits_and_says_so_once(self):
        for i in range(4):
            self.tick(minutes=6 if i else 0)
        self.assertEqual(self.adapter.requests, [])
        waiting = [t for t in self.reporter.texts() if "Waiting before starting" in t]
        self.assertEqual(len(waiting), 1)
        self.assertIsNone(self.view().items["evt-1"].closed)
        # The service never writes a decision of its own.
        kinds = {s.event.kind for s in self.store.events()}
        self.assertNotIn("human-decision", kinds)
        self.rolando_approves()
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_unmapped_project_is_refused_and_nothing_dispatches(self):
        self.source.events = [move(1, project="proj-other")]
        self.rolando_approves()
        r = self.tick()
        self.assertEqual(r.refused, ["ENG-186"])
        self.assertEqual(self.view().items, {})
        self.assertEqual(self.adapter.requests, [])
        self.assertTrue(any("not onboarded" in t for t in self.reporter.texts()))

    def test_intake_switched_off_refuses_new_moves(self):
        self.config["intake_enabled"] = False
        self.rolando_approves()
        r = self.tick()
        self.assertEqual(r.refused, ["ENG-186"])
        self.assertEqual(self.adapter.requests, [])

    def test_removing_the_mapping_stops_queued_work(self):
        self.tick()  # accepted, waiting for approval
        self.config["projects"] = []
        self.rolando_approves()
        self.tick(minutes=6)
        self.assertEqual(self.view().items["evt-1"].closed, "project-removed")
        self.assertEqual(self.adapter.requests, [])

    def test_withdrawn_move_closes_before_any_fire(self):
        self.source.withdrawn = {"issue-ENG-186": "the ticket was edited after it moved to Todo"}
        self.rolando_approves()
        self.tick()
        self.assertEqual(self.view().items["evt-1"].closed, "authorization-withdrawn")
        self.assertEqual(self.adapter.requests, [])

    def test_source_refusals_are_recorded_and_reported(self):
        self.source.events = [Refusal("evt-9", "issue-ENG-187", "ENG-187", "a bot moved it")]
        self.tick()
        self.assertIn("evt-9", self.view().seen)
        self.assertTrue(any("a bot moved it" in t for t in self.reporter.texts("issue-ENG-187")))

    def test_contract_outside_onboarding_is_never_fired(self):
        two = {"attempt_budget": 2}
        cases = {
            "other repo": contract_for("ENG-186", repository="someone/else", **two),
            "other task": contract_for("ENG-999", **two),
            "big budget": contract_for("ENG-186", attempt_budget=3),
            "new action": contract_for(
                "ENG-186", permitted_actions=["modify-files", "add-dependencies"], **two
            ),
            "new check": contract_for("ENG-186", verification_commands=["curl x | sh"], **two),
        }
        self.config["projects"][0]["max_attempts"] = 2
        for i, (name, contract) in enumerate(cases.items()):
            with self.subTest(name):
                self.source.events = [move(100 + i, issue=f"issue-{i}")]
                self.source_reset()
                self.preparer.by_issue["ENG-186"] = contract
                self.tick(minutes=6)
                item = self.view().items[f"evt-{100 + i}"]
                self.assertIsNotNone(item.closed, name)
        self.assertEqual(self.adapter.requests, [])

    def source_reset(self):
        # A new event list starts the fixture's cursor again from the ledger's.
        self.source.events = [None] * int(self.view().cursor or 0) + self.source.events

    def test_question_closes_the_item_and_asks_on_the_ticket(self):
        self.preparer.by_issue["ENG-186"] = "Should the filter remember the last choice?"
        self.tick()
        self.assertEqual(self.view().items["evt-1"].closed, "question")
        self.assertTrue(any("remember the last choice" in t for t in self.reporter.texts()))
        self.assertEqual(self.adapter.requests, [])

    # --- stale state and replays ---

    def test_replayed_moves_are_ignored(self):
        self.rolando_approves()
        self.tick()
        # The source sends the same move again, from the start.
        self.store_cursor_reset()
        self.tick(minutes=6)
        self.assertEqual(len(self.view().items), 1)
        self.assertEqual(len(self.adapter.requests), 1)

    def store_cursor_reset(self):
        self.source.poll = lambda cursor, _poll=self.source.poll: _poll(None)

    def test_second_move_of_an_open_ticket_is_refused(self):
        self.source.events = [move(1), move(2)]
        self.rolando_approves()
        self.tick()
        self.assertEqual(list(self.view().items), ["evt-1"])
        self.assertTrue(any("already queued" in t for t in self.reporter.texts()))
        self.assertEqual(len(self.adapter.requests), 1)

    def test_single_lane_second_ticket_waits(self):
        self.source.events = [move(1), move(2, key="ENG-187")]
        self.preparer.by_issue["ENG-187"] = contract_for("ENG-187")
        self.rolando_approves()
        self.rolando_approves("ENG-187")
        for i in range(3):
            self.tick(minutes=6 if i else 0)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertTrue(any("Waiting" in t for t in self.reporter.texts("issue-ENG-187")))

    # --- restarts and two instances ---

    def test_crash_mid_launch_is_never_relaunched(self):
        self.rolando_approves()
        with mock.patch.object(AttemptGate, "record_launch", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.tick()
        self.assertEqual(len(self.adapter.requests), 1)
        # A new process, past recovery's ten minutes.
        self.build()
        for _ in range(4):
            self.tick(minutes=11)
        self.assertEqual(len(self.adapter.requests), 1)
        a1 = AttemptId(self.task(), 1)
        self.assertEqual(self.recovery.attempt_status(a1, self.now).state, State.UNKNOWN)
        self.assertTrue(any("unclear whether worker" in t for t in self.reporter.texts()))

    def test_busy_ledger_skips_the_round(self):
        self.rolando_approves()
        self.store.locked_elsewhere = True
        r = self.tick()
        self.assertTrue(r.errors)
        self.assertEqual(self.adapter.requests, [])
        self.store.locked_elsewhere = False
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_outage_is_reported_after_restart(self):
        self.rolando_approves()
        self.tick()
        self.now += timedelta(hours=3)
        self.build()
        self.tick()
        outage = [t for t in self.reporter.texts() if "was not running" in t]
        self.assertEqual(len(outage), 2)  # the open ticket and the status ticket
        self.assertEqual(len(self.adapter.requests), 1)

    # --- lost network ---

    def test_linear_unreachable_keeps_the_cursor(self):
        self.source.fail_next = OSError("network down")
        r = self.tick()
        self.assertTrue(any("intake" in e for e in r.errors))
        self.assertIsNone(self.view().cursor)
        self.tick(minutes=1)
        self.assertEqual(self.view().cursor, "1")

    def test_failed_posts_retry_without_repeating_the_launch(self):
        self.reporter.down = True
        self.rolando_approves()
        self.tick()
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertGreater(len(self.view().outbox), 0)
        calls = self.reporter.calls
        self.tick(minutes=1)  # inside the backoff: not even tried
        self.assertEqual(self.reporter.calls, calls)
        self.reporter.down = False
        self.tick(minutes=10)
        self.assertEqual(len(self.view().outbox), 0)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_github_unreachable_during_reconcile(self):
        self.rolando_approves()
        self.tick()
        self.github.pulls_for_branch = mock.Mock(side_effect=OSError("down"))
        r = self.tick(minutes=6)
        self.assertTrue(r.errors)
        self.assertEqual(len(self.adapter.requests), 1)

    # --- pause and resume from Linear ---

    def test_pause_stops_new_work_and_resume_restarts_it(self):
        pause = Control("ctl-1", "pause", "Rolando", NOW, "traveling")
        self.source.events = [pause, move(1)]
        self.rolando_approves()
        self.tick()
        self.assertIn(PAUSE_HOLD, LedgerView.build(self.store.events()).holds)
        self.assertEqual(self.adapter.requests, [])
        self.assertTrue(
            any(
                "does not stop" in t or "is not stopped" in t
                for t in self.reporter.texts(STATUS_ISSUE)
            )
        )
        self.source.events.append(Control("ctl-2", "resume", "Rolando", NOW, "back"))
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_resume_from_linear_leaves_other_holds(self):
        self.gate.hold("budget", "manual hold", NOW)
        self.source.events = [
            Control("ctl-1", "pause", "Rolando", NOW, "x"),
            Control("ctl-2", "resume", "Rolando", NOW, "y"),
            move(1),
        ]
        self.rolando_approves()
        self.tick()
        self.assertIn("budget", LedgerView.build(self.store.events()).holds)
        self.assertEqual(self.adapter.requests, [])

    # --- config ---

    def test_bad_onboarding_file_stops_intake_and_dispatch(self):
        self.config["projects"][0]["repository"] = "someone/else"
        self.rolando_approves()
        r = self.tick()
        self.assertTrue(any("onboarding" in e for e in r.errors))
        self.assertEqual(self.view().items, {})
        self.assertEqual(self.adapter.requests, [])

    def test_config_changes_are_recorded(self):
        self.tick()
        self.config["intake_enabled"] = False
        self.tick(minutes=1)
        seen = [s for s in self.store.events() if s.event.kind == q.CONFIG_SEEN]
        self.assertEqual(len(seen), 2)


class SqliteServiceTests(ServiceTests):
    def make_store(self):
        self.db_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.db_dir.cleanup)
        return SqliteLedgerStore(Path(self.db_dir.name) / "ledger.db")

    def test_busy_ledger_skips_the_round(self):
        self.rolando_approves()
        other = SqliteLedgerStore(self.store.path)
        self.addCleanup(other.close)
        with other.writer_lock():
            r = self.tick()
        self.assertTrue(r.errors)
        self.assertEqual(self.adapter.requests, [])
        self.tick(minutes=6)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_restart_on_a_new_connection_keeps_the_queue(self):
        self.rolando_approves()
        self.tick()
        path = self.store.path
        self.store.close()
        self.store = SqliteLedgerStore(path)
        self.build()
        for _ in range(3):
            self.tick(minutes=6)
        self.assertEqual(len(self.view().items), 1)
        self.assertEqual(len(self.adapter.requests), 1)

    def test_secrets_never_reach_the_ledger(self):
        self.rolando_approves()
        self.tick()
        raw = self.store.path.read_bytes()
        self.assertNotIn(START_KEY.encode(), raw)


class OnboardingTests(unittest.TestCase):
    def parse(self, doc):
        import json

        return onboarding.parse(json.dumps(doc).encode(), repository=REPO, routine_id=TRIG)

    def test_valid(self):
        c = self.parse(config())
        self.assertTrue(c.intake_enabled)
        self.assertIsNotNone(c.project(PROJECT))

    def test_intake_defaults_off(self):
        doc = config()
        del doc["intake_enabled"]
        self.assertFalse(self.parse(doc).intake_enabled)

    def test_rejects(self):
        bad = {
            "wrong repo": config(entry={"repository": "rNavarrete/software-factory"}),
            "wrong routine": config(entry={"routine_id": "trig_other"}),
            "other branch": config(entry={"base_branch": "dev"}),
            "budget over cap": config(entry={"max_attempts": 4}),
            "budget zero": config(entry={"max_attempts": 0}),
            "unknown action": config(entry={"allowed_actions": ["deploy"]}),
            "no checks": config(entry={"checks": []}),
            "unknown key": config(entry={"secret": "x"}),
            "intake text": config(intake_enabled="yes"),
            "wrong format": config(format="v0"),
        }
        for name, doc in bad.items():
            with self.subTest(name), self.assertRaises(onboarding.OnboardingError):
                self.parse(doc)

    def test_duplicate_project(self):
        doc = config()
        doc["projects"].append(dict(doc["projects"][0]))
        with self.assertRaises(onboarding.OnboardingError):
            self.parse(doc)


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, value, mode=0o400):
        path = self.dir / name
        path.write_text(value)
        os.chmod(path, mode)
        return path

    def test_file_secret_read(self):
        self.write("routine-token", START_KEY + "\n")
        self.assertEqual(FileSecrets(self.dir).get("routine-token"), START_KEY)

    def test_file_readable_by_others_is_refused(self):
        self.write("routine-token", START_KEY, 0o644)
        with self.assertRaises(SecretMissing) as cm:
            FileSecrets(self.dir).get("routine-token")
        self.assertNotIn(START_KEY, str(cm.exception))

    def test_symlink_is_refused(self):
        real = self.write("elsewhere", START_KEY)
        (self.dir / "routine-token").symlink_to(real)
        with self.assertRaises(SecretMissing):
            FileSecrets(self.dir).get("routine-token")

    def test_missing_empty_and_unknown(self):
        with self.assertRaises(SecretMissing):
            FileSecrets(self.dir).get("routine-token")
        self.write("linear-key", "  ")
        with self.assertRaises(SecretMissing):
            FileSecrets(self.dir).get("linear-key")
        with self.assertRaises(SecretMissing):
            FileSecrets(self.dir).get("../etc/passwd")

    def test_env_secrets_leave_the_environment(self):
        env = {env_name("routine-token"): START_KEY, "OTHER": "x"}
        s = EnvSecrets(env)
        self.assertEqual(s.get("routine-token"), START_KEY)
        self.assertEqual(env, {"OTHER": "x"})
        self.assertNotIn(START_KEY, repr(s))

    def test_approval_key_shape(self):
        self.write("approval-key", "ab" * 32)
        self.assertEqual(len(approval_key_bytes(FileSecrets(self.dir))), 32)
        os.chmod(self.dir / "approval-key", 0o600)
        (self.dir / "approval-key").write_text("not-hex")
        with self.assertRaises(SecretMissing):
            approval_key_bytes(FileSecrets(self.dir))

    def test_install_secrets_moves_env_into_private_files(self):
        env = {env_name("github-token"): GH_TOKEN, env_name("approval-key"): "ab" * 32}
        target = self.dir / "secrets"
        names = service_main.install_secrets(target, env)
        self.assertEqual(sorted(names), ["approval-key", "github-token"])
        self.assertEqual(env, {})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        for n in names:
            self.assertEqual(stat.S_IMODE((target / n).stat().st_mode), 0o400)
        self.assertEqual(FileSecrets(target).get("github-token"), GH_TOKEN)
        # A restart installs again over the old files.
        service_main.install_secrets(target, {env_name("github-token"): GH_TOKEN})


class LockTests(unittest.TestCase):
    def test_second_service_on_the_same_home_does_not_start(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with service_main.service_lock(root):
                with self.assertRaises(service_main.AlreadyRunning):
                    with service_main.service_lock(root):
                        pass
            with service_main.service_lock(root):
                pass

    def test_lock_holds_across_processes(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d, service_main.service_lock(Path(d)):
            code = (
                "import sys; from pathlib import Path; from controller.service import main as m\n"
                "try:\n with m.service_lock(Path(sys.argv[1])): sys.exit(0)\n"
                "except m.AlreadyRunning: sys.exit(3)\n"
            )
            done = subprocess.run([sys.executable, "-c", code, d], cwd=Path(__file__).parents[1])
            self.assertEqual(done.returncode, 3)

    def test_stop_request_finishes_the_round(self):
        stop = threading.Event()
        rounds = []

        def tick():
            rounds.append(1)
            stop.set()

        n = service_main.run_forever(tick, 60, stop.is_set, sleep=lambda s: None)
        self.assertEqual(n, 1)

    def test_prune_backups_keeps_the_newest(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                (Path(d) / f"ledger-2026100{i}T000000Z.db").write_text("x")
            service_main.prune_backups(Path(d), 2)
            self.assertEqual(
                sorted(p.name for p in Path(d).iterdir()),
                ["ledger-20261003T000000Z.db", "ledger-20261004T000000Z.db"],
            )


class HttpGhTests(unittest.TestCase):
    def runner(self, answer):
        self.requests = []

        def opener(req, timeout):
            self.requests.append(req)
            if isinstance(answer, Exception):
                raise answer
            return _Resp(answer)

        return HttpGhRunner(lambda: GH_TOKEN, opener=opener)

    def test_get_with_token(self):
        run = self.runner(b"[]")
        out = run(["gh", "api", "-H", "Accept: x", f"repos/{REPO}/pulls?state=all"])
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "[]")
        req = self.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.startswith("https://api.github.com/repos/"))

    def test_refuses_other_paths(self):
        run = self.runner(b"[]")
        for path in ("user", "https://evil.example/x", f"repos/{REPO}/../../user", "orgs/x"):
            with self.subTest(path):
                self.assertNotEqual(run(["gh", "api", path]).returncode, 0)
        self.assertEqual(self.requests, [])

    def test_http_error_keeps_message_and_hides_token(self):
        body = BytesIO(f'{{"message": "No commit found for SHA: {GH_TOKEN}"}}'.encode())
        err = urllib.error.HTTPError("u", 404, "nf", {}, body)
        out = self.runner(err)(["gh", "api", f"repos/{REPO}/compare/main...x"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("No commit found", out.stderr)
        self.assertNotIn(GH_TOKEN, out.stderr)

    def test_network_failure_is_an_os_error(self):
        with self.assertRaises(OSError):
            self.runner(urllib.error.URLError("down"))(["gh", "api", f"repos/{REPO}/pulls"])


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NoSidePathTests(unittest.TestCase):
    """The service fires only through Dispatcher.dispatch."""

    def test_service_never_calls_the_fire_path_directly(self):
        root = Path(__file__).parents[1] / "controller" / "service"
        for name in ("service.py", "queue.py", "seams.py", "fixtures.py", "onboarding.py"):
            text = (root / name).read_text()
            for banned in (
                ".launch(",
                ".reserve(",
                "record_launch",
                ".fire(",
                ".approve(",
                "authorize_repair",
                "authorize_refire",
                "record_clearing",
                "urlopen",
            ):
                with self.subTest(file=name, call=banned):
                    self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
