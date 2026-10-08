"""Drafting a contract from an authorized Linear ticket (ENG-175).

Everything runs against fixtures: a fake ticket reader, a fake GitHub and the
pilot's drafting policy. Nothing here touches Linear, GitHub or a worker.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.loop.collect import GitHubUnreadable, NotFound
from controller.prepare import base as bases
from controller.prepare import policy as policies
from controller.prepare.drafter import RuleDrafter
from controller.prepare.preparer import LinearTicketReader, Preparer, assemble
from controller.prepare.review import review
from controller.prepare.ticket import Snapshot, clean, read, revision
from controller.service import onboarding
from controller.service.seams import Authorization, Prepared, Question

ROOT = Path(__file__).resolve().parent.parent
REPO = "rNavarrete/factory-pilot-demo"
PROJECT_ID = "58d48e11-3a45-4431-92b7-19b51a4e9539"
TRIG = "trig_01CHWbQ267i1CMLGUym1kGd9"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
GREEN = "a" * 40
RED = "b" * 40
OLDER = "c" * 40
POLICY = policies.load(ROOT / "deploy" / "pilot" / "drafting.json")
WORKFLOW = ".github/workflows/ci.yml"


def project(**changes):
    entry = {
        "linear_project_id": PROJECT_ID,
        "name": "Factory Pilot Demo",
        "repository": REPO,
        "routine_id": TRIG,
        "allowed_actions": ["modify-files", "add-files", "add-tests"],
        "checks": ["npm run typecheck", "npm test", "npm run build"],
        "max_attempts": 2,
    }
    entry.update(changes)
    doc = {"format": onboarding.FORMAT, "intake_enabled": True, "projects": [entry]}
    parsed = onboarding.parse(json.dumps(doc).encode(), repository=REPO, routine_id=TRIG)
    return parsed.project(PROJECT_ID)


def ticket(description, *, title="Add a thing", key="ENG-187", labels=(), project_id=PROJECT_ID):
    return Snapshot(
        id=f"issue-{key}",
        key=key,
        title=title,
        description=description,
        project_id=project_id,
        team_id="team-eng",
        parent_id=None,
        labels=tuple(labels),
    )


def authorize(snap, n=1):
    return Authorization(
        event_id=f"evt-{n}",
        issue_id=snap.id,
        issue_key=snap.key,
        project_id=PROJECT_ID,
        actor="Rolando",
        moved_at=NOW - timedelta(minutes=5),
        revision=revision(snap),
        evidence="fixture",
    )


class Reader:
    def __init__(self, *snaps):
        self.by_id = {s.id: s for s in snaps}
        self.calls = []

    def fetch(self, issue_id):
        self.calls.append(issue_id)
        return self.by_id.get(issue_id)


def run(sha, *, status="completed", path=WORKFLOW, event="push", repo=REPO, branch="main", rid=1):
    return {
        "id": rid,
        "head_sha": sha,
        "path": path,
        "event": event,
        "head_branch": branch,
        "status": status,
        "html_url": f"https://github.com/{REPO}/actions/runs/{rid}",
        "created_at": "2026-10-09T11:00:00Z",
        "check_suite_id": 100 + rid,
        "repository": {"full_name": repo},
        "head_repository": {"full_name": repo},
    }


class GitHub:
    """Answers the base selector's three calls from a small table."""

    def __init__(self, commits=(GREEN,), runs=None, jobs=None):
        self.commits = list(commits)
        self.runs = runs if runs is not None else {GREEN: [run(GREEN)]}
        self.jobs = jobs if jobs is not None else {1: "success"}
        self.apps = {}
        self.paths = []
        self.down = False

    def json(self, path):
        self.paths.append(path)
        if self.down:
            raise GitHubUnreadable("GitHub is down")
        if path.startswith(f"repos/{REPO}/commits?"):
            return [{"sha": s} for s in self.commits]
        if "/actions/workflows/ci.yml/runs?head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            return {"workflow_runs": self.runs.get(sha, [])}
        if "/check-suites/" in path:
            suite = int(path.rsplit("/", 1)[1])
            return {"app": {"slug": self.apps.get(suite, "github-actions")}}
        if "/actions/runs/" in path and path.endswith("/jobs?per_page=100"):
            rid = int(path.split("/actions/runs/")[1].split("/")[0])
            return {"jobs": [{"name": "verified", "conclusion": self.jobs.get(rid, "failure")}]}
        raise NotFound(path)


def prep(*snaps, github=None, policy=None):
    return Preparer(Reader(*snaps), github or GitHub(), lambda: policy or POLICY)


def sample_ticket(name, key="ENG-187"):
    """A ticket written the way Rolando writes them, from a completed sample."""
    c = json.loads((ROOT / "tasks" / "samples" / f"{name}.json").read_text())
    lines = "\n".join(f"- [ ] {a['statement']}" for a in c["acceptance_criteria"])
    return ticket(
        f"## Outcome\n\n{c['goal']}\n\n## Acceptance criteria\n\n{lines}\n",
        title=name.replace("-", " ").capitalize(),
        key=key,
    ), c


class SampleTests(unittest.TestCase):
    """Clear UI, logic and documentation tasks become contracts unaided."""

    def check_sample(self, name):
        snap, sample = sample_ticket(name)
        out = prep(snap).prepare(authorize(snap), project().as_mapping())
        self.assertIsInstance(out, Prepared, getattr(out, "text", ""))
        c = out.contract
        self.assertEqual(contracts.approval_errors(c), [])
        self.assertEqual(project().contract_problems(c, "eng-187"), [])
        self.assertEqual(c["base_commit"], GREEN)
        self.assertEqual(c["repository"], REPO)
        self.assertEqual(c["attempt_budget"], 2)
        self.assertEqual(
            [a["statement"] for a in c["acceptance_criteria"]],
            [a["statement"] for a in sample["acceptance_criteria"]],
        )
        self.assertEqual(
            [a["evidence"]["type"] for a in c["acceptance_criteria"]],
            [a["evidence"]["type"] for a in sample["acceptance_criteria"]],
        )
        self.assertIn(revision(snap), c["notes"])
        self.assertIn("evt-1", c["notes"])
        self.assertIn(POLICY.sha256, c["notes"])
        self.assertIn("The factory prepared this task", out.summary)
        self.assertNotIn("approve", out.summary.lower())
        return c

    def test_logic_sample(self):
        c = self.check_sample("rename-book")
        self.assertEqual({a["evidence"]["command"] for a in c["acceptance_criteria"]}, {"npm test"})

    def test_ui_sample(self):
        self.check_sample("clear-finished")

    def test_documentation_sample(self):
        self.check_sample("readme-checks")

    def test_count_sample(self):
        self.check_sample("count-by-status")

    def test_same_ticket_same_contract(self):
        snap, _ = sample_ticket("rename-book")
        a = prep(snap).prepare(authorize(snap), project().as_mapping())
        b = prep(snap).prepare(authorize(snap), project().as_mapping())
        self.assertEqual(contracts.digest(a.contract), contracts.digest(b.contract))

    def test_paths_and_actions_come_from_policy_only(self):
        snap, _ = sample_ticket("readme-checks")
        c = prep(snap).prepare(authorize(snap), project().as_mapping()).contract
        self.assertEqual(
            list(c["permitted_paths"]), list(POLICY.project(PROJECT_ID).writable_paths)
        )
        self.assertEqual(sorted(c["permitted_actions"]), ["add-files", "add-tests", "modify-files"])
        self.assertEqual(c["risk_markers"], [])

    def test_project_that_allows_less_gets_less(self):
        snap, _ = sample_ticket("rename-book")
        p = project(allowed_actions=["modify-files"], max_attempts=1)
        c = prep(snap).prepare(authorize(snap), p.as_mapping()).contract
        self.assertEqual(list(c["permitted_actions"]), ["modify-files"])
        self.assertEqual(c["attempt_budget"], 1)


class WaitTests(unittest.TestCase):
    """Ambiguous or changed tickets wait; nothing is invented."""

    def test_ambiguous_ticket_asks(self):
        snap = ticket(
            "## Acceptance criteria\n- [ ] The list is sorted by title or by date?\n"
            "- [ ] Sorting is remembered TBD\n"
        )
        out = prep(snap).prepare(authorize(snap), project().as_mapping())
        self.assertIsInstance(out, Question)
        self.assertEqual(out.kind, "product")
        self.assertIn("sorted by title or by date", out.text)
        self.assertIn("Sorting is remembered", out.text)

    def test_no_criteria_asks(self):
        for description in ("", "Make it nicer.", "## Acceptance criteria\n\n(none yet)"):
            snap = ticket(description)
            out = prep(snap).prepare(authorize(snap), project().as_mapping())
            self.assertIsInstance(out, Question, description)
            self.assertEqual(out.kind, "product")
            self.assertIn("acceptance criteria", out.text.lower())

    def test_changed_ticket_needs_fresh_authorization(self):
        snap, _ = sample_ticket("rename-book")
        auth = authorize(snap)
        edited = Snapshot(**{**snap.__dict__, "description": snap.description + "\n- [ ] More"})
        gh = GitHub()
        out = prep(edited, github=gh).prepare(auth, project().as_mapping())
        self.assertIsInstance(out, Question)
        self.assertEqual(out.kind, "changed")
        self.assertIn("move the ticket to Todo again", out.text)
        self.assertEqual(gh.paths, [], "nothing is drafted from changed text")

    def test_label_or_project_change_is_a_change(self):
        snap, _ = sample_ticket("rename-book")
        auth = authorize(snap)
        for change in ({"labels": ("baseline",)}, {"parent_id": "issue-other"}):
            edited = Snapshot(**{**snap.__dict__, **change})
            out = prep(edited).prepare(auth, project().as_mapping())
            self.assertEqual(out.kind, "changed", change)
        moved = Snapshot(**{**snap.__dict__, "project_id": "other-project"})
        self.assertEqual(prep(moved).prepare(auth, project().as_mapping()).kind, "changed")

    def test_question_keys_follow_the_ticket_text(self):
        a = ticket("Make it nicer.")
        b = ticket("Make it much nicer.")
        qa = prep(a).prepare(authorize(a), project().as_mapping())
        qa2 = prep(a).prepare(authorize(a, n=2), project().as_mapping())
        qb = prep(b).prepare(authorize(b), project().as_mapping())
        self.assertEqual(qa.key, qa2.key, "same text, same question: never posted twice")
        self.assertNotEqual(qa.key, qb.key)
        self.assertTrue(qa.key.startswith("product-"))

    def test_gone_ticket_stops(self):
        snap, _ = sample_ticket("rename-book")
        out = prep().prepare(authorize(snap), project().as_mapping())
        self.assertEqual(out.kind, "changed")

    def test_project_without_policy_is_retried_not_closed(self):
        """A setup gap raises, so the service retries each round instead of
        closing the ticket for good."""
        snap, _ = sample_ticket("rename-book")
        empty = policies.parse(json.dumps({"format": policies.FORMAT, "projects": []}).encode())
        with self.assertRaises(policies.PolicyError):
            prep(snap, policy=empty).prepare(authorize(snap), project().as_mapping())

    def test_policy_branch_must_be_the_onboarded_branch(self):
        snap, _ = sample_ticket("rename-book")
        doc = json.loads((ROOT / "deploy" / "pilot" / "drafting.json").read_text())
        doc["projects"][0]["base"]["branch"] = "develop"
        other = policies.parse(json.dumps(doc).encode())
        with self.assertRaises(policies.PolicyError):
            prep(snap, policy=other).prepare(authorize(snap), project().as_mapping())

    def test_too_many_criteria_proposes_a_split(self):
        lines = "\n".join(f"- [ ] sortBooks handles case {i}" for i in range(1, 10))
        snap = ticket(f"## Acceptance criteria\n{lines}\n")
        out = prep(snap).prepare(authorize(snap), project().as_mapping())
        self.assertEqual(out.kind, "split")
        self.assertIn("Part 1:", out.text)
        self.assertIn("Part 2:", out.text)
        self.assertNotIn("Part 3:", out.text)
        for i in range(1, 10):
            self.assertIn(f"case {i}”", out.text)
        self.assertIn("Nothing starts until you decide", out.text)


class BaseTests(unittest.TestCase):
    P = POLICY.project(PROJECT_ID).base

    def test_newest_green_commit(self):
        gh = GitHub(commits=[RED, GREEN], runs={RED: [run(RED, rid=2)], GREEN: [run(GREEN)]})
        b = bases.select(gh, REPO, self.P)
        self.assertEqual(b.commit, GREEN)
        self.assertEqual(b.skipped, (RED,))

    def test_running_ci_is_not_green(self):
        gh = GitHub(
            commits=[RED, GREEN],
            runs={RED: [run(RED, rid=1, status="in_progress")], GREEN: [run(GREEN, rid=3)]},
            jobs={1: "success", 3: "success"},
        )
        self.assertEqual(bases.select(gh, REPO, self.P).commit, GREEN)

    def test_untrusted_runs_do_not_count(self):
        for bad in (
            run(GREEN, path=".github/workflows/other.yml"),
            run(GREEN, event="pull_request"),
            run(GREEN, repo="someone/fork"),
            run(GREEN, branch="feature"),
            run(RED),
        ):
            gh = GitHub(commits=[GREEN], runs={GREEN: [bad]})
            with self.assertRaises(bases.NoEligibleBase, msg=str(bad)):
                bases.select(gh, REPO, self.P)

    def test_run_from_another_app_does_not_count(self):
        gh = GitHub()
        gh.apps[101] = "some-other-app"
        with self.assertRaises(bases.NoEligibleBase):
            bases.select(gh, REPO, self.P)

    def test_lookback_is_bounded(self):
        many = [f"{i:040x}" for i in range(1, 40)]
        gh = GitHub(commits=many + [GREEN])
        with self.assertRaises(bases.NoEligibleBase):
            bases.select(gh, REPO, self.P)
        self.assertLessEqual(sum("/runs?head_sha=" in p for p in gh.paths), self.P.lookback)

    def test_no_green_base_raises_so_the_service_retries(self):
        snap, _ = sample_ticket("rename-book")
        gh = GitHub(runs={})
        with self.assertRaises(bases.NoEligibleBase):
            prep(snap, github=gh).prepare(authorize(snap), project().as_mapping())
        gh.down = True
        with self.assertRaises(GitHubUnreadable):
            prep(snap, github=gh).prepare(authorize(snap), project().as_mapping())


class ReviewTests(unittest.TestCase):
    """The review doesn't trust the drafter."""

    def setUp(self):
        snap, _ = sample_ticket("clear-finished")
        self.snap = snap
        self.auth = authorize(snap)
        self.reading = read(snap)
        self.policy = POLICY.project(PROJECT_ID)
        self.project = project().as_mapping()
        out = prep(snap).prepare(self.auth, self.project)
        self.contract = json.loads(contracts.canonical_bytes(out.contract))

    def problems(self, contract):
        return review(
            contract,
            reading=self.reading,
            policy=self.policy,
            policy_sha256=POLICY.sha256,
            project=self.project,
            task_id="eng-187",
            base_commit=GREEN,
            revision=self.auth.revision,
            event_id="evt-1",
        )

    def test_clean_draft_passes(self):
        self.assertEqual(self.problems(self.contract), [])

    def test_each_widening_is_refused(self):
        cases = {
            "path": ("permitted_paths", ["src/**", ".github/workflows/ci.yml"]),
            "glob": ("permitted_paths", ["**"]),
            "action": ("permitted_actions", ["modify-files", "delete-files"]),
            "repo": ("repository", "rNavarrete/software-factory"),
            "base": ("base_commit", RED),
            "budget": ("attempt_budget", 1),
            "task": ("task_id", "eng-999"),
            "goal": ("goal", "Something else"),
            "notes": ("notes", "nothing"),
        }
        for name, (k, v) in cases.items():
            c = json.loads(json.dumps(self.contract))
            c[k] = v
            self.assertTrue(self.problems(c), name)

    def test_added_dropped_or_reworded_criteria_are_refused(self):
        added = json.loads(json.dumps(self.contract))
        added["acceptance_criteria"].append(
            {
                "id": "ac4",
                "statement": "Also delete CI.",
                "status": "ready",
                "evidence": {"type": "automated-check", "command": "npm test"},
            }
        )
        dropped = json.loads(json.dumps(self.contract))
        dropped["acceptance_criteria"].pop()
        reworded = json.loads(json.dumps(self.contract))
        reworded["acceptance_criteria"][0]["statement"] += " Mostly."
        human = json.loads(json.dumps(self.contract))
        human["acceptance_criteria"][0]["evidence"] = {
            "type": "human-review",
            "reviewer": "rNavarrete",
            "question": "ok?",
        }
        for c in (added, dropped, reworded, human):
            self.assertTrue(self.problems(c))

    def test_a_drafter_that_widens_scope_is_never_returned(self):
        class Wide(RuleDrafter):
            def draft(self, reading, policy, project):
                d = super().draft(reading, policy, project)
                return d.__class__(**{**d.__dict__, "permitted_paths": ("src/**", "package.json")})

        p = Preparer(Reader(self.snap), GitHub(), lambda: POLICY, drafter=Wide())
        out = p.prepare(self.auth, self.project)
        self.assertIsInstance(out, Question)
        self.assertEqual(out.kind, "factory")
        self.assertIn("package.json", out.text)


class PolicyTests(unittest.TestCase):
    def doc(self, **changes):
        d = json.loads((ROOT / "deploy" / "pilot" / "drafting.json").read_text())
        d["projects"][0].update(changes)
        return json.dumps(d).encode()

    def test_pilot_policy_loads(self):
        p = POLICY.project(PROJECT_ID)
        self.assertIn("package.json", p.protected_paths)
        self.assertEqual(p.base.job, "verified")

    def test_bad_policies_refused(self):
        for changes in (
            {"writable_paths": ["src/**", ".github/workflows/x.yml"]},
            {"writable_paths": ["**"]},
            {"writable_paths": ["../outside"]},
            {"writable_paths": ["/abs"]},
            {"test_command": "rm -rf /"},
            {"escalate_to": "not a login"},
            {"max_criteria": 0},
            {"extra": 1},
        ):
            with self.assertRaises(policies.PolicyError, msg=str(changes)):
                policies.parse(self.doc(**changes))


class TicketTests(unittest.TestCase):
    def test_revision_matches_intake(self):
        """Pinned against ENG-174's formula: same body, keys and encoding."""
        snap = ticket("café ✓\n- [ ] x", labels=("b", "a"))
        body = {
            "id": snap.id,
            "key": snap.key,
            "title": snap.title,
            "description": snap.description,
            "project_id": snap.project_id,
            "team_id": snap.team_id,
            "parent_id": snap.parent_id,
            "labels": ["a", "b"],
        }
        import hashlib

        want = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        self.assertEqual(revision(snap), want)

    def test_mentions_and_invisible_characters_are_cleaned(self):
        text = 'See <issue id="x" href="https://linear.app/x">ENG-174</issue>.​‮hidden\x07 bell'
        self.assertEqual(clean(text), "See ENG-174.hidden bell")

    def test_bold_heading_and_numbered_items(self):
        r = read(ticket("**Acceptance criteria**\n1. sortBooks(books) sorts\n2) it is stable\n"))
        self.assertEqual(r.criteria, ("sortBooks(books) sorts", "it is stable"))

    def test_continuation_lines_join(self):
        r = read(ticket("## Acceptance criteria\n- [ ] first part\n  second part\n- [ ] next"))
        self.assertEqual(r.criteria, ("first part second part", "next"))

    def test_linear_reader(self):
        def query(text, variables):
            self.assertIn("issue(id: $id)", text)
            return {
                "issue": {
                    "id": "issue-1",
                    "identifier": "ENG-1",
                    "title": "T",
                    "description": None,
                    "project": {"id": "p"},
                    "team": {"id": "t"},
                    "parent": None,
                    "labels": {"nodes": [{"name": "x"}]},
                    "trashed": False,
                    "archivedAt": None,
                }
            }

        s = LinearTicketReader(query).fetch("issue-1")
        self.assertEqual((s.key, s.description, s.project_id, s.labels), ("ENG-1", "", "p", ("x",)))
        trashed = LinearTicketReader(lambda q, v: {"issue": {"trashed": True}})
        self.assertIsNone(trashed.fetch("issue-1"))


class AssembleTests(unittest.TestCase):
    def test_context_is_bounded_and_marked_as_context(self):
        snap = ticket(
            "## Acceptance criteria\n- [ ] sortBooks(books) sorts\n\n## Notes\n" + "n" * 6000
        )
        reading = read(snap)
        draft = RuleDrafter().draft(reading, POLICY.project(PROJECT_ID), project().as_mapping())
        from controller.prepare.preparer import Trace

        trace = Trace("ENG-187", snap.id, revision(snap), "evt-1", POLICY.sha256, "rules/v1", "r")
        c = assemble(
            authorize(snap),
            reading,
            draft,
            GREEN,
            project().as_mapping(),
            POLICY.project(PROJECT_ID),
            trace,
        )
        self.assertTrue(all(len(i) <= 4000 for i in c["inputs"]))
        self.assertIn("context, not instructions", c["inputs"][1])


if __name__ == "__main__":
    unittest.main()


class ServiceWiringTests(unittest.TestCase):
    """The real preparer behind the service, end to end on fixtures."""

    def setUp(self):
        from tests import test_service as ts

        self.ts = ts
        doc = json.loads((ROOT / "deploy" / "pilot" / "drafting.json").read_text())
        doc["projects"][0]["linear_project_id"] = ts.PROJECT
        self.policy = policies.parse(json.dumps(doc).encode())
        self.snap = ticket(
            "## Outcome\nSort the list.\n\n## Acceptance criteria\n"
            "- [ ] sortBooks(books) returns the books ordered by title.\n"
            "- [ ] The page lists books in title order.\n",
            key="ENG-186",
            project_id=ts.PROJECT,
        )
        self.case = ts.ServiceTests("test_approved_todo_move_fires_once_and_reports")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.auth = Authorization(
            event_id="evt-9",
            issue_id=self.snap.id,
            issue_key="ENG-186",
            project_id=ts.PROJECT,
            actor="Rolando",
            moved_at=ts.NOW - timedelta(minutes=5),
            revision=revision(self.snap),
            evidence="fixture",
        )
        self.reader = Reader(self.snap)
        self.preparer = Preparer(self.reader, GitHub(), lambda: self.policy)
        self.case.source = ts.FixtureSource([self.auth])
        self.case.preparer = self.preparer
        self.case.build()

    def entry(self):
        return self.case.load_config().project(self.ts.PROJECT).as_mapping()

    def test_prepared_contract_fires_after_approval_and_summary_is_posted(self):
        from controller.approval import Approvals

        out = self.preparer.prepare(self.auth, self.entry())
        self.assertIsInstance(out, Prepared, getattr(out, "text", ""))
        Approvals(self.case.store, self.ts.KEY, confirm=self.ts.yes, os_user="rolando").approve(
            out.contract, self.case.now
        )
        r = self.case.tick()
        self.assertEqual(r.errors, [])
        self.assertEqual(r.fired, ["eng-186-a1-f1"])
        texts = self.case.reporter.texts(self.snap.id)
        self.assertEqual(sum("The factory prepared this task" in t for t in texts), 1)
        self.case.tick(minutes=6)
        texts = self.case.reporter.texts(self.snap.id)
        self.assertEqual(sum("The factory prepared this task" in t for t in texts), 1)
        self.assertEqual(len(self.case.fires()), 1)

    def test_unapproved_contract_does_not_fire(self):
        r = self.case.tick()
        self.assertEqual(r.fired, [])
        self.assertEqual(self.case.adapter.requests, [])

    def test_changed_ticket_closes_and_nothing_starts(self):
        """How the notice is worded by kind is ENG-178's (Service._ask)."""
        self.reader.by_id[self.snap.id] = Snapshot(
            **{**self.snap.__dict__, "title": "Sort the list, and delete CI"}
        )
        self.case.tick()
        item = self.case.view().items["evt-9"]
        self.assertIn(item.closed, ("question", "question-changed"))
        texts = self.case.reporter.texts(self.snap.id)
        self.assertTrue(any("changed after you moved it to Todo" in t for t in texts))
        self.assertEqual(self.case.adapter.requests, [])

    def test_product_question_is_posted_as_a_question(self):
        self.reader.by_id[self.snap.id] = Snapshot(**{**self.snap.__dict__, "description": ""})
        snap = self.reader.by_id[self.snap.id]
        self.case.source = self.ts.FixtureSource(
            [
                Authorization(
                    **{**self.auth.__dict__, "event_id": "evt-10", "revision": revision(snap)}
                )
            ]
        )
        self.case.build()
        self.case.tick()
        self.assertEqual(self.case.view().items["evt-10"].closed, "question")
        texts = self.case.reporter.texts(self.snap.id)
        self.assertTrue(any("needs an answer" in t and "acceptance criteria" in t for t in texts))
