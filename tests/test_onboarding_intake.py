"""The onboarding file's Todo-intake keys (ENG-174): ``approver_linear_user_id``,
``intake_since``, and per project ``issues`` and ``skip_labels``."""

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone

from controller.intake import policy_from
from controller.service import onboarding
from tests.test_recovery import REPO
from tests.test_service import PROJECT, TRIG, config


def parse(doc):
    return onboarding.parse(json.dumps(doc).encode(), repository=REPO, routine_id=TRIG)


class IntakeKeysTests(unittest.TestCase):
    def test_files_without_the_new_keys_still_parse(self):
        c = parse(config())
        self.assertIsNone(c.approver_linear_user_id)
        self.assertIsNone(c.intake_since)
        p = c.project(PROJECT)
        self.assertIsNone(p.issues)
        self.assertEqual(p.skip_labels, frozenset())

    def test_all_keys_parse(self):
        c = parse(
            config(
                approver_linear_user_id="cd9ec650-f957-4f25-b5f0-9c14bcae49c8",
                intake_since="2026-10-09T09:00:00+00:00",
                entry={"issues": ["ENG-187", "ENG-188"], "skip_labels": ["baseline", "manual"]},
            )
        )
        self.assertEqual(c.approver_linear_user_id, "cd9ec650-f957-4f25-b5f0-9c14bcae49c8")
        self.assertEqual(c.intake_since, datetime(2026, 10, 9, 9, 0, tzinfo=UTC))
        p = c.project(PROJECT)
        self.assertEqual(p.issues, frozenset({"ENG-187", "ENG-188"}))
        self.assertEqual(p.skip_labels, frozenset({"baseline", "manual"}))
        # And they reach the intake rules.
        rule = policy_from(c).projects[PROJECT]
        self.assertEqual(rule.issues, p.issues)
        self.assertEqual(rule.skip_labels, p.skip_labels)

    def test_intake_since_accepts_any_zone(self):
        cases = {
            "2026-10-09T09:00:00Z": datetime(2026, 10, 9, 9, 0, tzinfo=UTC),
            "2026-10-09T09:00:00.000Z": datetime(2026, 10, 9, 9, 0, tzinfo=UTC),
            "2026-10-09T11:00:00+02:00": datetime(
                2026, 10, 9, 11, 0, tzinfo=timezone(timedelta(hours=2))
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text):
                since = parse(config(intake_since=text)).intake_since
                self.assertEqual(since, expected)
                self.assertIsNotNone(since.tzinfo)

    def test_new_keys_are_part_of_the_recorded_hash(self):
        a = parse(config(entry={"issues": ["ENG-187"]}))
        b = parse(config(entry={"issues": ["ENG-188"]}))
        self.assertNotEqual(a.sha256, b.sha256)

    def test_bad_values_are_refused(self):
        bad = {
            "approver empty": config(approver_linear_user_id=""),
            "approver blank": config(approver_linear_user_id="   "),
            "approver number": config(approver_linear_user_id=42),
            "approver list": config(approver_linear_user_id=["a"]),
            "since without zone": config(intake_since="2026-10-09T09:00:00"),
            "since a date": config(intake_since="2026-10-09"),
            "since not a time": config(intake_since="yesterday"),
            "since a number": config(intake_since=1791536400),
            "since empty": config(intake_since=""),
            "issues empty": config(entry={"issues": []}),
            "issues text": config(entry={"issues": "ENG-187"}),
            "issues lowercase": config(entry={"issues": ["eng-187"]}),
            "issues number zero": config(entry={"issues": ["ENG-0"]}),
            "issues url": config(entry={"issues": ["https://linear.app/x/issue/ENG-187"]}),
            "issues id": config(entry={"issues": ["cd9ec650-f957-4f25-b5f0-9c14bcae49c8"]}),
            "issues padded": config(entry={"issues": [" ENG-187"]}),
            "issues not text": config(entry={"issues": [187]}),
            "skip text": config(entry={"skip_labels": "baseline"}),
            "skip blank": config(entry={"skip_labels": [" "]}),
            "skip not text": config(entry={"skip_labels": [1]}),
            "skip empty": config(entry={"skip_labels": []}),
            "protected text": config(entry={"protected_paths": ".github/"}),
            "protected empty": config(entry={"protected_paths": []}),
            "protected absolute": config(entry={"protected_paths": ["/etc"]}),
            "protected parent": config(entry={"protected_paths": ["../x"]}),
            "protected root": config(entry={"protected_paths": ["/"]}),
            "unknown top key": config(approver="x"),
        }
        for name, doc in bad.items():
            with self.subTest(name), self.assertRaises(onboarding.OnboardingError):
                parse(doc)

    def test_protected_paths_parse_and_are_hashed(self):
        doc = config(entry={"protected_paths": [".github/", "CLAUDE.md"]})
        (project,) = parse(doc).projects.values()
        self.assertEqual(project.protected_paths, frozenset({".github/", "CLAUDE.md"}))
        self.assertNotEqual(parse(doc).sha256, parse(config()).sha256)

    def test_null_values_mean_not_set(self):
        c = parse(config(approver_linear_user_id=None, intake_since=None))
        self.assertIsNone(c.approver_linear_user_id)
        self.assertIsNone(c.intake_since)

    def test_pilot_onboarding_file_lists_only_the_factory_tickets(self):
        from pathlib import Path

        path = Path(__file__).parents[1] / "deploy" / "pilot" / "onboarding.json"
        if not path.exists():
            self.skipTest("no pilot onboarding file")
        doc = json.loads(path.read_text())
        (entry,) = doc["projects"]
        # The pilot's factory tickets plus the go-live samples (docs/go-live.md).
        self.assertEqual(
            sorted(entry["issues"]),
            ["ENG-187", "ENG-188", "ENG-189", "ENG-191", "ENG-200", "ENG-201", "ENG-202"],
        )
        for baseline in ("ENG-186", "ENG-190", "ENG-192", "ENG-193"):
            self.assertNotIn(baseline, entry["issues"])


if __name__ == "__main__":
    unittest.main()
