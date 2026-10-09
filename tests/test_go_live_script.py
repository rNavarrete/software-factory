"""The go-live command (deploy/fly/go-live.sh), run against stand-ins.

``fly``, ``gh``, ``curl``, ``security`` and ``pbcopy`` are small fakes on
PATH that record every call and keep their state in one JSON file. The
service machine is a scripted sequence of "worlds": each look at the
factory's status moves one step on, as the factory would while Rolando acts
in Linear. Nothing touches the network, Fly, GitHub, Linear or Keychain.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fly" / "go-live.sh"
ROLANDO = "cd9ec650-f957-4f25-b5f0-9c14bcae49c8"
FACTORY_USER = "f0f0f0f0-0000-4000-8000-000000000001"
LINEAR_KEY = "lin_api_" + "GoLiveTestKey000" * 3
ROUTINE_KEY = "sk-ant-oat01-" + "GoLiveRoutine000" * 3

FAKE_FLY = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_STATE"]
st = json.load(open(state_path))
args = sys.argv[1:]
st["calls"].append(["fly"] + args)
st.setdefault("stdin", [])
def save():
    json.dump(st, open(state_path, "w"))
def world():
    return st["worlds"][min(st["step"], len(st["worlds"]) - 1)]
out = ""
code = 0
if args[:2] == ["auth", "whoami"]:
    out = "rolando@example.com\n"
elif args[:2] == ["secrets", "list"]:
    out = "NAME\tDIGEST\tSTATUS\n" + "".join(f"{n}\tabc\tDeployed\n" for n in st["secrets"])
    out += "".join(f"{n}\tabc\tStaged\n" for n in st["staged"])
elif args[:2] == ["secrets", "import"]:
    data = sys.stdin.read()
    st["stdin"].append(data)
    for line in data.splitlines():
        name, _, value = line.partition("=")
        st["staged"][name] = value
elif args[0] == "deploy":
    st["deploy_dirs"].append(args[1])
    st["deployed_files"] = sorted(
        os.path.relpath(os.path.join(d, f), args[1])
        for d, _, fs in os.walk(args[1]) for f in fs
    )
    if st.get("deploy_fails_at") == st["deploys"] + 1:
        st["deploy_fails_at"] = None
        save()
        sys.exit(1)
    st["secrets"].update(st["staged"])
    st["staged"] = {}
    st["deploys"] += 1
    st["mode"] = st["secrets"].get("FACTORY_MODE", "qualification")
elif args[:2] == ["apps", "restart"]:
    st["restarts"] += 1
    if st.get("fire_on_restart"):
        st["extra_fires"].append(["eng-200-a2-f1", "launched https://claude.ai/code/s2"])
elif args[:2] == ["ssh", "console"]:
    cmd = args[args.index("-C") + 1]
    if any(op in cmd for op in ("|", "&", ";", ">", "<")) and not cmd.startswith("sh -c "):
        # Fly runs the command without a shell: operators reach the program.
        out, code = "cat: '||': No such file or directory\n", 1
    elif cmd == "cat /run/factory-home":
        out = "/data/factory\n" if st["mode"] == "live" else "/data/qualification\n"
    elif cmd.startswith("sh -c 'cat /data/go-live/"):
        name = cmd.split("/data/go-live/")[1].split()[0]
        out = st["records"].get(name, "") + ("\n" if name in st["records"] else "")
    elif cmd.startswith("sh -c 'mkdir -p /data/go-live && echo "):
        value, name = cmd.split("echo ")[1].split(" > /data/go-live/")
        st["records"][name.rstrip("'")] = value
    elif "controller.intake probe" in cmd:
        out = st["intake_probe"]
    elif "controller.report probe" in cmd:
        out = st["report_probe"]
        st["report_probes"] += 1
    elif cmd == "/app/factory status":
        w = world()
        fires = w["fires"] + st["extra_fires"]
        lines = ["No tasks on record."] if not fires else ["eng-200: running (release: none)"]
        lines += w.get("extra", [])
        reading = "2026-10-09 00:00 UTC, session 5%, weekly 20%" if st["snapshot"] else ""
        lines.append(f"Usage reading: {reading or 'none recorded'}")
        lines += [f"Fire {run}: {answer}" for run, answer in fires]
        lines.append(f"Fires on record: {len(fires)}")
        out = "\n".join(lines) + "\n"
    elif cmd == "/app/factory queue":
        w = world()
        st["step"] += 1
        out = "cursor: x\n" + "".join(f"{k:10} {k.lower()}  {v}\n" for k, v in w["items"].items())
    elif cmd.startswith("/app/factory snapshot "):
        st["snapshot"] = cmd.split()[2:]
    else:
        out = "unknown command\n"
        code = 1
else:
    code = 2
save()
sys.stdout.write(out)
sys.exit(code)
"""

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
st["calls"].append(["gh"] + sys.argv[1:])
json.dump(st, open(os.environ["FAKE_STATE"], "w"))
if sys.argv[1:3] == ["auth", "status"]:
    sys.exit(0)
print(st["ci"])
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
st["calls"].append(["curl"] + sys.argv[1:])
header = sys.stdin.read()
st["curl_stdin"] = header
json.dump(st, open(os.environ["FAKE_STATE"], "w"))
print(json.dumps({"data": {"viewer": {"id": st["viewer"], "name": "Factory"}}}))
"""

FAKE_SECURITY = r"""#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
st["calls"].append(["security"] + sys.argv[1:])
json.dump(st, open(os.environ["FAKE_STATE"], "w"))
if st.get("keychain"):
    print(st["keychain"])
    sys.exit(0)
sys.exit(44)
"""

FAKE_PBCOPY = r"""#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
st["clipboard"] = sys.stdin.read()
json.dump(st, open(os.environ["FAKE_STATE"], "w"))
"""

GOOD_INTAKE_PROBE = (
    "the factory's Linear key acts as: Factory (f0f0)\n"
    "ENG-201: now in Backlog\n"
    "  2026-10-08 22:50:27 - -> Backlog                  actor='Rolando'"
    " bot='oauthClient: Claude' automation=False import=False:"
    " made through an integration (oauthClient: Claude), not by Rolando himself\n"
)
GOOD_REPORT_PROBE = "OK: posted once under the factory's own user; retries showed nothing twice;\n"

# The factory as Rolando acts: ENG-200 is moved, asked a question, answered,
# moved again and started; then ENG-202 is moved, queued and withdrawn.
LAUNCHED = [["eng-200-a1-f1", "launched https://claude.ai/code/session_1"]]
_PHASES = [
    {"fires": [], "items": {}},
    {"fires": [], "items": {"ENG-200": "question"}},
    {"fires": LAUNCHED, "items": {"ENG-200": "open"}},
    {"fires": LAUNCHED, "items": {"ENG-200": "open", "ENG-202": "open"}},
    {"fires": LAUNCHED, "items": {"ENG-200": "open", "ENG-202": "authorization-withdrawn"}},
]
HAPPY = [w for w in _PHASES for _ in range(3)]
"""Each look at the queue moves one world on; each phase lasts three looks."""


def write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class GoLiveScriptCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, text in {
            "fly": FAKE_FLY,
            "gh": FAKE_GH,
            "curl": FAKE_CURL,
            "security": FAKE_SECURITY,
            "pbcopy": FAKE_PBCOPY,
        }.items():
            write(self.bin / name, text)
        self.state = self.dir / "state.json"
        self.set_state(
            calls=[],
            secrets={
                "FACTORY_APPROVAL_KEY": "x",
                "FACTORY_GITHUB_TOKEN": "x",
                "FACTORY_REVIEW_TOKEN": "x",
                "FACTORY_REVIEW_MODEL": "m",
                "FACTORY_REVIEW_DISPATCHER": "rNavarrete",
            },
            staged={},
            deploys=0,
            restarts=0,
            report_probes=0,
            mode="qualification",
            records={},
            snapshot=None,
            step=0,
            worlds=HAPPY,
            extra_fires=[],
            deploy_dirs=[],
            deployed_files=[],
            intake_probe=GOOD_INTAKE_PROBE,
            report_probe=GOOD_REPORT_PROBE,
            ci="success",
            viewer=FACTORY_USER,
            keychain=ROUTINE_KEY,
        )
        self.repo = self.make_repo()

    def set_state(self, **values):
        st = json.loads(self.state.read_text()) if self.state.exists() else {}
        st.update(values)
        self.state.write_text(json.dumps(st))

    def st(self):
        return json.loads(self.state.read_text())

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            check=True,
            capture_output=True,
            env=self.git_env(),
        )

    def git_env(self):
        return dict(
            os.environ,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.com",
        )

    def make_repo(self):
        origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        repo = self.dir / "software-factory"
        repo.mkdir()
        shutil.copytree(ROOT / "deploy", repo / "deploy")
        (repo / "controller" / "adapter").mkdir(parents=True)
        shutil.copy(
            ROOT / "controller" / "adapter" / "routine_prompt.md",
            repo / "controller" / "adapter" / "routine_prompt.md",
        )
        self.git("init", "-q", "-b", "main", cwd=repo)
        self.git("add", ".", cwd=repo)
        self.git("commit", "-q", "-m", "main", cwd=repo)
        self.git("remote", "add", "origin", str(origin), cwd=repo)
        self.git("push", "-q", "origin", "main", cwd=repo)
        return repo

    def run_script(self, answers=(), shell="sh"):
        tty = self.dir / "tty"
        tty.write_text("".join(f"{a}\n" for a in answers))
        env = dict(
            self.git_env(),
            PATH=f"{self.bin}:{os.environ['PATH']}",
            HOME=str(self.dir / "home"),
            FAKE_STATE=str(self.state),
            FACTORY_TTY=str(tty),
            POLL="0",
            RESTART_WAIT="0",
        )
        return subprocess.run(
            [shell, str(SCRIPT)],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def fly_calls(self, *prefix):
        return [c for c in self.st()["calls"] if c[: len(prefix) + 1] == ["fly", *prefix]]

    def all_args(self):
        return "\n".join(" ".join(c) for c in self.st()["calls"])


FIRST_RUN = [LINEAR_KEY, "", "5", "20", "0"]
"""The Linear key, Enter once the routine is saved, then the usage reading."""


class HappyPathTests(GoLiveScriptCase):
    def test_first_run_goes_live_and_walks_the_first_ticket(self):
        out = self.run_script(FIRST_RUN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        st = self.st()
        self.assertEqual(st["secrets"]["FACTORY_LINEAR_KEY"], LINEAR_KEY)
        self.assertEqual(st["secrets"]["FACTORY_ROUTINE_TOKEN"], ROUTINE_KEY)
        self.assertEqual(st["secrets"]["FACTORY_MODE"], "live")
        self.assertEqual(st["mode"], "live")
        # One deploy to check the keys in practice mode, one to go live.
        self.assertEqual(st["deploys"], 2)
        self.assertEqual(st["report_probes"], 1)
        self.assertEqual(st["restarts"], 1)
        self.assertEqual(st["snapshot"], ["5", "20", "0"])
        self.assertEqual(set(st["records"]), {"routine-prompt", "restart-checked", "edit-checked"})
        # The routine text on the clipboard is exactly what is below the line.
        prompt = (ROOT / "controller" / "adapter" / "routine_prompt.md").read_text()
        self.assertEqual(st["clipboard"], prompt.split("\n---\n", 1)[1])
        self.assertIn("move ENG-200", out.stdout)
        self.assertEqual(out.stdout.count("The factory asked a question on ENG-200"), 1)
        self.assertIn("add one word anywhere in ENG-202", out.stdout)
        self.assertIn("Done. The factory is live", out.stdout)

    def test_secrets_never_reach_a_command_line(self):
        out = self.run_script(FIRST_RUN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        args = self.all_args()
        self.assertNotIn(LINEAR_KEY, args)
        self.assertNotIn(ROUTINE_KEY, args)
        self.assertNotIn(LINEAR_KEY, out.stdout + out.stderr)
        self.assertNotIn(ROUTINE_KEY, out.stdout + out.stderr)
        # The Linear key went to Linear on standard input only.
        self.assertEqual(self.st()["curl_stdin"], f"Authorization: {LINEAR_KEY}\n")

    def test_works_in_the_macs_shell_too(self):
        out = self.run_script(FIRST_RUN, shell="bash")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_running_it_again_does_nothing_twice(self):
        self.assertEqual(self.run_script(FIRST_RUN).returncode, 0)
        before = self.st()
        out = self.run_script(())  # no answers: it must not ask for anything
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        after = self.st()
        self.assertEqual(after["deploys"], before["deploys"])
        self.assertEqual(after["restarts"], before["restarts"])
        self.assertEqual(after["report_probes"], before["report_probes"])
        self.assertEqual(len(after["stdin"]), len(before["stdin"]))
        self.assertIn("Already set.", out.stdout)

    def test_keychain_missing_asks_for_the_start_key_hidden(self):
        self.set_state(keychain="")
        out = self.run_script([LINEAR_KEY, ROUTINE_KEY, "", "5", "20", "0"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.st()["secrets"]["FACTORY_ROUTINE_TOKEN"], ROUTINE_KEY)

    def test_bad_usage_numbers_are_asked_again(self):
        out = self.run_script([LINEAR_KEY, "", "five", "20", "0", "5", "20", "0"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Please type plain numbers", out.stdout)
        self.assertEqual(self.st()["snapshot"], ["5", "20", "0"])


class RefusalTests(GoLiveScriptCase):
    def assertStoppedBeforeLive(self, out, words):
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(words, out.stderr)
        self.assertNotIn("FACTORY_MODE", self.st()["secrets"])

    def test_rolandos_own_linear_key_is_refused_and_not_stored(self):
        self.set_state(viewer=ROLANDO)
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "That key is yours")
        st = self.st()
        self.assertNotIn("FACTORY_LINEAR_KEY", st["secrets"])
        self.assertNotIn("FACTORY_LINEAR_KEY", st["staged"])

    def test_something_that_is_not_a_linear_key_is_refused(self):
        out = self.run_script(["hello", "", "5", "20", "0"])
        self.assertStoppedBeforeLive(out, "isn't a Linear API key")

    def test_not_on_main(self):
        self.git("checkout", "-q", "-b", "other")
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "Switch to main")
        self.assertEqual(self.fly_calls(), [])

    def test_behind_origin(self):
        clone = self.dir / "other"
        self.git("clone", "-q", str(self.dir / "origin.git"), str(clone), cwd=self.dir)
        (clone / "new.txt").write_text("x")
        self.git("add", ".", cwd=clone)
        self.git("commit", "-q", "-m", "newer", cwd=clone)
        self.git("push", "-q", "origin", "main", cwd=clone)
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "git pull")
        self.assertEqual(self.fly_calls(), [])

    def test_local_changes_are_not_deployed(self):
        (self.repo / "deploy" / "fly" / "entrypoint.sh").write_text("# changed\n")
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "local changes")

    def test_before_the_go_live_change_is_merged(self):
        path = self.repo / "deploy" / "pilot" / "onboarding.json"
        path.write_text(
            path.read_text().replace('"intake_enabled": true', '"intake_enabled": false')
        )
        self.git("commit", "-q", "-am", "intake off")
        self.git("push", "-q", "origin", "main")
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "isn't on main yet")

    def test_red_ci_on_main(self):
        self.set_state(ci="failure")
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "CI on main isn't green")
        self.assertEqual(self.fly_calls("deploy"), [])

    def test_failed_posting_check_stays_in_practice_mode_then_resumes(self):
        self.set_state(report_probe="FAIL: expected one comment by the factory's user, found 2\n")
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "posting check failed")
        self.assertEqual(self.st()["mode"], "qualification")
        # Fixed; running again asks for nothing already given and goes live.
        self.set_state(report_probe=GOOD_REPORT_PROBE)
        out = self.run_script(["5", "20", "0"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.st()["mode"], "live")

    def test_connector_change_counted_as_rolandos_is_the_accepted_limit(self):
        # Seen live: the connector signs in as Rolando and Linear marks no app.
        # He chose to go live with the rule that Claude never moves pilot tickets.
        self.set_state(
            intake_probe=GOOD_INTAKE_PROBE.replace(
                "made through an integration (oauthClient: Claude), not by Rolando himself",
                "counts as Rolando's own action",
            )
        )
        out = self.run_script(FIRST_RUN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(
            "Known limit: Linear records the Linear connector's changes as yours.", out.stdout
        )
        self.assertEqual(self.st()["mode"], "live")

    def test_an_empty_history_still_stops(self):
        self.set_state(intake_probe=GOOD_INTAKE_PROBE.split("  2026")[0])
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "Couldn't read ENG-201's history")

    def test_linear_key_acting_as_rolando_on_the_host_stops(self):
        self.set_state(intake_probe="  PROBLEM: that is Rolando.\n" + GOOD_INTAKE_PROBE)
        out = self.run_script(FIRST_RUN)
        self.assertStoppedBeforeLive(out, "acts as you")

    def test_a_second_worker_after_the_restart_is_an_alarm(self):
        self.set_state(fire_on_restart=True)
        out = self.run_script(FIRST_RUN)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Another worker started after the restart", out.stderr)
        self.assertNotIn("restart-checked", self.st()["records"])

    def test_a_worker_for_the_edited_ticket_is_an_alarm(self):
        fired = LAUNCHED + [["eng-202-a1-f1", "launched https://claude.ai/code/session_2"]]
        worlds = HAPPY[:-3] + [{"fires": fired, "items": {"ENG-200": "open", "ENG-202": "open"}}]
        self.set_state(worlds=worlds)
        out = self.run_script(FIRST_RUN)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("The factory fired a worker for ENG-202", out.stderr)

    def test_the_first_ticket_refused_says_so(self):
        self.set_state(
            worlds=[
                {"fires": [], "items": {}},
                {"fires": [], "items": {"ENG-200": "authorization-refused"}},
            ]
        )
        out = self.run_script(FIRST_RUN)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("The factory stopped ENG-200", out.stderr)

    def test_a_hold_stops_before_any_todo_move(self):
        worlds = [dict(w, extra=["Hold: paused"]) for w in HAPPY]
        self.set_state(worlds=worlds)
        out = self.run_script(FIRST_RUN)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Something is holding the factory", out.stderr)
        self.assertNotIn("move ENG-200", out.stdout)


class ReviewFindingTests(GoLiveScriptCase):
    """Cases from Rolando's review of the first version."""

    def test_no_live_switch_without_the_codex_review(self):
        for name in ("FACTORY_REVIEW_DISPATCHER", "FACTORY_REVIEW_MODEL", "FACTORY_REVIEW_TOKEN"):
            with self.subTest(missing=name):
                secrets = dict(self.st()["secrets"])
                value = secrets.pop(name)
                self.set_state(secrets=secrets, calls=[])
                out = self.run_script(FIRST_RUN)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("setup-reviewer.sh", out.stderr)
                self.assertEqual(self.fly_calls("deploy"), [])
                self.assertEqual(self.st()["staged"], {})
                secrets[name] = value
                self.set_state(secrets=secrets)

    def check_launch_answer(self, answer):
        fire = [["eng-200-a1-f1", answer]]
        worlds = [{"fires": [], "items": {}}] * 3 + [
            {"fires": fire, "items": {"ENG-200": "open"}}
        ] * 3
        if answer == "no answer yet":
            worlds += HAPPY[6:]
        self.set_state(worlds=worlds)
        return self.run_script(FIRST_RUN)

    def test_a_refused_launch_is_not_a_started_worker(self):
        for answer in ("not-launched", "launch-outcome-unknown"):
            with self.subTest(answer=answer):
                self.set_state(step=0, records={}, restarts=0)
                out = self.check_launch_answer(answer)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("didn't launch", out.stderr)
                self.assertNotIn("A worker started", out.stdout)
                self.assertEqual(self.st()["restarts"], 0)

    def test_a_launch_still_waiting_for_its_answer_is_waited_for(self):
        out = self.check_launch_answer("no answer yet")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("eng-200-a1-f1: launched", out.stdout)

    def test_another_tickets_launch_is_not_the_work_tickets(self):
        other = [["eng-187-a1-f1", "launched https://claude.ai/code/session_9"]]
        worlds = [{"fires": other, "items": {}}] * 6 + [
            {"fires": other + w["fires"], "items": w["items"]} for w in HAPPY[6:]
        ]
        self.set_state(worlds=worlds)
        out = self.run_script(FIRST_RUN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("eng-200-a1-f1: launched", out.stdout)
        started = out.stdout.split("A worker started for ENG-200:\n", 1)[1].splitlines()
        self.assertIn("eng-200-a1-f1: launched", started[0])
        self.assertNotIn("eng-187", started[1])

    def test_interrupted_switch_finishes_on_the_next_run(self):
        # The deploy that switches to live dies after FACTORY_MODE was staged.
        self.set_state(deploy_fails_at=2)
        out = self.run_script(FIRST_RUN)
        self.assertNotEqual(out.returncode, 0)
        st = self.st()
        self.assertEqual(st["mode"], "qualification")
        self.assertIn("FACTORY_MODE", st["staged"])
        out = self.run_script(["5", "20", "0"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        st = self.st()
        self.assertEqual(st["mode"], "live")
        self.assertEqual(st["deploys"], 2)  # the practice deploy, then the finished switch
        self.assertEqual(st["report_probes"], 1)

    def test_saved_markers_are_read_back_through_a_shell(self):
        self.assertEqual(self.run_script(FIRST_RUN).returncode, 0)
        self.set_state(clipboard=None)
        out = self.run_script(())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNone(self.st()["clipboard"])  # the prompt step wasn't repeated
        self.assertEqual(self.st()["restarts"], 1)
        self.assertEqual(out.stdout.count("Already checked."), 2)

    def test_untracked_files_never_reach_the_image(self):
        (self.repo / "controller" / "evil.py").write_text("raise SystemExit")
        (self.repo / "deploy" / "fly" / "extra.sh").write_text("echo hi")
        out = self.run_script(FIRST_RUN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        st = self.st()
        for d in st["deploy_dirs"]:
            self.assertNotEqual(Path(d).resolve(), self.repo.resolve())
            self.assertFalse(Path(d).exists())  # the export is cleaned up
        self.assertIn("deploy/fly/fly.toml", st["deployed_files"])
        self.assertNotIn("controller/evil.py", st["deployed_files"])
        self.assertNotIn("deploy/fly/extra.sh", st["deployed_files"])


if __name__ == "__main__":
    unittest.main()
