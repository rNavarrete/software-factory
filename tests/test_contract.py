"""Contract format, validation, canonical bytes and digest (ENG-144)."""

import copy
import json
import re
import unittest
from pathlib import Path

from controller import contract as ct
from controller.interfaces import AttemptId, ContractDigest, TaskId

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "schema" / "examples"
SCHEMA = ROOT / "schema" / "contract-v1.schema.json"
OTHER_COMMIT = "7ecb861cc6e08305094bbd9f942160f75028a5da"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def ready() -> dict:
    """The approvable example, as a fresh mutable dict."""
    return example("filter-by-status")


class ExamplesTest(unittest.TestCase):
    def test_there_are_two_examples(self):
        self.assertEqual(
            sorted(p.stem for p in EXAMPLES.glob("*.json")),
            ["export-reading-list", "filter-by-status"],
        )

    def test_both_examples_are_well_formed(self):
        for path in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(path.name):
                self.assertEqual(ct.structure_errors(ct.loads(path.read_text())), [])

    def test_ready_example_validates_against_its_own_digest(self):
        c = ready()
        self.assertEqual(ct.validate(c, ct.digest(c)), [])
        self.assertEqual(ct.validate(c, str(ct.digest(c))), [])

    def test_needs_clarification_example_cannot_be_approved(self):
        c = example("export-reading-list")
        errors = ct.approval_errors(c)
        self.assertEqual(len(errors), 1)
        self.assertIn("ac3", errors[0])
        self.assertIn("needs clarification", errors[0])
        self.assertEqual(ct.validate(c, ct.digest(c)), errors)


class SchemaFileTest(unittest.TestCase):
    """The JSON Schema file must describe the same fields the validator enforces."""

    def setUp(self):
        self.schema = json.loads(SCHEMA.read_text())

    def test_required_and_optional_fields_match(self):
        self.assertEqual(self.schema["required"], list(ct.contract.REQUIRED))
        self.assertEqual(
            set(self.schema["properties"]), set(ct.contract.REQUIRED) | set(ct.contract.OPTIONAL)
        )
        self.assertIs(self.schema["additionalProperties"], False)

    def test_enums_match(self):
        props = self.schema["properties"]
        self.assertEqual(props["format"]["const"], ct.FORMAT)
        self.assertEqual(tuple(props["permitted_actions"]["items"]["enum"]), ct.ACTIONS)
        self.assertEqual(tuple(props["risk_markers"]["items"]["enum"]), ct.RISK_MARKERS)
        crit = self.schema["$defs"]["criterion"]["properties"]
        self.assertEqual(
            tuple(e["properties"]["type"]["const"] for e in crit["evidence"]["oneOf"]),
            ct.EVIDENCE_TYPES,
        )
        self.assertEqual(tuple(crit["status"]["enum"]), ct.contract.STATUSES)
        self.assertEqual(props["attempt_budget"]["maximum"], ct.contract.MAX_ATTEMPT_BUDGET)


class StructureTest(unittest.TestCase):
    def assertRejected(self, c: dict, fragment: str):
        errors = ct.structure_errors(c)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")
        self.assertTrue(ct.validate(c, "0" * 64))

    def test_not_an_object(self):
        for value in (None, [], "x", 3):
            self.assertEqual(ct.structure_errors(value), ["contract must be a JSON object"])
            self.assertEqual(ct.validate(value, "0" * 64), ["contract must be a JSON object"])

    def test_each_required_field(self):
        for key in ct.contract.REQUIRED:
            with self.subTest(key):
                c = ready()
                del c[key]
                self.assertRejected(c, f"{key} is required")

    def test_unknown_field(self):
        c = ready()
        c["priority"] = "high"
        self.assertRejected(c, "priority is not a contract field")

    def test_wrong_format(self):
        c = ready()
        c["format"] = "factory-contract/v2"
        self.assertRejected(c, "format must be")

    def test_base_must_be_a_full_commit_not_a_branch(self):
        for base in (
            "main",
            "refs/heads/main",
            "v1.0",
            "6c6badb",
            "6C6BADB8F086B7C4A3EAF3BA43317854E87E9B96",
            7,
        ):
            with self.subTest(base):
                c = ready()
                c["base_commit"] = base
                self.assertRejected(c, "base_commit must be a full 40-character")

    def test_task_id(self):
        for task in ("Filter", "filter_status", "-x", "a--b", "x" * 65, "", 5):
            with self.subTest(task):
                c = ready()
                c["task_id"] = task
                self.assertRejected(c, "task_id must be")

    def test_wrong_types(self):
        cases = {
            "goal": ("", "goal must be non-empty text"),
            "inputs": ("README.md", "inputs must be a list"),
            "repository": ("factory-pilot-demo", "repository must be 'owner/name'"),
            "permitted_paths": ([], "permitted_paths must not be empty"),
            "verification_commands": ([], "verification_commands must not be empty"),
            "acceptance_criteria": ([], "acceptance_criteria must not be empty"),
            "escalate_to": ("rolando@example.com", "escalate_to must be a GitHub login"),
            "version": ("   ", "version must be non-empty text"),
        }
        for key, (value, fragment) in cases.items():
            with self.subTest(key):
                c = ready()
                c[key] = value
                self.assertRejected(c, fragment)

    def test_attempt_budget(self):
        for budget in (0, 4, -1, True, "3", 2.0):
            with self.subTest(budget):
                c = ready()
                c["attempt_budget"] = budget
                self.assertTrue(ct.structure_errors(c))

    def test_floats_are_rejected(self):
        c = ready()
        c["attempt_budget"] = 2.0
        self.assertRejected(c, "float")
        with self.assertRaises(ValueError):
            ct.digest(c)

    def test_paths(self):
        for path in ("/etc/passwd", "../x", "src/../x", "src//x", "./src", "src\\x", "src/a b"):
            with self.subTest(path):
                c = ready()
                c["permitted_paths"] = [path]
                self.assertRejected(c, "permitted_paths[0]")

    def test_duplicate_list_entries(self):
        c = ready()
        c["permitted_paths"] = ["src/books.ts", "src/books.ts"]
        self.assertRejected(c, "permitted_paths[1] repeats")

    def test_unsupported_action_and_marker(self):
        c = ready()
        c["permitted_actions"] = ["push-to-main"]
        self.assertRejected(c, "permitted_actions[0] must be one of")
        c = ready()
        c["risk_markers"] = ["spicy"]
        self.assertRejected(c, "risk_markers[0] must be one of")

    def test_risky_actions_need_their_marker(self):
        for action, marker in ct.contract.ACTION_NEEDS_MARKER.items():
            with self.subTest(action):
                c = ready()
                c["permitted_actions"] = ["modify-files", action]
                self.assertRejected(c, f"needs risk marker {marker}")
                c["risk_markers"] = [marker]
                self.assertEqual(ct.structure_errors(c), [])

    def test_dependencies_name_exact_commits(self):
        dep = {"repository": "rNavarrete/factory-pilot-demo", "commit": OTHER_COMMIT, "reason": "x"}
        c = ready()
        c["depends_on"] = [dep]
        self.assertEqual(ct.structure_errors(c), [])
        c["depends_on"] = [{**dep, "commit": "main"}]
        self.assertRejected(c, "depends_on[0].commit must be a full 40-character")
        c["depends_on"] = [{"repository": dep["repository"], "commit": OTHER_COMMIT}]
        self.assertRejected(c, "depends_on[0] must have exactly the keys")


class CriteriaTest(unittest.TestCase):
    def crit(self, **changes) -> dict:
        c = ready()
        c["acceptance_criteria"][0].update(changes)
        for k, v in changes.items():
            if v is None:
                del c["acceptance_criteria"][0][k]
        return c

    def assertRejected(self, c: dict, fragment: str):
        errors = ct.structure_errors(c)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_missing_evidence(self):
        self.assertRejected(self.crit(evidence=None), "acceptance_criteria[0].evidence is required")

    def test_unsupported_evidence_type(self):
        self.assertRejected(
            self.crit(evidence={"type": "vibes"}), "acceptance_criteria[0].evidence.type must be"
        )

    def test_each_evidence_type_needs_its_fields(self):
        for kind, fields in ct.contract._EVIDENCE_FIELDS.items():
            for missing in fields:
                with self.subTest(kind=kind, missing=missing):
                    ev = {"type": kind, **{f: "npm test" for f in fields if f != missing}}
                    self.assertRejected(
                        self.crit(evidence=ev), f"evidence.{missing} is required for {kind}"
                    )

    def test_evidence_fields_from_another_type_are_rejected(self):
        ev = {"type": "automated-check", "command": "npm test", "reviewer": "rNavarrete"}
        self.assertRejected(self.crit(evidence=ev), "evidence.reviewer is not a field")

    def test_automated_check_must_use_a_listed_command(self):
        ev = {"type": "automated-check", "command": "npm run lint"}
        self.assertRejected(self.crit(evidence=ev), "must be one of verification_commands")

    def test_human_review_needs_a_login(self):
        ev = {"type": "human-review", "reviewer": "Rolando N", "question": "ok?"}
        self.assertRejected(self.crit(evidence=ev), "reviewer must be a GitHub login")

    def test_ids(self):
        self.assertRejected(self.crit(id="1"), "id must look like ac1")
        self.assertRejected(self.crit(id="ac2"), "id ac2 is used twice")

    def test_status(self):
        self.assertRejected(self.crit(status="maybe"), "status must be one of")
        self.assertRejected(self.crit(status=None), "status is required")
        self.assertRejected(self.crit(status="needs-clarification"), "clarification is required")
        self.assertRejected(self.crit(clarification="why?"), "clarification is only allowed")

    def test_needs_clarification_is_well_formed_but_not_approvable(self):
        c = self.crit(status="needs-clarification", clarification="Which statuses?")
        self.assertEqual(ct.structure_errors(c), [])
        self.assertEqual(
            ct.approval_errors(c),
            ["acceptance_criteria[0] (ac1) needs clarification: Which statuses?"],
        )
        self.assertEqual(ct.validate(c, ct.digest(c)), ct.approval_errors(c))


class NeverRaisesTest(unittest.TestCase):
    """The routine adapter calls validate before every launch: it must return
    errors for any input, never raise."""

    def junk(self) -> list:
        deep: list = []
        for _ in range(5000):
            deep = [deep]
        c = ready()
        return [
            None,
            [],
            object(),
            {1: "x"},
            {"\ud800": 1},
            {**c, "notes": "\ud800"},
            {**c, "attempt_budget": 10**5000},
            {**c, "notes": deep},
            {**c, "notes": float("nan")},
            {**c, "notes": {"a": object()}},
            {**c, "acceptance_criteria": [{"evidence": {"type": []}}]},
            {**c, "acceptance_criteria": [{"evidence": {"type": {}}, "id": [], "status": {}}]},
            {**c, "acceptance_criteria": "ac1"},
            {**c, "acceptance_criteria": [None, 3, []]},
            {**c, "permitted_actions": [[], {}], "risk_markers": [{}]},
            {**c, "depends_on": [[], {"a": 1}, {"repository": 1, "commit": [], "reason": {}}]},
            {**c, "task_id": []},
            {**c, "permitted_paths": [None, {}]},
        ]

    def test_bad_contracts(self):
        for i, value in enumerate(self.junk()):
            with self.subTest(i):
                errors = ct.validate(value, "0" * 64)
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)
                self.assertTrue(all(isinstance(e, str) for e in errors))

    def test_bad_expected_digest(self):
        class Boom:
            def __str__(self):
                raise RuntimeError

        for bad in (None, "", "F" * 64, "0" * 63, Boom(), 5):
            with self.subTest(repr(bad) if not isinstance(bad, Boom) else "Boom"):
                self.assertEqual(
                    ct.validate(ready(), bad),
                    ["expected digest must be 64 lowercase hex characters"],
                )

    def test_loads_raises_only_value_error(self):
        for text in ("[" * 100_000 + "]" * 100_000, '{"a": ' + "9" * 5000 + "}", "{", "\xff"):
            with self.subTest(text[:10]):
                with self.assertRaises(ValueError):
                    ct.loads(text)


class SchemaPatternsTest(unittest.TestCase):
    """Sample values must get the same verdict from the schema patterns and the validator."""

    def test_patterns_agree(self):
        schema = json.loads(SCHEMA.read_text())
        defs, props = schema["$defs"], schema["properties"]
        cases = {
            "base_commit": (defs["commit"]["pattern"], ["a" * 40, "A" * 40, "a" * 39, "main"]),
            "repository": (
                defs["repository"]["pattern"],
                ["rNavarrete/x", "a-/x", "a--b/x", "-a/x", "a/b/c", "a/.x", "a"],
            ),
            "escalate_to": (defs["login"]["pattern"], ["rNavarrete", "a-b", "a-", "a--b", "-a"]),
            "task_id": (props["task_id"]["pattern"], ["a-b", "a--b", "A", "a_b", "-a", "x" * 64]),
        }
        for key, (pattern, values) in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    c = ready()
                    c[key] = value
                    schema_ok = re.search(pattern, value) is not None
                    validator_ok = not any(e.startswith(key) for e in ct.structure_errors(c))
                    self.assertEqual(schema_ok, validator_ok)

    def test_path_pattern_agrees(self):
        pattern = json.loads(SCHEMA.read_text())["properties"]["permitted_paths"]["items"][
            "pattern"
        ]
        for value in ("src/a.ts", "src/*.ts", "/a", "a//b", "../a", "a/./b", "a/ b", "a\\b", "a/"):
            with self.subTest(value):
                c = ready()
                c["permitted_paths"] = [value]
                schema_ok = re.search(pattern, value) is not None
                schema_ok = schema_ok and not ({".", ".."} & set(value.split("/")))
                self.assertEqual(schema_ok, not ct.structure_errors(c))


class CanonicalDigestTest(unittest.TestCase):
    def test_key_order_and_layout_do_not_matter(self):
        text = (EXAMPLES / "filter-by-status.json").read_text()
        a = ct.loads(text)
        b = ct.loads(json.dumps(json.loads(text), indent=4))
        reordered = dict(reversed(list(json.loads(text).items())))
        self.assertEqual(ct.canonical_bytes(a), ct.canonical_bytes(b))
        self.assertEqual(ct.digest(a), ct.digest(reordered))

    def test_canonical_bytes_form(self):
        self.assertEqual(
            ct.canonical_bytes({"b": [1, {"d": "é", "c": None}], "a": True}),
            '{"a":true,"b":[1,{"c":null,"d":"é"}]}'.encode(),
        )

    def test_digest_is_sha256_of_canonical_bytes(self):
        c = ready()
        self.assertEqual(ct.digest(c), ContractDigest.of(ct.canonical_bytes(c)))

    def test_pinned_digest(self):
        # Pins the canonical form: if this changes, every approval ever given breaks.
        self.assertEqual(
            str(ct.digest({"b": [1, {"d": "é", "c": None}], "a": True})),
            "b56515827d996f78785feb87c17f2fd96235141b1e74b434486eef2ff47f990e",
        )

    def test_any_content_change_changes_the_digest_even_with_the_same_version(self):
        base = ready()
        changes = {
            "goal": base["goal"] + " ",
            "base_commit": OTHER_COMMIT,
            "permitted_paths": base["permitted_paths"] + ["src/extra.ts"],
            "attempt_budget": 2,
            "permitted_actions": ["modify-files", "add-tests", "add-files"],
            "verification_commands": ["npm test"],
        }
        for key, value in changes.items():
            with self.subTest(key):
                c = ready()
                c[key] = value
                self.assertEqual(c["version"], base["version"])
                self.assertNotEqual(ct.digest(c), ct.digest(base))
                errors = ct.validate(c, ct.digest(base))
                self.assertTrue(errors[-1].startswith("contract digest is "), errors)

    def test_wrong_digest_is_refused(self):
        c = ready()
        errors = ct.validate(c, "f" * 64)
        self.assertEqual(errors, [f"contract digest is {ct.digest(c)}, expected {'f' * 64}"])

    def test_digest_matches_the_pr_markers(self):
        c = ready()
        d = ct.digest(c)
        attempt = AttemptId(TaskId(c["task_id"]), 1)
        title = attempt.pr_title_marker(d) + " Filter books by status"
        parsed, short = AttemptId.from_pr_title(title)
        self.assertEqual((parsed, short), (attempt, d.short))
        self.assertEqual(ContractDigest.from_pr_body(f"x\n{d.pr_body_line}\n"), d)


class ImmutabilityTest(unittest.TestCase):
    def test_loads_gives_a_read_only_contract(self):
        c = ct.loads((EXAMPLES / "filter-by-status.json").read_text())
        with self.assertRaises(TypeError):
            c["attempt_budget"] = 3  # type: ignore[index]
        with self.assertRaises(TypeError):
            c["acceptance_criteria"][0]["status"] = "ready"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            c["permitted_paths"].append("x")  # type: ignore[union-attr]
        self.assertEqual(ct.validate(c, ct.digest(ready())), [])

    def test_freeze_copies(self):
        c = ready()
        frozen = ct.freeze(c)
        c["permitted_paths"].append("src/extra.ts")
        self.assertNotIn("src/extra.ts", frozen["permitted_paths"])

    def test_loads_rejects_ambiguous_json(self):
        for text in (
            '{"a": 1, "a": 2}',
            '{"a": NaN}',
            '{"a": Infinity}',
            "[1]",
        ):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    ct.loads(text)

    def test_binding_covers_scope_base_and_budget(self):
        base = ct.binding(ready())
        self.assertEqual(
            set(base),
            {*ct.BOUND_FIELDS, "digest", "task_id", "version"},
        )
        self.assertEqual(base["digest"], str(ct.digest(ready())))
        for key, value in {
            "base_commit": OTHER_COMMIT,
            "permitted_paths": ["src/**"],
            "attempt_budget": 1,
            "repository": "rNavarrete/software-factory",
        }.items():
            with self.subTest(key):
                c = ready()
                c[key] = value
                changed = ct.binding(c)
                self.assertNotEqual(changed[key], base[key])
                self.assertNotEqual(changed["digest"], base["digest"])

    def test_binding_refuses_a_malformed_contract(self):
        c = ready()
        c["base_commit"] = "main"
        with self.assertRaises(ValueError):
            ct.binding(c)

    def test_validate_does_not_change_its_input(self):
        c = ready()
        before = copy.deepcopy(c)
        ct.validate(c, "0" * 64)
        ct.digest(c)
        self.assertEqual(c, before)


if __name__ == "__main__":
    unittest.main()
