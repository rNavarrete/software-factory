"""Exercise the synthetic context probe through the real contract/fire boundary."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from controller import contract as contracts
from controller.adapter.routine import PayloadRejected, build_fire_text
from controller.interfaces import AttemptId, TaskId

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tools" / "context_fixture.py"


class ContextFixtureTests(unittest.TestCase):
    def probe(self):
        self.assertTrue(PROBE.is_file(), "the offline context probe is not implemented")
        spec = importlib.util.spec_from_file_location("context_fixture", PROBE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_bytes_survive_the_real_fire_envelope(self):
        probe = self.probe()
        files = {"reference.png": b"\x89PNG\r\n\x1a\n\x00\xff", "brief.md": b"Keep order.\n"}
        contract = probe.contract_for(files)
        digest = contracts.digest(contract)
        text = build_fire_text(contract, digest, AttemptId(TaskId(contract["task_id"]), 1))
        self.assertEqual(probe.unpack(text), files)
        self.assertEqual(contracts.validate(contract, digest), [])

    def test_changed_image_breaks_the_contract_digest(self):
        probe = self.probe()
        contract = probe.contract_for({"reference.png": b"original"})
        digest = contracts.digest(contract)
        contract["inputs"] = probe.contract_for({"reference.png": b"changed"})["inputs"]
        with self.assertRaises(PayloadRejected):
            build_fire_text(contract, digest, AttemptId(TaskId(contract["task_id"]), 1))

    def test_oversized_context_is_rejected_without_truncation(self):
        probe = self.probe()
        with self.assertRaises(PayloadRejected):
            probe.fire_text({"large.png": b"x" * 70000})

    def test_missing_chunk_is_rejected_even_with_a_recomputed_contract_digest(self):
        probe = self.probe()
        contract = probe.contract_for({"image.png": b"x" * 8000})
        contract["inputs"].pop()
        text = build_fire_text(
            contract, contracts.digest(contract), AttemptId(TaskId(contract["task_id"]), 1)
        )
        with self.assertRaisesRegex(
            ValueError, "missing, reordered or inconsistent context chunks"
        ):
            probe.unpack(text)

    def test_unsafe_artifact_names_are_rejected(self):
        probe = self.probe()
        for name in ("../escape.png", "/tmp/escape.png", "a/b.png", "a\\b.png"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                probe.fire_text({name: b"image"})

    def test_cli_builds_a_local_round_trip_without_claiming_cloud_success(self):
        self.probe()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "proof"
            run = subprocess.run(
                [sys.executable, "-m", "tools.context_fixture", "--out", str(out)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["local_byte_round_trip"], "passed")
            self.assertEqual(report["hosted_visual_inspection"], "not_run")
            self.assertEqual(report["reviewer_visual_inspection"], "not_run")
            original = ROOT / "tests/fixtures/context/design.png"
            self.assertEqual((out / "recovered/design.png").read_bytes(), original.read_bytes())
            capsule = json.loads((out / "fire-text.json").read_text())
            packed = self.probe().unpack(json.dumps(capsule))
            self.assertNotIn("expected-visual.json", packed)


if __name__ == "__main__":
    unittest.main()
