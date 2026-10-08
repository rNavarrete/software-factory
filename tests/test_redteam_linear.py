"""The Linear path's seeded bypass cases (ENG-158): ``redteam/linear_path.py``.

Every offline case must be blocked by the control it names, and its honest
twin (the same run without the one change) must not be: otherwise a blocked
case could mean the harness stops everything.
"""

import re
import tempfile
import unittest
from pathlib import Path

from redteam import Result, holds_qualification, render, run_all
from redteam import linear_path as lp
from redteam.cases import CASES, LIVE_CASES, every_case


class LinearCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r.case.id: r for r in run_all(lp.LINEAR_CASES)}

    def test_every_offline_case_is_blocked(self):
        for case in lp.LINEAR_CASES:
            with self.subTest(case=case.id):
                r = self.results[case.id]
                self.assertIs(r.result, Result.BLOCKED, r.observed)
                self.assertFalse(case.known_gap)

    def test_every_honest_twin_goes_through(self):
        twins = [c for c in lp.LINEAR_CASES if hasattr(c.check, "honest")]
        self.assertGreaterEqual(len(twins), len(lp.LINEAR_CASES) - 1)
        for case in twins:
            with self.subTest(case=case.id):
                honest = case.check.honest()
                self.assertFalse(honest.blocked, honest.detail)
                self.assertFalse(honest.wrong_reason, honest.detail)

    def test_every_group_has_offline_cases(self):
        for group in lp.LinearGroup:
            with self.subTest(group=group.name):
                self.assertTrue(any(c.group is group and c.check for c in lp.LINEAR_CASES))

    def test_ids_are_unique_across_the_whole_checklist(self):
        ids = [c.id for c in every_case()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            len(every_case()),
            len(CASES + LIVE_CASES) + len(lp.LINEAR_CASES) + len(lp.LINEAR_LIVE_CASES),
        )

    def test_the_comment_scan_would_see_another_linear_write(self):
        self.assertTrue(lp._only_comments().blocked)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "controller"
            root.mkdir()
            (root / "report.py").write_text('Q = "mutation { commentCreate(input: $i) { id } }"')
            self.assertTrue(lp._only_comments(root).blocked)
            for planted in (
                'Q = "mutation($i: IssueUpdateInput!) { issueUpdate(input: $i) { id } }"',
                'Q = "mutation { issueAddLabel(id: $id, labelId: $l) { success } }"',
                'V = {"stateId": todo}',
            ):
                with self.subTest(planted=planted):
                    (root / "other.py").write_text(planted)
                    self.assertFalse(lp._only_comments(root).blocked)
            (root / "other.py").unlink()
            (root / "report.py").unlink()
            out = lp._only_comments(root)
            self.assertTrue(out.wrong_reason, out.detail)


class LinearLiveCasesTest(unittest.TestCase):
    def test_live_cases_are_a_plan_only(self):
        for case in lp.LINEAR_LIVE_CASES:
            with self.subTest(case=case.id):
                self.assertIsNone(case.check)
                self.assertTrue(case.live)
                self.assertTrue(case.id.startswith("live-"))

    def test_the_plan_uses_at_most_one_worker_start(self):
        starts = sum(
            int(n)
            for c in lp.LINEAR_LIVE_CASES
            for n in re.findall(r"Count: (\d+) worker start", c.live)
        )
        self.assertEqual(starts, 1)

    def test_live_cases_hold_qualification_until_run(self):
        holds = holds_qualification(run_all())
        for case in lp.LINEAR_LIVE_CASES:
            self.assertIn(f"{case.id}: {Result.NOT_RUN.value}", holds)
        offline = [h for h in holds if not h.split(":")[0].startswith("live-")]
        self.assertEqual(offline, [])

    def test_the_report_has_a_section_per_linear_group(self):
        text = render(run_all())
        for group in lp.LinearGroup:
            self.assertIn(f"### {group.value}", text)


if __name__ == "__main__":
    unittest.main()
