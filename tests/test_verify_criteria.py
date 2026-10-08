"""Per-criterion verifier (ENG-156). Each test class names the acceptance criterion it covers."""

import ast
import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from controller.contract import digest, freeze
from verify import (
    Candidate,
    CheckResult,
    Observation,
    Source,
    TrustPolicy,
    Verdict,
    WriterClaim,
    render,
    results_from_check_evidence,
    verify,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "schema" / "examples" / "filter-by-status.json"

HEAD = "a" * 40
NEW_HEAD = "b" * 40
MAIN = "c" * 40  # tip of main when the PR was checked
NEW_MAIN = "d" * 40
CI_URL = "https://github.com/rNavarrete/factory-pilot-demo/actions/runs/1"
REVIEWER = "rNavarrete"
WORKER = "rnavarrete-factory-bot"


def load_contract():
    return freeze(json.loads(EXAMPLE.read_text()))


CONTRACT = load_contract()
DIGEST = digest(CONTRACT)
BASE = CONTRACT["base_commit"]
COMMANDS = CONTRACT["verification_commands"]


def candidate(approved=None, **changes):
    d = approved or DIGEST
    c = Candidate(
        repository="rNavarrete/factory-pilot-demo",
        head_commit=HEAD,
        base_commit=MAIN,
        merge_base=BASE,
        branch="claude/filter-by-status-a1",
        pr_title=f"[filter-by-status a1 {d.short}] Filter books by status",
        pr_body=f"Adds a status filter.\n\n{d.pr_body_line}\n",
        changed_paths=("src/books.ts", "src/main.ts", "tests/books.test.ts"),
    )
    return replace(c, **changes)


def ci(command, exit_code=0, **changes):
    r = CheckResult(
        command=command,
        commit=HEAD,
        base_commit=MAIN,
        exit_code=exit_code,
        source=Source.CI,
        url=CI_URL,
        output_excerpt=f"{command}: ok" if exit_code == 0 else f"{command}: 1 failed",
        repository="rNavarrete/factory-pilot-demo",
        workflow_path=".github/workflows/ci.yml",
        app="github-actions",
    )
    return replace(r, **changes)


def all_green(**changes):
    return [ci(c, **changes) for c in COMMANDS]


def seen(criterion="ac3", verdict=Verdict.PASS, **changes):
    o = Observation(
        criterion=criterion,
        commit=HEAD,
        base_commit=MAIN,
        observer=REVIEWER,
        verdict=verdict,
        seen="Added three books, marked one reading and one done, picked 'reading': "
        "only that book listed; 'all' listed three.",
        limitations="Checked in Chrome only; did not try an empty list.",
    )
    return replace(o, **changes)


def run(results=None, observations=None, claims=(), cand=None, contract=CONTRACT, **kw):
    return verify(
        contract,
        kw.pop("approved", DIGEST),
        cand or candidate(),
        all_green() if results is None else results,
        [seen()] if observations is None else observations,
        claims,
        **kw,
    )


def verdicts(report):
    return {c.criterion: c.verdict for c in report.criteria}


class HappyPathTest(unittest.TestCase):
    def test_everything_shown_is_ready(self):
        report = run()
        self.assertEqual(report.blockers, ())
        self.assertTrue(report.ready)
        self.assertEqual(set(verdicts(report).values()), {Verdict.PASS})


class NamesDigestCommitAndEachCriterionTest(unittest.TestCase):
    """AC1: output names contract digest, candidate commit and each criterion."""

    def test_report_names_digest_commit_and_every_criterion(self):
        report = run()
        self.assertEqual(report.contract_digest, DIGEST.value)
        self.assertEqual(report.candidate_commit, HEAD)
        self.assertEqual(report.base_commit, MAIN)
        self.assertEqual([c.criterion for c in report.criteria], ["ac1", "ac2", "ac3"])

    def test_every_verdict_is_pass_fail_or_unknown(self):
        report = run(results=[ci("npm test", exit_code=1)], observations=[])
        self.assertEqual(
            verdicts(report), {"ac1": Verdict.FAIL, "ac2": Verdict.FAIL, "ac3": Verdict.UNKNOWN}
        )

    def test_rendered_report_names_them_too(self):
        text = render(run())
        self.assertIn(DIGEST.value, text)
        self.assertIn(HEAD, text)
        for cid in ("ac1", "ac2", "ac3"):
            self.assertIn(f"| {cid} | pass |", text)
        self.assertIn("this is not an approval", text)


class PassCitesEvidenceAndLimitsTest(unittest.TestCase):
    """AC2: each pass cites reproducible check output or observed behavior and its limits."""

    def test_automated_pass_cites_run_output_and_limitation(self):
        ac1 = run().criteria[0]
        self.assertEqual(ac1.verdict, Verdict.PASS)
        self.assertTrue(ac1.citations)
        for cite in ac1.citations:
            self.assertEqual(cite.url, CI_URL)
            self.assertIn("`npm test` exited 0", cite.detail)
            self.assertIn(HEAD, cite.detail)
            self.assertIn("npm test: ok", cite.detail)
            self.assertIn("does not show which test", cite.limitations)

    def test_observed_pass_cites_what_was_seen_and_limitations(self):
        ac3 = run().criteria[2]
        self.assertEqual(ac3.verdict, Verdict.PASS)
        (cite,) = ac3.citations
        self.assertIn("only that book listed", cite.detail)
        self.assertIn("Chrome only", cite.limitations)

    def test_observed_pass_without_limitations_is_unknown(self):
        report = run(observations=[seen(limitations=" ")])
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)

    def test_observed_pass_without_what_was_seen_is_unknown(self):
        report = run(observations=[seen(seen="")])
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)

    def test_every_pass_in_every_report_has_a_citation(self):
        for report in (run(), run(observations=[seen(), seen(observer="someone-else")])):
            for c in report.criteria:
                if c.verdict is Verdict.PASS:
                    self.assertTrue(c.citations, c.criterion)
                    for cite in c.citations:
                        self.assertTrue(cite.limitations.strip())


class MissingOrStaleEvidenceBlocksTest(unittest.TestCase):
    """AC3: missing or stale evidence and unverifiable requirements block readiness."""

    def test_no_evidence_at_all_is_unknown_and_not_ready(self):
        report = run(results=[], observations=[])
        self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})
        self.assertFalse(report.ready)

    def test_missing_observation_blocks(self):
        report = run(observations=[])
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)
        self.assertFalse(report.ready)
        self.assertTrue(any(b.startswith("ac3 is unknown") for b in report.blockers))

    def test_missing_verification_command_blocks_even_if_criteria_pass(self):
        report = run(results=[ci("npm test")])  # typecheck and build never ran
        self.assertEqual(verdicts(report)["ac1"], Verdict.PASS)
        self.assertFalse(report.ready)
        gate = next(g for g in report.gates if g.name == "verification commands")
        self.assertFalse(gate.ok)
        self.assertIn("npm run typecheck", gate.detail)

    def test_evidence_for_an_older_commit_is_stale(self):
        report = run(results=all_green(commit=NEW_HEAD))
        self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)
        self.assertFalse(report.ready)
        self.assertTrue(any(i.startswith("stale:") for i in report.ignored))

    def test_evidence_against_another_base_is_stale(self):
        report = run(results=all_green(base_commit=NEW_MAIN))
        self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)

    def test_stale_observation_is_not_used(self):
        report = run(observations=[seen(commit=NEW_HEAD)])
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)

    def test_unfinished_run_is_unknown_not_pass(self):
        report = run(results=[*all_green(), ci("npm test", exit_code=None)])
        self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)
        self.assertFalse(report.ready)

    def test_any_failing_run_beats_a_passing_one(self):
        report = run(results=[*all_green(), ci("npm test", exit_code=1)])
        self.assertEqual(verdicts(report)["ac1"], Verdict.FAIL)

    def test_criterion_needing_clarification_is_never_a_pass(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["acceptance_criteria"][2]["status"] = "needs-clarification"
        raw["acceptance_criteria"][2]["clarification"] = "Which control: select or buttons?"
        contract = freeze(raw)
        report = run(contract=contract, approved=digest(contract))
        self.assertFalse(report.ready)
        self.assertEqual(report.criteria, ())
        self.assertIn("needs clarification", report.gates[0].detail)

    def test_contract_that_does_not_match_the_approved_digest_blocks(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["permitted_paths"].append("package.json")
        report = run(contract=freeze(raw))  # still the old approved digest
        self.assertFalse(report.ready)
        self.assertFalse(report.gates[0].ok)
        self.assertIn("expected", report.gates[0].detail)

    def test_human_review_needs_the_named_reviewer(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["acceptance_criteria"][2]["evidence"] = {
            "type": "human-review",
            "reviewer": REVIEWER,
            "question": "Is the filter where a reader expects it?",
        }
        contract = freeze(raw)
        other = run(
            contract=contract, approved=digest(contract), observations=[seen(observer="helper")]
        )
        self.assertEqual(verdicts(other)["ac3"], Verdict.UNKNOWN)
        named = run(contract=contract, approved=digest(contract))
        self.assertEqual(verdicts(named)["ac3"], Verdict.PASS)


class UntrustedEvidenceTest(unittest.TestCase):
    """AC3 and the trusted-source lesson: a check name alone is not evidence."""

    def assert_untrusted(self, results, cand=None):
        report = run(results=results, cand=cand)
        self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)
        self.assertFalse(report.ready)
        self.assertTrue(any(i.startswith("untrusted:") for i in report.ignored))

    def test_result_from_another_workflow(self):
        self.assert_untrusted(all_green(workflow_path=".github/workflows/fake.yml"))

    def test_result_posted_by_another_app(self):
        self.assert_untrusted(all_green(app="some-bot"))

    def test_result_from_another_repository(self):
        self.assert_untrusted(all_green(repository="someone/fork"))

    def test_result_with_no_link(self):
        self.assert_untrusted(all_green(url=""))

    def test_ci_is_not_trusted_when_the_candidate_edits_its_own_checks(self):
        for path in (".github/workflows/ci.yml", "scripts/check.mjs"):
            with self.subTest(path=path):
                cand = candidate(changed_paths=("src/books.ts", path))
                self.assert_untrusted(all_green(), cand=cand)

    def test_rerun_by_the_worker_is_not_evidence(self):
        self.assert_untrusted(all_green(source=Source.RERUN, by=WORKER))

    def test_rerun_with_no_reviewer_is_not_evidence(self):
        self.assert_untrusted(all_green(source=Source.RERUN, by=""))

    def test_rerun_by_an_independent_reviewer_counts(self):
        report = run(results=all_green(source=Source.RERUN, by=REVIEWER, app="", workflow_path=""))
        self.assertTrue(report.ready, report.blockers)

    def test_worker_observation_is_not_evidence(self):
        report = run(observations=[seen(observer=WORKER)])
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)

    def test_policy_names_the_worker(self):
        policy = TrustPolicy(worker_logins=frozenset({REVIEWER}))
        report = run(policy=policy)
        self.assertEqual(verdicts(report)["ac3"], Verdict.UNKNOWN)


class CandidateGatesTest(unittest.TestCase):
    """Whole-candidate gates: markers, approved base (G-A11) and scope (G-A10)."""

    def assert_blocked(self, gate, **changes):
        report = run(cand=candidate(**changes))
        g = next(g for g in report.gates if g.name == gate)
        self.assertFalse(g.ok, g.detail)
        self.assertFalse(report.ready)
        return g

    def test_file_outside_permitted_paths(self):
        g = self.assert_blocked("scope", changed_paths=("src/books.ts", "package.json"))
        self.assertIn("package.json", g.detail)

    def test_star_does_not_cross_directories(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["permitted_paths"] = ["src/*.ts"]
        contract = freeze(raw)
        inside = run(
            contract=contract,
            approved=digest(contract),
            cand=candidate(digest(contract), changed_paths=("src/books.ts",)),
        )
        self.assertTrue(inside.ready, inside.blockers)
        nested = run(
            contract=contract,
            approved=digest(contract),
            cand=candidate(digest(contract), changed_paths=("src/deep/books.ts",)),
        )
        self.assertFalse(nested.ready)

    def test_double_star_matches_any_depth(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["permitted_paths"] = ["src/**"]
        contract = freeze(raw)
        report = run(
            contract=contract,
            approved=digest(contract),
            cand=candidate(digest(contract), changed_paths=("src/books.ts", "src/deep/x.ts")),
        )
        self.assertTrue(report.ready, report.blockers)

    def test_no_changes_is_not_ready(self):
        self.assert_blocked("scope", changed_paths=())

    def test_branched_from_a_later_main(self):
        self.assert_blocked("base", merge_base=NEW_MAIN)

    def test_other_repository(self):
        self.assert_blocked("repository", repository="someone/fork")

    def test_not_a_marker_branch(self):
        self.assert_blocked("branch", branch="feature/filter")

    def test_marker_branch_for_another_task(self):
        self.assert_blocked("branch", branch="claude/export-reading-list-a1")

    def test_title_marker_with_another_digest(self):
        self.assert_blocked("pr title", pr_title="[filter-by-status a1 000000000000] x")

    def test_title_marker_for_another_attempt(self):
        self.assert_blocked("pr title", pr_title=f"[filter-by-status a2 {DIGEST.short}] x")

    def test_title_without_marker(self):
        self.assert_blocked("pr title", pr_title="Filter books")

    def test_body_with_another_digest(self):
        self.assert_blocked("pr body", pr_body="Contract-Digest: " + "0" * 64)

    def test_body_with_two_digest_lines(self):
        self.assert_blocked("pr body", pr_body=f"{DIGEST.pr_body_line}\n{DIGEST.pr_body_line}")


class NewRevisionInvalidatesTest(unittest.TestCase):
    """AC4: a new candidate or base revision invalidates prior verdicts until checks rerun."""

    def test_report_is_current_for_the_same_revision(self):
        report = run()
        self.assertEqual(report.still_current(candidate()), [])

    def test_new_push_invalidates(self):
        report = run()
        why = report.still_current(candidate(head_commit=NEW_HEAD))
        self.assertEqual(len(why), 1)
        self.assertIn("candidate moved", why[0])

    def test_moved_base_invalidates(self):
        report = run()
        why = report.still_current(candidate(base_commit=NEW_MAIN))
        self.assertIn("base moved", why[0])

    def test_reverifying_the_new_revision_with_old_evidence_is_not_ready(self):
        old_evidence = all_green()
        for moved in (candidate(head_commit=NEW_HEAD), candidate(base_commit=NEW_MAIN)):
            report = run(results=old_evidence, observations=[seen()], cand=moved)
            self.assertFalse(report.ready)
            self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})

    def test_fresh_evidence_for_the_new_revision_restores_readiness(self):
        # The collector labels evidence from GitHub's record of each run; relabelling
        # old evidence is the collector's bug to avoid (see verify/criteria.py).
        moved = candidate(head_commit=NEW_HEAD, base_commit=NEW_MAIN)
        report = run(
            results=all_green(commit=NEW_HEAD, base_commit=NEW_MAIN),
            observations=[seen(commit=NEW_HEAD, base_commit=NEW_MAIN)],
            cand=moved,
        )
        self.assertTrue(report.ready, report.blockers)


class ReadOnlyTest(unittest.TestCase):
    """AC5: the verifier cannot approve the plan, mutate code or evidence, or release."""

    FORBIDDEN_MODULES = {
        "subprocess", "socket", "urllib", "http", "ftplib", "smtplib", "os", "shutil",
        "sqlite3", "pathlib", "io", "tempfile", "asyncio", "ssl", "multiprocessing",
    }  # fmt: skip
    FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input"}

    def files(self):
        files = sorted((ROOT / "verify").rglob("*.py"))
        self.assertTrue(files)
        return files

    def test_verifier_has_no_way_to_do_io(self):
        for path in self.files():
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    roots = {(node.module or "").split(".")[0]}
                else:
                    roots = set()
                self.assertFalse(roots & self.FORBIDDEN_MODULES, f"{path.name} imports {roots}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, self.FORBIDDEN_CALLS, path.name)

    def test_verifier_does_not_reach_controller_writers(self):
        allowed = {"controller.contract", "controller.interfaces"}
        for path in self.files():
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "controller"
                ):
                    self.assertIn(node.module, allowed, path.name)

    def test_inputs_are_unchanged(self):
        raw = json.loads(EXAMPLE.read_text())
        before = copy.deepcopy(raw)
        results, observations = all_green(), [seen()]
        claims = [WriterClaim("All tests pass.", says=Verdict.PASS)]
        snapshot = (list(results), list(observations), list(claims))
        verify(raw, digest(raw), candidate(), results, observations, claims)
        self.assertEqual(raw, before)
        self.assertEqual((results, observations, claims), snapshot)

    def test_report_and_evidence_are_frozen(self):
        report = run()
        with self.assertRaises(FrozenInstanceError):
            report.criteria[0].verdict = Verdict.FAIL  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            ci("npm test").exit_code = 1  # type: ignore[misc]

    def test_report_offers_no_approval_merge_or_release(self):
        names = {n for n in dir(run()) if not n.startswith("_")}
        for word in ("approve", "merge", "release", "deploy", "publish", "push"):
            self.assertFalse([n for n in names if word in n], word)
        self.assertNotIn("approved", {v.value for v in Verdict})


class WriterClaimIsNotEvidenceTest(unittest.TestCase):
    """AC6: a confident writer claim contradicted by independent evidence stays failed/unknown."""

    CLAIMS = [
        WriterClaim("Done. All tests pass and every criterion is met.", says=Verdict.PASS),
        WriterClaim("filterByStatus is implemented and tested.", criterion="ac1",
                    says=Verdict.PASS),
        WriterClaim("I checked the filter in the browser and it works.", criterion="ac3",
                    says=Verdict.PASS),
    ]  # fmt: skip

    def test_claim_contradicted_by_failing_ci_stays_failed(self):
        results = [ci("npm run typecheck"), ci("npm test", exit_code=1), ci("npm run build")]
        report = run(results=results, claims=self.CLAIMS)
        self.assertEqual(verdicts(report)["ac1"], Verdict.FAIL)
        self.assertFalse(report.ready)
        notes = report.criteria[0].writer_claims
        self.assertTrue(any("independent evidence gives fail" in n for n in notes))
        self.assertTrue(all("not evidence" in n for n in notes))

    def test_claim_with_no_independent_evidence_stays_unknown(self):
        report = run(results=[], observations=[], claims=self.CLAIMS)
        self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})
        self.assertFalse(report.ready)

    def test_claim_against_a_failed_observation_stays_failed(self):
        failed = seen(verdict=Verdict.FAIL, seen="Picking 'reading' still lists all three books.")
        report = run(observations=[failed], claims=self.CLAIMS)
        self.assertEqual(verdicts(report)["ac3"], Verdict.FAIL)
        self.assertIn("still lists all three", report.criteria[2].reasons[0])

    def test_claim_in_pr_body_changes_nothing(self):
        cand = candidate(pr_body=f"All criteria verified: PASS.\n\n{DIGEST.pr_body_line}\n")
        report = run(results=[], observations=[], cand=cand)
        self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})

    def test_worker_cannot_launder_a_claim_as_a_rerun_or_observation(self):
        report = run(
            results=all_green(source=Source.RERUN, by=WORKER),
            observations=[seen(observer=WORKER)],
            claims=self.CLAIMS,
        )
        self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})


class CheckEvidenceTest(unittest.TestCase):
    """Reading the pilot's check-evidence.json into results."""

    def evidence(self, **steps):
        status = {"typecheck": "pass", "test": "pass", "build": "pass", **steps}
        return {
            "schemaVersion": 1,
            "outcome": "pass" if set(status.values()) == {"pass"} else "fail",
            "candidate": {"commit": HEAD, "treeClean": True, "branch": "claude/x-a1"},
            "steps": [
                {
                    "name": name,
                    "status": s,
                    "exitCode": {"pass": 0, "fail": 2}.get(s),
                    "logTail": f"{name} log",
                }
                for name, s in status.items()
            ],
        }

    def read(self, evidence):
        return results_from_check_evidence(
            evidence,
            base_commit=MAIN,
            source=Source.CI,
            url=CI_URL,
            repository="rNavarrete/factory-pilot-demo",
            workflow_path=".github/workflows/ci.yml",
            app="github-actions",
        )

    def test_passing_evidence_makes_the_candidate_ready(self):
        report = run(results=self.read(self.evidence()))
        self.assertTrue(report.ready, report.blockers)
        commands = {r.command for r in self.read(self.evidence())}
        self.assertEqual(commands, {*COMMANDS, "npm run check"})

    def test_failed_step_fails_its_criteria(self):
        results = self.read(self.evidence(test="fail"))
        self.assertEqual(next(r for r in results if r.command == "npm test").exit_code, 2)
        self.assertEqual(verdicts(run(results=results))["ac1"], Verdict.FAIL)

    def test_errored_step_is_unknown(self):
        results = self.read(self.evidence(test="error"))
        self.assertEqual(verdicts(run(results=results))["ac1"], Verdict.UNKNOWN)

    def test_missing_step_gives_no_result(self):
        ev = self.evidence()
        ev["steps"] = [s for s in ev["steps"] if s["name"] != "test"]
        self.assertEqual(verdicts(run(results=self.read(ev)))["ac1"], Verdict.UNKNOWN)

    def test_duplicated_step_gives_no_result(self):
        ev = self.evidence()
        ev["steps"].append(dict(ev["steps"][1]))
        self.assertEqual(verdicts(run(results=self.read(ev)))["ac1"], Verdict.UNKNOWN)

    def test_evidence_for_another_commit_comes_out_stale(self):
        ev = self.evidence()
        ev["candidate"]["commit"] = NEW_HEAD
        self.assertEqual(verdicts(run(results=self.read(ev)))["ac1"], Verdict.UNKNOWN)

    def test_unsupported_evidence_is_refused(self):
        with self.assertRaises(ValueError):
            self.read({"schemaVersion": 2})
        with self.assertRaises(ValueError):
            self.read({"schemaVersion": 1, "candidate": {"commit": "main"}})


class ReviewFindingsTest(unittest.TestCase):
    """Holes an independent review found in the first version, each now closed."""

    def control_contract(self, *extra_paths):
        raw = json.loads(EXAMPLE.read_text())
        raw["permitted_paths"] += list(extra_paths)
        raw["permitted_actions"].append("change-control-files")
        raw["risk_markers"].append("control-change")
        contract = freeze(raw)
        return contract, digest(contract)

    def test_ci_is_not_trusted_when_any_control_file_changes(self):
        paths = [
            "vite.config.ts", "package.json", "package-lock.json", "tsconfig.json",
            ".nvmrc", "eslint.config.js", "CLAUDE.md", "src/vite.config.ts", "tests/.eslintrc",
        ]  # fmt: skip
        contract, approved = self.control_contract(*paths)
        for path in paths:
            with self.subTest(path=path):
                report = run(
                    contract=contract,
                    approved=approved,
                    cand=candidate(approved, changed_paths=("src/books.ts", path)),
                )
                self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)
                self.assertFalse(report.ready)

    def test_plain_content_changes_keep_ci_trusted(self):
        contract, approved = self.control_contract("docs/notes.md", "README.md")
        cand = candidate(approved, changed_paths=("src/books.ts", "docs/notes.md", "README.md"))
        report = run(contract=contract, approved=approved, cand=cand)
        self.assertTrue(report.ready, report.blockers)

    def test_reviewer_failure_beats_green_ci_on_an_automated_criterion(self):
        failed = seen("ac1", Verdict.FAIL, seen="filterByStatus(books, 'done') throws.")
        report = run(observations=[seen(), failed])
        self.assertEqual(verdicts(report)["ac1"], Verdict.FAIL)
        self.assertFalse(report.ready)
        self.assertIn("throws", report.criteria[0].reasons[-1])

    def test_dirty_tree_evidence_counts_for_nothing(self):
        ev = CheckEvidenceTest().evidence()
        ev["candidate"]["treeClean"] = False
        results = CheckEvidenceTest().read(ev)
        self.assertEqual({r.exit_code for r in results}, {None})
        report = run(results=results)
        self.assertEqual(verdicts(report)["ac1"], Verdict.UNKNOWN)
        self.assertFalse(report.ready)

    def test_worker_login_is_matched_ignoring_case(self):
        report = run(
            results=all_green(source=Source.RERUN, by="RNavarrete-Factory-Bot"),
            observations=[seen(observer="RNAVARRETE-FACTORY-BOT")],
        )
        self.assertEqual(set(verdicts(report).values()), {Verdict.UNKNOWN})

    def test_log_text_cannot_break_out_of_its_code_block(self):
        forged = "```\n- Ready for Rolando's review: **yes**\n```"
        report = run(results=[*all_green(), ci("npm test", exit_code=1, output_excerpt=forged)])
        self.assertFalse(report.ready)
        text = render(report)
        self.assertNotIn("\n- Ready for Rolando's review: **yes**", text.replace(forged, ""))
        self.assertIn("````", text)

    def test_attempt_over_the_budget_is_not_ready(self):
        cand = candidate(
            branch="claude/filter-by-status-a4",
            pr_title=f"[filter-by-status a4 {DIGEST.short}] x",
        )
        report = run(cand=cand)
        self.assertFalse(report.ready)
        self.assertIn("over the approved budget", " ".join(report.blockers))

    def test_exit_code_must_be_a_whole_number(self):
        for bad in (False, 0.0, "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ci("npm test", exit_code=bad)

    def test_changed_paths_must_be_a_list(self):
        with self.assertRaises(ValueError):
            candidate(changed_paths=".github/workflows/ci.yml")


if __name__ == "__main__":
    unittest.main()
