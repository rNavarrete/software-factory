"""The live service through ``main``: Todo moves, contracts and messages from
Linear (``--live``), as the start script runs it with ``FACTORY_MODE=live``.

Linear and GitHub are fakes behind the real HTTP clients (``linear_opener``
and ``github_opener``), so every request goes through the same code as on
the host and nothing touches the network. A real signer answers on a
socket, but it has no Linear key, so no Todo move can be approved and no
worker can start.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from controller.approval import StaticKey
from controller.attempts import events as ev
from controller.dispatch.dispatch import FACTORY_ROUTINE
from controller.intake.linear import LinearUnavailable
from controller.ledger import SqliteLedgerStore
from controller.loop.collect import GhApi, NotFound
from controller.recovery import PILOT_REPO
from controller.report.linear_api import LinearDown, LinearRefused
from controller.service import main as service_main
from controller.service import onboarding
from controller.service import queue as q
from controller.service.github_http import HttpGhRunner, as_bytes
from controller.service.secrets import env_name
from controller.signer import SignerServer
from tests import linear_world
from tests.linear_world import Resp, http_error
from tests.test_intake_linear import _QUERY_NAMES, APPROVER, FACTORY_USER, PROJECT, TODO
from tests.test_intake_linear import FakeLinear as IntakeLinear
from tests.test_prepare import GitHub
from tests.test_service import GH_TOKEN

ROOT = Path(__file__).resolve().parents[1]
LINEAR_KEY = "lin_api_" + "LiveServiceKey00" * 3
DESCRIPTION = (
    "## Outcome\nSort the list.\n\n## Acceptance criteria\n"
    "- [ ] sortBooks(books) returns the books ordered by title.\n"
    "- [ ] The page lists books in title order.\n"
)


class Later(datetime):
    """The service's clock ``ahead`` on."""

    ahead = timedelta(minutes=10)

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + cls.ahead


class LinearRouter:
    """One fake Linear for every client: intake's queries go to the intake
    workspace (tickets and their history), the reporter's to the comments
    workspace. Errors come back the way Linear sends them."""

    def __init__(self):
        self.tickets = IntakeLinear()
        self.comments = linear_world.FakeLinear(viewer_id=FACTORY_USER)
        self.requests = []
        self.down = False

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        query, variables = body["query"], body.get("variables") or {}
        self.requests.append(query)
        if self.down:
            raise http_error(503)
        try:
            if query in _QUERY_NAMES:
                data = self.tickets(query, variables)
            elif "PrepareIssue" in query:
                node = self.tickets.find(variables["id"])
                node = json.loads(json.dumps(node)) if node else None
                if node is not None:
                    node["attachments"] = {"nodes": [], "pageInfo": {"hasNextPage": False}}
                data = {"issue": node}
            else:
                data = self.comments(query, variables)
        except LinearDown:
            raise http_error(503) from None
        except (LinearRefused, LinearUnavailable) as e:
            return Resp(json.dumps({"errors": [{"message": str(e)}]}).encode())
        return Resp(json.dumps({"data": data}).encode())

    def bodies(self):
        return [c.body for c in self.comments.comments.values()]


class GitHubOpener:
    """The preparer's GitHub reads, answered by tests/test_prepare.py's table."""

    def __init__(self):
        self.github = GitHub()
        self.paths = []

    def __call__(self, req, timeout=None):
        path = req.full_url.removeprefix("https://api.github.com/")
        self.paths.append(path)
        try:
            answer = self.github.json(path)
        except NotFound:
            raise http_error(404, {"message": "Not Found"}) from None
        return Resp(json.dumps(answer).encode())


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED is Linux only")
class LiveCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.home = self.dir / "home"
        self.secrets = self.dir / "secrets"
        env = {
            "HOME": str(self.home),
            "FACTORY_SECRETS_DIR": str(self.secrets),
            "FACTORY_SIGNER_SOCKET": str(self.dir / "signer.sock"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The service's own copy, as the start script makes it: no approval key.
        self.install(
            {
                env_name("linear-key"): LINEAR_KEY,
                env_name("github-token"): GH_TOKEN,
                env_name("routine-token"): "sk-ant-oat01-" + "LiveRoutineKey00" * 3,
            }
        )
        self.start_signer()
        self.linear = LinearRouter()
        self.gh = GitHubOpener()
        for name, opener in (("linear_opener", self.linear), ("github_opener", self.gh)):
            p = mock.patch.object(service_main, name, return_value=opener)
            p.start()
            self.addCleanup(p.stop)
        self.now = datetime.now(UTC)
        self.write_onboarding(intake_enabled=False)
        doc = json.loads((ROOT / "deploy" / "pilot" / "drafting.json").read_text())
        doc["projects"][0]["linear_project_id"] = PROJECT
        self.drafting = self.dir / "drafting.json"
        self.drafting.write_text(json.dumps(doc))

    def start_signer(self):
        """The signer as the start script runs it, without Todo-move approval."""
        server = SignerServer(
            StaticKey(b"k" * 32), self.dir / "signer.sock", allowed_uids={os.getuid()}
        )
        server.listen()
        stop = threading.Event()
        thread = threading.Thread(target=server.serve_forever, args=(stop.is_set,), daemon=True)
        thread.start()

        def shutdown():
            stop.set()
            thread.join(5)
            server.close()

        self.addCleanup(shutdown)

    def install(self, values):
        for f in self.secrets.glob("*") if self.secrets.exists() else ():
            f.unlink()
        service_main.install_secrets(self.secrets, dict(values))

    def write_onboarding(self, **changes):
        doc = json.loads((ROOT / "deploy" / "pilot" / "onboarding.json").read_text())
        doc["approver_linear_user_id"] = APPROVER
        doc["intake_since"] = (self.now - timedelta(hours=3)).isoformat()
        doc["projects"][0]["linear_project_id"] = PROJECT
        doc["projects"][0]["issues"] = ["ENG-187"]
        doc.update(changes)
        self.onboarding = self.dir / "onboarding.json"
        self.onboarding.write_text(json.dumps(doc))

    def once(self, *extra):
        args = [
            "once",
            "--live",
            "--onboarding",
            str(self.onboarding),
            "--drafting",
            str(self.drafting),
            "--approver-linear-id",
            APPROVER,
            *extra,
        ]
        return service_main.main(args)

    def rolando_moves(self, key="ENG-187"):
        self.linear.tickets.add(key, created=self.now - timedelta(hours=1), description=DESCRIPTION)
        self.linear.tickets.move(key, TODO, self.now - timedelta(minutes=5))

    def events(self):
        store = SqliteLedgerStore(self.home / ".software-factory" / "ledger.db")
        try:
            return [s.event for s in store.events()]
        finally:
            store.close()

    def view(self):
        store = SqliteLedgerStore(self.home / ".software-factory" / "ledger.db")
        try:
            return q.ServiceView.build(store.events())
        finally:
            store.close()


class LiveIntakeOffTests(LiveCase):
    def test_runs_without_a_fixture_folder_and_reads_linear(self):
        self.assertEqual(self.once(), 0)
        self.assertTrue(any("FactoryViewer" in r for r in self.linear.requests))
        self.assertEqual(self.linear.bodies(), [])
        self.assertEqual(self.gh.paths, [])

    def test_move_is_refused_once_and_nothing_is_drafted_or_fired(self):
        self.rolando_moves()
        self.once()
        self.once()
        bodies = self.linear.bodies()
        self.assertEqual(len(bodies), 1, bodies)
        self.assertIn("intake is switched off", bodies[0])
        self.assertEqual(self.gh.paths, [])  # no contract was drafted
        kinds = {e.kind for e in self.events()}
        self.assertNotIn(ev.FIRE_INTENT, kinds)
        self.assertEqual(list(self.view().open_items()), [])

    def test_restart_with_a_message_queued_posts_it_once(self):
        self.rolando_moves()
        self.linear.comments.down = LinearDown("Linear answered HTTP 503")
        self.once()
        self.assertEqual(self.linear.bodies(), [])
        self.assertEqual(len(self.view().outbox), 1)
        self.linear.comments.down = None
        with mock.patch("controller.service.service.datetime", Later):  # past the retry wait
            for _ in range(3):  # three separate service processes
                self.once()
        self.assertEqual(len(self.linear.bodies()), 1)
        self.assertEqual(len(self.view().outbox), 0)

    def test_key_acting_as_rolando_reads_and_posts_nothing(self):
        self.rolando_moves()
        self.linear.tickets.viewer["id"] = APPROVER
        self.linear.comments.viewer_id = APPROVER
        self.once()
        self.once()
        self.assertEqual(self.linear.bodies(), [])
        self.assertEqual(list(self.view().items), [])
        self.assertTrue(all("FactoryViewer" in r for r in self.linear.requests))

    def test_approver_changed_while_running_stops_posting_as_that_user(self):
        """The onboarding file is read every round. If it comes to name the
        factory's own Linear user as approver, intake stops and queued
        messages are not posted with that user's key either."""
        self.rolando_moves()
        self.linear.comments.down = LinearDown("Linear answered HTTP 503")

        def two_rounds(tick, interval, stop):
            tick()  # the refusal is queued; Linear is down for comments
            self.write_onboarding(approver_linear_user_id=FACTORY_USER)
            self.linear.comments.down = None
            Later.ahead = timedelta(minutes=30)  # past the retry wait
            try:
                tick()
            finally:
                Later.ahead = timedelta(minutes=10)
            return 2

        with (
            mock.patch.object(service_main, "run_forever", two_rounds),
            mock.patch("controller.service.service.datetime", Later),
        ):
            service_main.main(
                [
                    "run",
                    "--live",
                    "--onboarding",
                    str(self.onboarding),
                    "--drafting",
                    str(self.drafting),
                    "--approver-linear-id",
                    APPROVER,
                ]
            )
        self.assertEqual(self.linear.bodies(), [])
        self.assertEqual(len(self.view().outbox), 1)

    def test_linear_down_records_nothing_and_catches_up(self):
        self.rolando_moves()
        self.linear.down = True
        self.assertEqual(self.once(), 1)
        self.assertEqual(self.linear.bodies(), [])
        self.assertEqual(list(self.view().items), [])
        self.linear.down = False
        self.once()
        self.assertEqual(len(self.linear.bodies()), 1)


class LiveIntakeOnTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.write_onboarding(intake_enabled=True)

    def test_move_is_drafted_from_linear_and_waits_for_the_signer(self):
        self.rolando_moves()
        self.once()
        self.once()
        item = next(iter(self.view().items.values()))
        self.assertIsNotNone(item.digest, "a contract was drafted from the ticket")
        self.assertIsNone(item.closed)
        self.assertTrue(any("/actions/workflows/ci.yml/runs" in p for p in self.gh.paths))
        self.assertTrue(any("PrepareIssue" in r for r in self.linear.requests))
        bodies = self.linear.bodies()
        self.assertEqual(sum("The factory prepared this task" in b for b in bodies), 1, bodies)
        # No signer, so no approval: nothing fired.
        self.assertNotIn(ev.FIRE_INTENT, {e.kind for e in self.events()})

    def test_restart_with_a_drafted_item_queued_drafts_nothing_twice(self):
        self.rolando_moves()
        self.once()
        digest = next(iter(self.view().items.values())).digest
        reads = len(self.gh.paths)
        for _ in range(2):
            self.once()
        item = next(iter(self.view().items.values()))
        self.assertEqual(item.digest, digest)
        self.assertEqual(len(self.gh.paths), reads)  # the base isn't chosen again
        contracts = list((self.home / ".software-factory" / "contracts").glob("*.json"))
        self.assertEqual(len(contracts), 1)


class NotStartedTests(LiveCase):
    def assertNotStarted(self, *extra):
        self.assertEqual(self.once(*extra), 4)
        self.assertEqual(self.linear.requests, [])
        self.assertEqual(self.gh.paths, [])

    def test_missing_linear_key(self):
        self.install({env_name("github-token"): GH_TOKEN})
        self.assertNotStarted()

    def test_missing_github_token(self):
        self.install({env_name("linear-key"): LINEAR_KEY})
        self.assertNotStarted()

    def test_no_signer(self):
        with mock.patch.dict(os.environ, {"FACTORY_SIGNER_SOCKET": ""}):
            self.assertNotStarted()

    def test_signer_socket_set_but_nothing_answers(self):
        with mock.patch.dict(os.environ, {"FACTORY_SIGNER_SOCKET": str(self.dir / "gone.sock")}):
            self.assertNotStarted()

    def test_onboarding_without_intake_since(self):
        doc = json.loads(self.onboarding.read_text())
        del doc["intake_since"]
        self.onboarding.write_text(json.dumps(doc))
        self.assertNotStarted()

    def test_onboarding_without_an_approver(self):
        doc = json.loads(self.onboarding.read_text())
        del doc["approver_linear_user_id"]
        self.onboarding.write_text(json.dumps(doc))
        self.assertNotStarted()

    def test_drafting_policy_without_the_onboarded_project(self):
        doc = json.loads(self.drafting.read_text())
        doc["projects"][0]["linear_project_id"] = "another-project"
        self.drafting.write_text(json.dumps(doc))
        self.assertNotStarted()

    def test_drafting_policy_on_another_base_branch(self):
        doc = json.loads(self.drafting.read_text())
        doc["projects"][0]["base"]["branch"] = "release"
        self.drafting.write_text(json.dumps(doc))
        self.assertNotStarted()

    def test_unreadable_drafting_policy(self):
        self.drafting.write_text("{")
        self.assertNotStarted()

    def test_approver_differs_from_the_onboarding_file(self):
        self.assertNotStarted("--approver-linear-id", "someone-else")

    def test_missing_onboarding_file(self):
        self.onboarding.unlink()
        self.assertNotStarted()

    def test_fixture_source_without_a_fixture_folder(self):
        args = ["once", "--source", "fixtures", "--onboarding", str(self.onboarding)]
        self.assertEqual(service_main.main(args), 4)

    def test_nothing_is_written_to_the_ledger(self):
        self.install({})
        self.once()
        self.assertFalse((self.home / ".software-factory" / "ledger.db").exists())


class PackagedImageTests(unittest.TestCase):
    """The service from the files the Dockerfile copies, and nothing else."""

    def test_every_module_imports_from_the_image_layout(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "app"
            app.mkdir()
            for line in (ROOT / "deploy" / "fly" / "Dockerfile").read_text().splitlines():
                parts = line.split()
                if not parts or parts[0] != "COPY":
                    continue
                src, dest = [x for x in parts[1:] if not x.startswith("--")]
                target = Path(d) / dest.lstrip("/")
                if (ROOT / src).is_dir():
                    shutil.copytree(
                        ROOT / src, target, ignore=shutil.ignore_patterns("__pycache__")
                    )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(ROOT / src, target)
            script = (
                "import importlib, pkgutil\n"
                "n = 0\n"
                "for top in ('controller', 'verify'):\n"
                "    pkg = importlib.import_module(top)\n"
                "    for m in pkgutil.walk_packages(pkg.__path__, top + '.'):\n"
                "        if not m.name.endswith('__main__'):\n"
                "            importlib.import_module(m.name)\n"
                "        n += 1\n"
                "from controller.service import main\n"
                "assert main.DRAFTING_POLICY.is_file(), main.DRAFTING_POLICY\n"
                "print(n)\n"
            )
            out = subprocess.run(
                [sys.executable, "-s", "-E", "-c", script],
                cwd=app,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertGreater(int(out.stdout.strip()), 50)


class ShippedFilesTests(unittest.TestCase):
    """What the start script runs with FACTORY_MODE=live."""

    def test_pilot_onboarding_turns_intake_on_only_from_a_fixed_time(self):
        # Switched on for the go-live run (docs/go-live.md); moves before
        # intake_since are never read.
        config = onboarding.load(
            ROOT / "deploy" / "pilot" / "onboarding.json",
            repository=PILOT_REPO,
            routine_id=FACTORY_ROUTINE,
        )
        self.assertTrue(config.intake_enabled)
        self.assertIsNotNone(config.intake_since)
        self.assertEqual(config.approver_linear_user_id, service_main.ROLANDO_LINEAR_ID)

    def test_pilot_drafting_policy_covers_every_onboarded_project(self):
        from controller.prepare import policy as drafting

        config = onboarding.load(
            ROOT / "deploy" / "pilot" / "onboarding.json",
            repository=PILOT_REPO,
            routine_id=FACTORY_ROUTINE,
        )
        policies = drafting.load(service_main.DRAFTING_POLICY)
        for pid, project in config.projects.items():
            with self.subTest(project=pid):
                self.assertIsNotNone(policies.project(pid))
                self.assertEqual(policies.project(pid).base.branch, project.base_branch)

    def test_start_script_defaults_to_the_qualification_fixture(self):
        text = (ROOT / "deploy" / "fly" / "entrypoint.sh").read_text()
        self.assertIn('case "${FACTORY_MODE:-qualification}" in', text)
        qualification = text.split("qualification)", 1)[1].split(";;", 1)[0]
        live = text.split("live)", 1)[1].split(";;", 1)[0]
        self.assertIn("--fixtures /app/deploy/qualification", qualification)
        self.assertNotIn("--real-runtime", qualification)
        self.assertIn("HOME=/data/qualification", qualification)
        self.assertIn("HOME=/data/factory", live)
        self.assertIn("/app/deploy/pilot/onboarding.json", live)
        self.assertIn("--live", live)
        self.assertNotIn("FACTORY_MODE=", (ROOT / "deploy" / "fly" / "fly.toml").read_text())

    def test_commands_over_ssh_use_the_running_services_home(self):
        text = (ROOT / "deploy" / "fly" / "factory").read_text()
        self.assertIn("/run/factory-home", text)
        self.assertIn(">/run/factory-home", (ROOT / "deploy" / "fly" / "entrypoint.sh").read_text())


class SignerStartTests(unittest.TestCase):
    """The signer's Todo-move request, as it is built once a Linear key exists."""

    def test_todo_move_handler_builds(self):
        import argparse

        from controller.approval import StaticKey

        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                onboarding=str(ROOT / "deploy" / "pilot" / "onboarding.json"),
                state=str(Path(d) / "moves.json"),
            )
            handler = service_main._todo_move_handler(args, StaticKey(b"k" * 32), LINEAR_KEY)
        self.assertTrue(callable(handler))


class GitHubForThePreparerTests(unittest.TestCase):
    """``GhApi`` over the service's HTTP client, as the live preparer uses it."""

    def test_reads_json_and_reports_not_found(self):
        gh = GitHubOpener()
        api = GhApi(run=as_bytes(HttpGhRunner(lambda: GH_TOKEN, gh)))
        commits = api.json(f"repos/{PILOT_REPO}/commits?sha=main&per_page=20")
        self.assertEqual(commits, [{"sha": "a" * 40}])
        with self.assertRaises(NotFound):
            api.json(f"repos/{PILOT_REPO}/nothing-here")

    def test_token_never_appears_in_errors(self):
        def opener(req, timeout=None):
            raise http_error(401, {"message": f"bad credentials {GH_TOKEN}"})

        api = GhApi(run=as_bytes(HttpGhRunner(lambda: GH_TOKEN, opener)))
        with self.assertRaises(Exception) as e:
            api.json(f"repos/{PILOT_REPO}/commits?sha=main")
        self.assertNotIn(GH_TOKEN, str(e.exception))


if __name__ == "__main__":
    unittest.main()
