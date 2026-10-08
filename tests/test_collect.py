"""The evidence collector (ENG-145), against the fake GitHub in github_world.

The honest world must come out usable with no problems; each test then changes
one thing and checks the PR is not usable, for the right reason. Offline: the
GitHub here is ``tests.github_world.World``; ``GhApi`` is driven through an
injected ``run`` and never starts ``gh``.
"""

import copy
import subprocess
import unittest
from unittest import mock

from controller.interfaces import AttemptId, ContractDigest, TaskId
from controller.loop import collect as collect_mod
from controller.loop.check import assess
from controller.loop.collect import GhApi, GitHubUnreadable, NotFound, collect
from redteam import fixtures as fx
from tests.github_world import (
    ATTEMPT,
    NUMBER,
    REPO,
    RUN_ID,
    SUITE_ID,
    World,
    control_change,
    evidence,
    zipped,
)

OTHER_DIGEST = ContractDigest("f" * 64)


def zip_with_compress_type(name: str, data: object, compress_type: int) -> bytes:
    """A zip whose one member claims a compression method zipfile can't read."""
    b = bytearray(zipped(name, data))
    central = b.find(b"PK\x01\x02")
    b[central + 10 : central + 12] = compress_type.to_bytes(2, "little")
    local = b.find(b"PK\x03\x04")
    b[local + 8 : local + 10] = compress_type.to_bytes(2, "little")
    return bytes(b)


def encrypted_zip(name: str, data: object) -> bytes:
    """A zip whose one member is marked encrypted (needs a password to read)."""
    b = bytearray(zipped(name, data))
    b[b.find(b"PK\x01\x02") + 8] |= 1
    b[b.find(b"PK\x03\x04") + 6] |= 1
    return bytes(b)


class CollectCase(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def collect(self, world=None, **kw):
        return collect(world or self.world, fx.CONTRACT, fx.DIGEST, ATTEMPT, NUMBER, **kw)

    def assess(self, c):
        return assess(
            fx.CONTRACT,
            fx.DIGEST,
            c,
            observations=(fx.observation(),),
            clearances=(fx.clearance(),),
        )

    def assertProblem(self, c, *fragments):
        """Not usable, and some problem mentions every fragment."""
        self.assertFalse(c.usable)
        text = "\n".join(c.problems)
        for f in fragments:
            self.assertIn(f, text)

    def assertNeverReady(self, c):
        self.assertFalse(c.usable and self.assess(c).ready)
        self.assertFalse(self.assess(c).ready)


class HonestTests(CollectCase):
    def test_honest_world_is_usable_with_no_problems(self):
        c = self.collect()
        self.assertTrue(c.usable, c.problems)
        self.assertEqual(c.problems, ())
        self.assertFalse(c.pending)
        self.assertEqual(c.author, fx.WORKER)
        self.assertTrue(c.draft)
        self.assertEqual(c.candidate.head_commit, fx.HEAD)
        self.assertEqual(c.candidate.base_commit, fx.MAIN)
        self.assertEqual(c.candidate.merge_base, fx.BASE)
        self.assertEqual(c.candidate.repository, REPO)
        self.assertEqual(c.candidate.branch, ATTEMPT.branch)
        self.assertEqual(c.candidate.changed_paths, ("src/books.ts", "src/main.ts", fx.TEST_FILE))
        self.assertEqual(c.ci.id, RUN_ID)
        self.assertEqual(c.ci.verified, "success")
        self.assertEqual(c.ci.app, "github-actions")
        self.assertEqual(c.ci.base_sha, fx.MAIN)
        self.assertEqual(len(c.comments), 1)
        self.assertIsNotNone(c.control_change)
        self.assertFalse(c.control_change.flagged)

    def test_honest_world_reads_every_changed_file_at_head_and_merge_base(self):
        c = self.collect()
        got = {(s.path, s.commit) for s in c.sources}
        want = {(p, sha) for p in c.candidate.changed_paths for sha in (fx.HEAD, fx.BASE)}
        self.assertEqual(got, want)
        by_key = {(s.path, s.commit): s.text for s in c.sources}
        self.assertEqual(by_key[(fx.TEST_FILE, fx.HEAD)], fx.HEAD_TEST)
        self.assertEqual(by_key[(fx.TEST_FILE, fx.BASE)], fx.BASE_TEST)

    def test_results_are_labelled_from_githubs_record_of_the_run(self):
        c = self.collect()
        self.assertTrue(c.results)
        for r in c.results:
            self.assertEqual(r.commit, fx.HEAD)
            self.assertEqual(r.base_commit, fx.MAIN)
            self.assertEqual(r.url, fx.CI_URL)
            self.assertEqual(r.repository, REPO)
            self.assertEqual(r.workflow_path, fx.CI_WORKFLOW)
            self.assertEqual(r.app, "github-actions")

    def test_the_pr_body_is_a_claim_only(self):
        c = self.collect()
        self.assertEqual(len(c.claims), 1)
        self.assertEqual(c.claims[0].text, self.world.pr["body"])

    def test_honest_world_with_rolandos_inputs_is_ready(self):
        a = self.assess(self.collect())
        self.assertTrue(a.ready, a.blockers)

    def test_world_is_only_ever_read(self):
        self.collect()
        self.assertTrue(self.world.calls)
        for path in self.world.calls:
            self.assertTrue(path.startswith(f"repos/{REPO}/"), path)

    def test_bad_arguments_are_refused_before_any_read(self):
        with self.assertRaises(ValueError):
            self.collect(repo="not a repo")
        for bad in (0, -1, True, "7"):
            with self.assertRaises(ValueError):
                collect(self.world, fx.CONTRACT, fx.DIGEST, ATTEMPT, bad)
        self.assertEqual(self.world.calls, [])

    def test_unreadable_pr_raises(self):
        self.world.fail.add(f"repos/{REPO}/pulls/{NUMBER}")
        with self.assertRaises(GitHubUnreadable):
            self.collect()


class PullRequestTests(CollectCase):
    def test_merges_into_another_repository(self):
        self.world.pr["base"]["repo"]["full_name"] = fx.FORK
        self.assertProblem(self.collect(), "merges into", fx.FORK)

    def test_repository_names_compare_ignoring_case(self):
        self.world.pr["base"]["repo"]["full_name"] = REPO.upper()
        self.world.pr["head"]["repo"]["full_name"] = REPO.lower()
        self.assertTrue(self.collect().usable)

    def test_merges_into_another_branch(self):
        self.world.pr["base"]["ref"] = "release"
        self.assertProblem(self.collect(), "merges into branch", "'release'")

    def test_head_from_a_fork(self):
        self.world.pr["head"]["repo"]["full_name"] = fx.FORK
        self.assertProblem(self.collect(), "lives in", fx.FORK)

    def test_head_repository_deleted(self):
        self.world.pr["head"]["repo"] = None
        self.assertProblem(self.collect(), "a deleted repository")

    def test_head_branch_not_the_marker_branch(self):
        self.world.pr["head"]["ref"] = "claude/filter-by-status-a2"
        self.assertProblem(self.collect(), "branch is", "claude/filter-by-status-a2")

    def test_head_branch_lookalike(self):
        self.world.pr["head"]["ref"] = ATTEMPT.branch + "-x"
        self.assertProblem(self.collect(), "branch is")

    def test_title_with_extra_text(self):
        self.world.pr["title"] += " (also bumps deps)"
        self.assertProblem(self.collect(), "title is", "not exactly")

    def test_title_with_another_digest(self):
        self.world.pr["title"] = f"[filter-by-status a1 {OTHER_DIGEST.short}] filter-by-status"
        self.assertProblem(self.collect(), "title is")

    def test_body_without_contract_digest(self):
        self.world.pr["body"] = "Adds a status filter."
        self.assertProblem(self.collect(), "Contract-Digest", fx.DIGEST.short)

    def test_empty_body(self):
        self.world.pr["body"] = None
        c = self.collect()
        self.assertProblem(c, "Contract-Digest")
        self.assertEqual(c.claims, ())

    def test_body_with_contract_digest_twice(self):
        self.world.pr["body"] += fx.DIGEST.pr_body_line + "\n"
        self.assertProblem(self.collect(), "no single Contract-Digest")

    def test_body_with_another_contract_digest(self):
        self.world.pr["body"] = f"Adds a status filter.\n\n{OTHER_DIGEST.pr_body_line}\n"
        self.assertProblem(self.collect(), "Contract-Digest")

    def test_body_with_both_digests(self):
        self.world.pr["body"] += OTHER_DIGEST.pr_body_line + "\n"
        self.assertProblem(self.collect(), "Contract-Digest")

    def test_closed_pr(self):
        self.world.pr["state"] = "closed"
        self.assertProblem(self.collect(), "is closed")

    def test_opened_by_someone_else(self):
        for login in ("rNavarrete", "someone-else", "rnavarrete-factory-bot-x", "dependabot[bot]"):
            with self.subTest(login=login):
                w = self.world.copy()
                w.pr["user"]["login"] = login
                self.assertProblem(self.collect(w), "opened by", "not the worker")

    def test_worker_in_any_spelling_still_counts_as_the_worker(self):
        for login in (
            "rnavarrete-factory-bot",
            "RNavarrete-Factory-Bot",
            "rnavarrete-factory-bot[bot]",
            "RNAVARRETE-FACTORY-BOT[bot]",
        ):
            with self.subTest(login=login):
                w = self.world.copy()
                w.pr["user"]["login"] = login
                c = self.collect(w)
                self.assertTrue(c.usable, c.problems)

    def test_malformed_author_login_is_not_taken_as_the_worker(self):
        for login in ("not a login", "", "rnavarrete-factory-bot "):
            with self.subTest(login=login):
                w = self.world.copy()
                w.pr["user"]["login"] = login
                self.assertProblem(self.collect(w), "not the worker")

    def test_malformed_pr_answer_gives_no_candidate(self):
        for field, value in (
            (("head", "sha"), "abc"),
            (("base", "sha"), None),
            (("state",), "merged"),
            (("draft",), "yes"),
            (("changed_files",), -1),
            (("html_url",), "http://example.com"),
        ):
            with self.subTest(field=field):
                w = self.world.copy()
                target = w.pr
                for k in field[:-1]:
                    target = target[k]
                target[field[-1]] = value
                c = self.collect(w)
                self.assertIsNone(c.candidate)
                self.assertProblem(c, "missing", ".".join(field))

    def test_no_merge_base_gives_no_candidate(self):
        self.world.merge_base = "not-a-sha"
        c = self.collect()
        self.assertIsNone(c.candidate)
        self.assertProblem(c, "merge base")

    def test_unreadable_compare_gives_no_candidate(self):
        self.world.fail.add(f"repos/{REPO}/compare/")
        c = self.collect()
        self.assertIsNone(c.candidate)
        self.assertProblem(c, "Could not read what PR #7 changes")


class FileTests(CollectCase):
    def test_file_list_shorter_than_changed_files_is_truncation(self):
        self.world.pr["changed_files"] = 4
        self.assertProblem(self.collect(), "listed 3 of the PR's 4 changed files")

    def test_file_list_longer_than_changed_files(self):
        self.world.pr["changed_files"] = 2
        self.assertProblem(self.collect(), "listed 3 of the PR's 2 changed files")

    def test_files_are_paged(self):
        self.world.files = [{"filename": f"src/f{i}.ts"} for i in range(150)]
        self.world.pr["changed_files"] = 150
        c = self.collect()
        self.assertEqual(len(c.candidate.changed_paths), 150)
        pages = [p for p in self.world.calls if "/files?" in p]
        self.assertEqual(len(pages), 2)

    def test_rename_includes_both_names_read_at_head_and_base(self):
        self.world.files[0] = {
            "filename": "src/books.ts",
            "previous_filename": "src/old-books.ts",
            "status": "renamed",
        }
        self.world.contents[("src/old-books.ts", fx.BASE)] = b"export const old = 1;\n"
        c = self.collect()
        self.assertIn("src/books.ts", c.candidate.changed_paths)
        self.assertIn("src/old-books.ts", c.candidate.changed_paths)
        for path in ("src/books.ts", "src/old-books.ts"):
            for sha in (fx.HEAD, fx.BASE):
                self.assertIn(f"repos/{REPO}/contents/{path}?ref={sha}", self.world.calls)
        texts = {(s.path, s.commit): s.text for s in c.sources}
        self.assertIsNone(texts[("src/old-books.ts", fx.HEAD)])
        self.assertEqual(texts[("src/old-books.ts", fx.BASE)], "export const old = 1;\n")
        self.assertEqual(c.problems, ())

    def test_file_with_unusable_name(self):
        for name in ("", "/etc/passwd", "a\0b", None):
            with self.subTest(name=name):
                w = self.world.copy()
                w.files[0] = {"filename": name}
                self.assertProblem(self.collect(w), "no usable name")

    def test_binary_file_is_a_problem(self):
        self.world.contents[("src/main.ts", fx.HEAD)] = b"\xff\xfe\x00\x81binary"
        self.assertProblem(self.collect(), "src/main.ts", "not UTF-8")

    def test_oversized_file_is_a_problem(self):
        with mock.patch.object(collect_mod, "MAX_FILE_BYTES", 10):
            self.assertProblem(self.collect(), "byte limit")

    def test_file_read_failing_is_a_problem(self):
        self.world.fail.add(f"repos/{REPO}/contents/src/main.ts")
        c = self.collect()
        self.assertProblem(c, "Could not read src/main.ts")
        self.assertNeverReady(c)

    def test_new_file_has_no_text_at_base_and_is_fine(self):
        del self.world.contents[("src/main.ts", fx.BASE)]
        c = self.collect()
        self.assertTrue(c.usable, c.problems)
        texts = {(s.path, s.commit): s.text for s in c.sources}
        self.assertIsNone(texts[("src/main.ts", fx.BASE)])
        self.assertEqual(texts[("src/main.ts", fx.HEAD)], "render(); // with a status filter\n")

    def test_unreadable_comments_are_a_problem(self):
        self.world.fail.add(f"repos/{REPO}/issues/{NUMBER}/comments")
        self.assertProblem(self.collect(), "comments")

    def test_malformed_comment_is_a_problem(self):
        self.world.comments[0]["user"] = None
        self.assertProblem(self.collect(), "comments")


class CiRunTests(CollectCase):
    def test_no_run_yet_is_pending(self):
        self.world.runs = []
        c = self.collect()
        self.assertTrue(c.pending)
        self.assertFalse(c.usable)
        self.assertIsNone(c.ci)
        self.assertEqual(c.results, ())
        a = self.assess(c)
        self.assertFalse(a.ready)
        self.assertEqual(a.conclusion(), "pending")

    def test_run_in_progress_is_pending(self):
        self.world.runs[0]["status"] = "in_progress"
        self.world.runs[0]["conclusion"] = None
        self.world.jobs[RUN_ID][1] = {"name": "verified", "status": "in_progress"}
        c = self.collect()
        self.assertTrue(c.pending)
        self.assertFalse(c.usable)
        self.assertEqual(c.results, ())
        self.assertNotIn(f"actions/runs/{RUN_ID}/artifacts", "\n".join(self.world.calls))

    def test_run_without_verified_job_yet_is_pending(self):
        self.world.runs[0]["status"] = "in_progress"
        self.world.jobs[RUN_ID] = [{"name": "check", "status": "in_progress"}]
        self.assertTrue(self.collect().pending)

    def test_untrusted_runs_are_ignored(self):
        changes = {
            "another workflow path": ("path", ".github/workflows/other.yml"),
            "a look-alike workflow path": ("path", "x/.github/workflows/ci.yml"),
            "another repository": ("repository", {"full_name": fx.FORK}),
            "a fork's head": ("head_repository", {"full_name": fx.FORK}),
            "another event": ("event", "pull_request_target"),
            "another commit": ("head_sha", fx.NEW_HEAD),
            "no id": ("id", "101"),
        }
        for label, (key, value) in changes.items():
            with self.subTest(label):
                w = self.world.copy()
                w.runs[0][key] = value
                if key == "head_sha":
                    # The fake filters by head_sha itself; make it return the run.
                    w.runs.append(dict(w.runs[0], head_sha=fx.HEAD, path="other.yml"))
                c = self.collect(w)
                self.assertTrue(c.pending, c.problems)
                self.assertIsNone(c.ci)
                self.assertFalse(c.usable)
                self.assertNeverReady(c)

    def test_run_from_another_app_is_never_ready(self):
        self.world.suites[SUITE_ID] = {"app": {"slug": "evil-app"}}
        c = self.collect()
        self.assertEqual(c.ci.app, "evil-app")
        for r in c.results:
            self.assertEqual(r.app, "evil-app")
        self.assertNeverReady(c)

    def test_run_without_a_check_suite_is_never_ready(self):
        del self.world.runs[0]["check_suite_id"]
        c = self.collect()
        self.assertEqual(c.ci.app, "")
        self.assertNeverReady(c)

    def test_unreadable_check_suite_is_a_problem(self):
        del self.world.suites[SUITE_ID]
        c = self.collect()
        self.assertProblem(c, "Could not read CI")
        self.assertNeverReady(c)

    def test_verified_job_failure(self):
        self.world.jobs[RUN_ID][1]["conclusion"] = "failure"
        c = self.collect()
        self.assertProblem(c, "did not pass", "verified", "failure")
        self.assertNeverReady(c)
        self.assertEqual(self.assess(c).conclusion(), "failure")

    def test_verified_job_missing(self):
        self.world.jobs[RUN_ID] = [
            {"name": "check", "status": "completed", "conclusion": "success"}
        ]
        c = self.collect()
        self.assertProblem(c, "verified", "missing")
        self.assertNeverReady(c)

    def test_verified_job_duplicated(self):
        self.world.jobs[RUN_ID].append(
            {"name": "verified", "status": "completed", "conclusion": "success"}
        )
        c = self.collect()
        self.assertProblem(c, "verified", "ambiguous")
        self.assertNeverReady(c)

    def test_verified_job_skipped(self):
        self.world.jobs[RUN_ID][1]["conclusion"] = "skipped"
        self.assertProblem(self.collect(), "concluded skipped")

    def test_newest_run_is_chosen(self):
        older = copy.deepcopy(self.world.runs[0])
        older.update(id=100, created_at="2026-10-08T13:00:00Z", html_url=fx.CI_URL[:-3] + "100")
        self.world.runs.insert(0, older)
        self.world.jobs[100] = [
            {"name": "verified", "status": "completed", "conclusion": "failure"}
        ]
        c = self.collect()
        self.assertEqual(c.ci.id, RUN_ID)
        self.assertTrue(c.usable, c.problems)

    def test_newest_run_failing_wins_over_an_older_pass(self):
        newer = copy.deepcopy(self.world.runs[0])
        newer.update(id=102, created_at="2026-10-08T16:00:00Z", html_url=fx.CI_URL[:-3] + "102")
        self.world.runs.append(newer)
        self.world.jobs[102] = [
            {"name": "verified", "status": "completed", "conclusion": "failure"}
        ]
        c = self.collect()
        self.assertEqual(c.ci.id, 102)
        self.assertProblem(c, "did not pass")

    def test_newer_run_still_running_is_pending(self):
        newer = copy.deepcopy(self.world.runs[0])
        newer.update(id=102, created_at="2026-10-08T16:00:00Z", status="in_progress")
        self.world.runs.append(newer)
        self.world.jobs[102] = [{"name": "verified", "status": "queued"}]
        c = self.collect()
        self.assertTrue(c.pending)
        self.assertEqual(c.ci.id, 102)

    def test_run_base_differs_from_pr_base_is_stale(self):
        self.world.runs[0]["pull_requests"] = [{"number": NUMBER, "base": {"sha": fx.NEW_MAIN}}]
        c = self.collect()
        self.assertEqual(c.ci.base_sha, fx.NEW_MAIN)
        for r in c.results:
            self.assertEqual(r.base_commit, fx.NEW_MAIN)
        self.assertNeverReady(c)

    def test_run_without_a_base_for_this_pr(self):
        for prs in ([], [{"number": NUMBER + 1, "base": {"sha": fx.MAIN}}], None):
            with self.subTest(prs=prs):
                w = self.world.copy()
                w.runs[0]["pull_requests"] = prs
                c = self.collect(w)
                self.assertProblem(c, "which base")
                self.assertNeverReady(c)

    def test_unreadable_runs_list_is_a_problem(self):
        self.world.fail.add(f"repos/{REPO}/actions/workflows/")
        c = self.collect()
        self.assertProblem(c, "Could not read CI")
        self.assertFalse(c.pending)


class ArtifactTests(CollectCase):
    def test_evidence_artifact_missing(self):
        self.world.artifacts[RUN_ID] = [a for a in self.world.artifacts[RUN_ID] if a["id"] != 1]
        c = self.collect()
        self.assertProblem(c, f"no check-evidence-{fx.HEAD} artifact")
        self.assertEqual(c.results, ())
        self.assertNeverReady(c)

    def test_control_change_artifact_missing(self):
        self.world.artifacts[RUN_ID] = [a for a in self.world.artifacts[RUN_ID] if a["id"] != 2]
        c = self.collect()
        self.assertProblem(c, f"no control-change-{fx.HEAD} artifact")
        self.assertIsNone(c.control_change)
        self.assertNeverReady(c)

    def test_artifact_named_for_another_commit_is_missing(self):
        self.world.artifacts[RUN_ID][0]["name"] = f"check-evidence-{fx.NEW_HEAD}"
        self.assertProblem(self.collect(), "no check-evidence-")

    def test_expired_artifact(self):
        self.world.artifacts[RUN_ID][0]["expired"] = True
        self.assertProblem(self.collect(), "no check-evidence-")

    def test_artifact_from_another_run(self):
        self.world.artifacts[RUN_ID][0]["workflow_run"] = {"id": 999}
        self.assertProblem(self.collect(), "no check-evidence-")

    def test_artifact_not_a_zip(self):
        self.world.blobs[1] = b"this is not a zip"
        self.assertProblem(self.collect(), "Could not read check-evidence.json")

    def test_artifact_download_fails(self):
        self.world.fail.add(f"repos/{REPO}/actions/artifacts/1/")
        self.assertProblem(self.collect(), "Could not read check-evidence.json")

    def test_artifact_member_missing(self):
        self.world.blobs[1] = zipped("something-else", evidence())
        self.assertProblem(self.collect(), "Could not read check-evidence.json")

    def test_artifact_member_too_big(self):
        with mock.patch.object(collect_mod, "MAX_ARTIFACT_BYTES", 10):
            c = self.collect()
        self.assertProblem(c, "too large")
        self.assertNeverReady(c)

    def test_artifact_not_json(self):
        self.world.blobs[1] = zipped("check-evidence", b"{not json")
        self.assertProblem(self.collect(), "Could not read check-evidence.json")

    def test_artifact_not_an_object(self):
        self.world.blobs[1] = zipped("check-evidence", [1, 2])
        self.assertProblem(self.collect(), "not a JSON object")

    def test_artifact_not_utf8(self):
        self.world.blobs[1] = zipped("check-evidence", b"\xff\xfe")
        self.assertProblem(self.collect(), "Could not read check-evidence.json")

    def test_artifact_with_unsupported_compression_is_a_problem(self):
        self.world.blobs[1] = zip_with_compress_type("check-evidence", evidence(), 99)
        self.assertProblem(self.collect(), "check-evidence")

    def test_encrypted_artifact_is_a_problem(self):
        self.world.blobs[2] = encrypted_zip("control-change", control_change())
        self.assertProblem(self.collect(), "control-change")

    def test_newest_artifact_of_that_name_is_read(self):
        self.world.artifacts[RUN_ID].append(
            {
                "id": 3,
                "name": f"check-evidence-{fx.HEAD}",
                "expired": False,
                "created_at": "2026-10-08T14:05:00Z",
            }
        )
        self.world.blobs[3] = zipped("check-evidence", evidence(outcome="fail"))
        c = self.collect()
        self.assertIn(f"repos/{REPO}/actions/artifacts/3/zip", self.world.calls)
        check = [r for r in c.results if r.command == "npm run check"]
        self.assertEqual([r.exit_code for r in check], [1])

    def test_evidence_for_another_contract_digest(self):
        self.world.blobs[1] = zipped("check-evidence", evidence(digest=str(OTHER_DIGEST)))
        c = self.collect()
        self.assertProblem(c, "names contract", OTHER_DIGEST.value[:12])
        self.assertNeverReady(c)

    def test_evidence_without_contract_digest(self):
        e = evidence()
        del e["contractDigest"]
        self.world.blobs[1] = zipped("check-evidence", e)
        self.assertProblem(self.collect(), "names contract")

    def test_contract_claim_errors(self):
        self.world.blobs[1] = zipped(
            "check-evidence", evidence(contractClaimErrors=["two Contract-Digest lines"])
        )
        c = self.collect()
        self.assertProblem(c, "contract claim malformed", "two Contract-Digest lines")
        self.assertNeverReady(c)

    def test_empty_contract_claim_errors_are_fine(self):
        self.world.blobs[1] = zipped("check-evidence", evidence(contractClaimErrors=[]))
        self.assertTrue(self.collect().usable)

    def test_evidence_for_another_commit_is_stale(self):
        self.world.blobs[1] = zipped("check-evidence", evidence(commit=fx.NEW_HEAD))
        c = self.collect()
        for r in c.results:
            self.assertEqual(r.commit, fx.NEW_HEAD)
        a = self.assess(c)
        self.assertFalse(a.ready)
        self.assertIn("no trusted result", "\n".join(a.blockers))

    def test_unreadable_evidence_schema(self):
        self.world.blobs[1] = zipped("check-evidence", evidence(schemaVersion=2))
        self.assertProblem(self.collect(), "could not be read", "schemaVersion")

    def test_dirty_tree_evidence_is_never_ready(self):
        self.world.blobs[1] = zipped(
            "check-evidence",
            evidence(candidate={"commit": fx.HEAD, "treeClean": False, "branch": None}),
        )
        self.assertNeverReady(self.collect())

    def test_failing_step_is_never_ready(self):
        e = evidence(outcome="fail")
        e["steps"][4] = {"name": "test", "status": "fail", "exitCode": 1, "logTail": "1 failed"}
        self.world.blobs[1] = zipped("check-evidence", e)
        self.assertNeverReady(self.collect())

    def test_control_change_for_another_commit_is_never_ready(self):
        self.world.blobs[2] = zipped("control-change", control_change(head=fx.NEW_HEAD))
        self.assertNeverReady(self.collect())

    def test_control_change_flagged_is_never_ready_without_clearance(self):
        self.world.blobs[2] = zipped(
            "control-change", control_change(flagged=True, reasons=["changes ci.yml"])
        )
        c = self.collect()
        self.assertTrue(c.control_change.flagged)
        self.assertNeverReady(c)

    def test_unreadable_control_change(self):
        self.world.blobs[2] = zipped("control-change", {"flagged": False})
        self.assertProblem(self.collect(), "control-change report could not be read")


class FakeRun:
    """Stands in for subprocess.run; records argv and answers as scripted."""

    def __init__(self, returncode=0, stdout=b"{}", stderr=b"", raises=None):
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class GhApiTests(unittest.TestCase):
    def test_json_reads_with_get_only(self):
        run = FakeRun(stdout=b'{"a": 1}')
        api = GhApi(run=run, gh="/usr/bin/gh")
        self.assertEqual(api.json("repos/o/r/pulls/7"), {"a": 1})
        ((argv, kw),) = run.calls
        self.assertEqual(argv[:4], ["/usr/bin/gh", "api", "--method", "GET"])
        self.assertEqual(argv[-1], "repos/o/r/pulls/7")
        self.assertIn("Accept: application/vnd.github+json", argv)
        self.assertTrue(kw["capture_output"])
        self.assertFalse(kw["check"])
        self.assertGreater(kw["timeout"], 0)

    def test_raw_reads_bytes_with_get_only(self):
        run = FakeRun(stdout=b"\x00\x01")
        self.assertEqual(GhApi(run=run).raw("repos/o/r/contents/x?ref=y"), b"\x00\x01")
        ((argv, _),) = run.calls
        self.assertIn("Accept: application/vnd.github.raw", argv)

    def test_every_call_is_a_get_with_no_body(self):
        run = FakeRun(stdout=b"[]")
        api = GhApi(run=run)
        api.json("a")
        api.raw("b")
        for argv, _ in run.calls:
            self.assertEqual(argv[argv.index("--method") + 1], "GET")
            self.assertEqual(argv.count("--method"), 1)
            for flag in ("-X", "-f", "-F", "--field", "--raw-field", "--input", "POST"):
                self.assertNotIn(flag, argv)

    def test_collect_through_ghapi_issues_only_gets(self):
        world = World()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            path, accept = argv[-1], argv[argv.index("-H") + 1]
            try:
                if accept.endswith("raw"):
                    out = world.raw(path)
                else:
                    import json

                    out = json.dumps(world.json(path)).encode()
            except NotFound:
                return subprocess.CompletedProcess(argv, 1, b"", b"gh: Not Found (HTTP 404)")
            return subprocess.CompletedProcess(argv, 0, out, b"")

        c = collect(GhApi(run=run), fx.CONTRACT, fx.DIGEST, ATTEMPT, NUMBER)
        self.assertTrue(c.usable, c.problems)
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[1:4], ["api", "--method", "GET"])

    def test_http_404_is_not_found(self):
        run = FakeRun(returncode=1, stderr=b"gh: Not Found (HTTP 404)")
        with self.assertRaises(NotFound):
            GhApi(run=run).json("x")
        with self.assertRaises(NotFound):
            GhApi(run=run).raw("x")

    def test_other_failures_are_unreadable_not_not_found(self):
        for stderr in (b"gh: Server Error (HTTP 502)", b"HTTP 403: rate limit", b"", None):
            with self.subTest(stderr=stderr):
                run = FakeRun(returncode=1, stderr=stderr)
                with self.assertRaises(GitHubUnreadable) as cm:
                    GhApi(run=run).json("x")
                self.assertNotIsInstance(cm.exception, NotFound)

    def test_gh_not_running_is_unreadable(self):
        for exc in (FileNotFoundError("gh"), subprocess.TimeoutExpired("gh", 60)):
            with self.subTest(exc=exc):
                with self.assertRaises(GitHubUnreadable) as cm:
                    GhApi(run=FakeRun(raises=exc)).json("x")
                self.assertNotIsInstance(cm.exception, NotFound)

    def test_bad_json_is_unreadable(self):
        with self.assertRaises(GitHubUnreadable):
            GhApi(run=FakeRun(stdout=b"<html>")).json("x")


class AttemptBindingTests(CollectCase):
    def test_another_attempt_of_the_same_task_is_not_this_pr(self):
        a2 = AttemptId(TaskId("filter-by-status"), 2)
        c = collect(self.world, fx.CONTRACT, fx.DIGEST, a2, NUMBER)
        self.assertProblem(c, "branch is", "title is")

    def test_approved_digest_is_the_callers_never_the_prs(self):
        c = collect(self.world, fx.CONTRACT, OTHER_DIGEST, ATTEMPT, NUMBER)
        self.assertProblem(c, "title is", "Contract-Digest", "names contract")


if __name__ == "__main__":
    unittest.main()
