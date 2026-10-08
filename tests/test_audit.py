"""The full control check (ENG-163): the auditor's rules, and the real control list.

The rule tests run on small made-up maps and stub runners. The real-list
tests read docs/governance-map.md and controller/audit/controls.py without
running any test (that would run this suite inside itself): they check that
the two agree and that every named test and red-team case exists.
"""

import unittest
from pathlib import Path

from controller.audit import Control, Record, map_classes, run_audit
from controller.audit.audit import render, run_redteam
from controller.audit.controls import CONTROLS

ROOT = Path(__file__).resolve().parents[1]
MAP_TEXT = (ROOT / "docs" / "governance-map.md").read_text(encoding="utf-8")

HEAD = "| ID | Requirement | Owner | Enforcing mechanism | Class | Issue(s) | Negative test |\n"
MAP = (
    "## 2. Classes\n\n| G-Z1 | not a control row |\n\n## 3. Requirement map\n\n"
    + HEAD
    + "|---|---|---|---|---|---|---|\n"
    + "| G-A1 | r | o | m | Code + Human gate | i | n |\n"
    + "| G-B2 | r | o | m | Platform | i | n |\n"
    + "| G-C9 | r | o | m | **Advisory** | i | n |\n"
    + "| G-G5 | r | o | m | **Advisory** + Detective (x) | i | n |\n"
    + "\n## 4. Later\n\n| G-A9 | ignored | x | x | Code | x | x |\n"
)
REC = Record("bot push to main refused with 403", "planning/x.md", "2026-10-08", "ruleset 1")


def passing(ids):
    return {i: None for i in ids}


def good():
    return (
        Control("G-A1", ("Code", "Human gate"), tests=("t.a1",)),
        Control("G-B2", ("Platform",), records=(REC,)),
        Control("G-C9", ("Advisory",), note="Stop procedure only; nothing relies on it."),
        Control("G-G5", ("Advisory", "Detective"), redteam=("c.g5",), note="never enabled"),
    )


def audit(controls, tests=passing, cases=passing, text=MAP):
    return run_audit(controls, text, run_tests=tests, run_cases=cases)


class MapTests(unittest.TestCase):
    def test_reads_only_section_3_rows(self):
        self.assertEqual(
            map_classes(MAP),
            {
                "G-A1": ("Code", "Human gate"),
                "G-B2": ("Platform",),
                "G-C9": ("Advisory",),
                "G-G5": ("Advisory", "Detective"),
            },
        )

    def test_real_map_has_every_control(self):
        m = map_classes(MAP_TEXT)
        self.assertEqual(len(m), 59)
        self.assertEqual(m["G-F3"], ("Advisory",))  # "**Advisory** (merge) — mitigated by ..."
        self.assertEqual(m["G-F6"], ("Human gate",))  # the 6-column row
        self.assertEqual(m["G-A10"], ("Detective", "Human gate"))

    def test_unreadable_or_repeated_rows_stop_the_audit(self):
        for bad in (
            MAP.replace("\n## 4. Later", "| G-A2 | r | o | m | Magic | i | n |\n\n## 4. Later"),
            MAP.replace("## 4. Later", "| G-A1 | r | o | m | Code | i | n |\n## 4."),
            MAP.replace("| G-B2 | r | o | m | Platform | i | n |", "| G-B2 | r | Platform |"),
            "## 3. Requirement map\n\nnothing here\n",
        ):
            with self.subTest(bad=bad[-60:]):
                r = audit(good(), text=bad)
                self.assertFalse(r.pilot_may_start)
                self.assertTrue(r.problems[0].startswith("governance map:"), r.problems)


class RuleTests(unittest.TestCase):
    def test_good_list_lets_the_pilot_start(self):
        r = audit(good())
        self.assertTrue(r.pilot_may_start, r.holds)
        self.assertEqual(r.holds, ())

    def test_every_map_id_exactly_once_and_nothing_else(self):
        cases = {
            "missing": good()[1:],
            "twice": (*good(), good()[0]),
            "unknown": (*good(), Control("G-A99", ("Code",), tests=("t",))),
        }
        for name, controls in cases.items():
            with self.subTest(name):
                self.assertFalse(audit(controls).pilot_may_start)
        # Blocked-mode checks are the only extra IDs allowed.
        extra = (*good(), Control("M-autofix", ("Code",), tests=("t.m",)))
        self.assertTrue(audit(extra).pilot_may_start, audit(extra).holds)

    def test_class_must_match_the_map(self):
        relabeled = (Control("G-C9", ("Code",), tests=("t",)),) + good()[:2] + good()[3:]
        r = audit(relabeled)
        self.assertFalse(r.pilot_may_start)
        self.assertIn("differs from the map", "\n".join(r.holds))
        # And an enforced control can't be relabeled advisory to dodge evidence.
        dodged = (Control("G-A1", ("Advisory",), note="x"),) + good()[1:]
        self.assertFalse(audit(dodged).pilot_may_start)

    def test_failing_missing_or_unrun_test_holds(self):
        for why in ("fails", "errors", "does not exist", "was skipped"):
            with self.subTest(why=why):
                r = audit(good(), tests=lambda ids, why=why: {i: why for i in ids})
                self.assertFalse(r.pilot_may_start)
                self.assertIn(f"test t.a1 {why}", "\n".join(r.holds))
        r = audit(good(), tests=lambda ids: {})
        self.assertIn("was not run", "\n".join(r.holds))

    def test_red_team_case_must_be_blocked(self):
        r = audit(good(), cases=lambda ids: {i: "not run yet" for i in ids})
        self.assertFalse(r.pilot_may_start)
        self.assertIn("red-team case c.g5: not run yet", "\n".join(r.holds))

    def test_code_needs_a_run_test_platform_needs_a_live_record(self):
        code_with_record_only = (Control("G-A1", ("Code", "Human gate"), records=(REC,)),)
        r = audit(code_with_record_only + good()[1:])
        self.assertIn("no passing test", "\n".join(r.holds))
        platform_with_test_only = (Control("G-B2", ("Platform",), tests=("t",)),)
        r = audit(good()[:1] + platform_with_test_only + good()[2:])
        self.assertIn("no live record", "\n".join(r.holds))

    def test_incomplete_record_does_not_count(self):
        for bad in (
            Record("", "w", "2026-10-08", "r"),
            Record("x", "", "2026-10-08", "r"),
            Record("x", "w", "yesterday", "r"),
            Record("x", "w", "2026-10-08", ""),
        ):
            with self.subTest(bad=bad):
                controls = good()[:1] + (Control("G-B2", ("Platform",), records=(bad,)),)
                self.assertFalse(audit(controls + good()[2:]).pilot_may_start)

    def test_pending_holds_even_with_evidence(self):
        pending = (Control("G-B2", ("Platform",), records=(REC,), pending="live case"),)
        r = audit(good()[:1] + pending + good()[2:])
        self.assertIn("not observed yet: live case", "\n".join(r.holds))

    def test_advisory_needs_only_its_note_and_mixed_rows_need_their_evidence(self):
        no_note = (Control("G-C9", ("Advisory",)),)
        self.assertFalse(audit(good()[:2] + no_note + good()[3:]).pilot_may_start)
        no_audit = (Control("G-G5", ("Advisory", "Detective"), note="never enabled"),)
        r = audit(good()[:3] + no_audit)
        self.assertIn("no passing test or red-team case", "\n".join(r.holds))

    def test_render_says_blocked_and_why(self):
        r = audit(good(), tests=lambda ids: {i: "fails" for i in ids})
        text = render(r, revision="abc")
        self.assertIn("The pilot stays blocked", text)
        self.assertIn("HOLDS: test t.a1 fails", text)
        self.assertIn("`abc`", text)
        self.assertIn("Every control the pilot needs is observed", render(audit(good())))


class RealListTests(unittest.TestCase):
    """The real control list against the real map, without running the tests."""

    def test_every_map_control_is_listed_once_with_its_class(self):
        r = audit(CONTROLS, text=MAP_TEXT)
        self.assertEqual(r.problems, ())
        drift = [h for h in r.holds if "differs from the map" in h]
        self.assertEqual(drift, [])

    def test_every_named_test_exists(self):
        import unittest as ut

        loader = ut.TestLoader()
        for c in CONTROLS:
            for t in c.tests:
                with self.subTest(control=c.id, test=t):
                    suite = loader.loadTestsFromName(t)
                    self.assertEqual(loader.errors, [])
                    self.assertEqual(suite.countTestCases(), 1)

    def test_every_named_red_team_case_exists(self):
        from redteam.cases import CASES, LIVE_CASES

        ids = {c.id for c in (*CASES, *LIVE_CASES)}
        for c in CONTROLS:
            for k in c.redteam:
                with self.subTest(control=c.id, case=k):
                    self.assertIn(k, ids)

    def test_unknown_red_team_case_is_reported(self):
        self.assertEqual(run_redteam(["no-such-case"]), {"no-such-case": "no such red-team case"})

    def test_records_are_complete(self):
        for c in CONTROLS:
            for rec in c.records:
                with self.subTest(control=c.id, record=rec.what[:40]):
                    self.assertEqual(rec.problems(), [])


if __name__ == "__main__":
    unittest.main()


class FakeApi:
    def __init__(self, prs, commits, runs, fail=()):
        self.prs, self.commits, self.runs, self.fail = prs, commits, runs, set(fail)

    def json(self, path):
        from controller.loop.collect import GitHubUnreadable

        if any(f in path for f in self.fail):
            raise GitHubUnreadable(f"{path}: HTTP 502")
        page = int(path.rsplit("page=", 1)[1]) if "page=" in path else 1
        if "/pulls?" in path:
            return self.prs if page == 1 else []
        if "/commits" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            return self.commits.get(n, []) if page == 1 else []
        if "/actions/runs" in path:
            ref = path.split("branch=")[1]
            return {"workflow_runs": [{"head_sha": s} for s in self.runs.get(ref, [])]}
        raise AssertionError(path)

    def raw(self, path):
        raise AssertionError(path)


def _pr(n, login="rnavarrete-factory-bot", opened="2026-10-08T14:47:53Z"):
    return {"number": n, "user": {"login": login}, "created_at": opened, "head": {"ref": f"b{n}"}}


def _commit(sha, when):
    return {"sha": sha, "commit": {"committer": {"date": when}}}


class AutofixTests(unittest.TestCase):
    def check(self, api):
        from controller.audit.autofix import check_autofix

        return check_autofix(api, "o/r", {"rnavarrete-factory-bot"})

    def test_one_commit_before_open_is_clean_and_people_are_skipped(self):
        api = FakeApi(
            [_pr(15), _pr(13, login="rNavarrete")],
            {15: [_commit("a" * 40, "2026-10-08T14:47:27Z")], 13: [_commit("b", "2099-01-01")]},
            {"b15": ["a" * 40, "a" * 40]},
        )
        r = self.check(api)
        self.assertTrue(r.clean, r.findings)
        self.assertEqual(r.checked, (15,))

    def test_a_push_after_open_is_found(self):
        api = FakeApi(
            [_pr(15)],
            {
                15: [
                    _commit("a" * 40, "2026-10-08T14:47:27Z"),
                    _commit("c" * 40, "2026-10-08T15:01:00Z"),
                ]
            },
            {"b15": ["a" * 40]},
        )
        r = self.check(api)
        self.assertFalse(r.clean)
        self.assertIn("after it opened", r.findings[0])

    def test_ci_at_two_heads_is_found_even_with_backdated_commits(self):
        api = FakeApi(
            [_pr(15)],
            {15: [_commit("a" * 40, "2026-10-08T14:00:00Z")]},
            {"b15": ["a" * 40, "c" * 40]},
        )
        self.assertIn("2 different head commits", self.check(api).findings[0])

    def test_unreadable_or_empty_is_never_clean(self):
        api = FakeApi([_pr(15)], {}, {}, fail={"/commits"})
        self.assertFalse(self.check(api).clean)
        self.assertFalse(self.check(FakeApi([], {}, {})).clean)
        self.assertFalse(self.check(FakeApi([_pr(15, login="someone")], {}, {})).clean)
