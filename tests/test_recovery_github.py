"""Reading an attempt's work from GitHub with ``gh api`` (ENG-153).

No network: ``run`` is replaced with a fake that answers like ``gh`` would.
"""

import json
import subprocess
import unittest

from controller.recovery import GhCliReader, GitHubUnreadable

SHA = "a" * 40
MERGE = "b" * 40


def pull(**changes):
    item = {
        "number": 7,
        "html_url": "https://github.com/rNavarrete/factory-pilot-demo/pull/7",
        "title": "[pilot-1 a1 0123456789ab] pilot-1",
        "body": "Contract-Digest: " + "0" * 64,
        "head": {
            "ref": "claude/pilot-1-a1",
            "sha": SHA,
            "repo": {"full_name": "rNavarrete/factory-pilot-demo"},
        },
        "user": {"login": "rnavarrete-factory-bot"},
        "state": "open",
        "draft": True,
        "merged_at": None,
        "merge_commit_sha": "c" * 40,
    }
    item.update(changes)
    return item


class Gh:
    """Answers ``gh api <path>`` from a dict of path -> (exit code, stdout)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        code, out = self.answers[args[-1]]
        if isinstance(code, BaseException):
            raise code
        stdout = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(args, code, stdout, "HTTP 500" if code else "")


REPO = "rNavarrete/factory-pilot-demo"
REFS = f"repos/{REPO}/git/matching-refs/heads/claude/pilot-1-a1"
BRANCH_PULLS = f"repos/{REPO}/pulls?state=all&per_page=100&head=rNavarrete%3Aclaude%2Fpilot-1-a1"
RECENT = f"repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100"
PERMISSION = f"repos/{REPO}/collaborators/rnavarrete-factory-bot/permission"


class GhCliReaderTest(unittest.TestCase):
    def reader(self, **answers):
        self.gh = Gh(answers)
        return GhCliReader(run=self.gh)

    def test_branch_head_is_the_exact_ref_not_a_prefix_match(self):
        refs = [
            {"ref": "refs/heads/claude/pilot-1-a10", "object": {"sha": "d" * 40}},
            {"ref": "refs/heads/claude/pilot-1-a1", "object": {"sha": SHA}},
        ]
        self.assertEqual(
            self.reader(**{REFS: (0, refs)}).branch_head(REPO, "claude/pilot-1-a1"), SHA
        )

    def test_missing_branch_is_none(self):
        refs = [{"ref": "refs/heads/claude/pilot-1-a10", "object": {"sha": SHA}}]
        self.assertIsNone(self.reader(**{REFS: (0, refs)}).branch_head(REPO, "claude/pilot-1-a1"))

    def test_runs_gh_without_a_shell_and_with_a_timeout(self):
        self.reader(**{REFS: (0, [])}).branch_head(REPO, "claude/pilot-1-a1")
        args, kwargs = self.gh.calls[0]
        self.assertEqual(args[:2], ["gh", "api"])
        self.assertNotIn("shell", kwargs)
        self.assertGreater(kwargs["timeout"], 0)

    def test_pulls_for_branch_filters_by_owner_and_branch(self):
        prs = self.reader(**{BRANCH_PULLS: (0, [pull()])}).pulls_for_branch(
            REPO, "claude/pilot-1-a1"
        )
        self.assertEqual(len(prs), 1)
        pr = prs[0]
        self.assertEqual((pr.number, pr.head_branch, pr.head_sha), (7, "claude/pilot-1-a1", SHA))
        self.assertEqual(pr.author, "rnavarrete-factory-bot")
        self.assertTrue(pr.draft)
        self.assertFalse(pr.merged)

    def test_merge_commit_only_when_merged(self):
        open_pr = self.reader(**{RECENT: (0, [pull()])}).recent_pulls(REPO)[0]
        self.assertIsNone(open_pr.merge_commit)
        merged = pull(state="closed", merged_at="2026-10-08T12:00:00Z", merge_commit_sha=MERGE)
        pr = self.reader(**{RECENT: (0, [merged])}).recent_pulls(REPO)[0]
        self.assertTrue(pr.merged)
        self.assertEqual(pr.merge_commit, MERGE)

    def test_closed_unmerged_is_not_merged(self):
        pr = self.reader(**{RECENT: (0, [pull(state="closed")])}).recent_pulls(REPO)[0]
        self.assertEqual(pr.state, "closed")
        self.assertFalse(pr.merged)

    def test_null_body_and_deleted_fork(self):
        item = pull(body=None)
        item["head"]["repo"] = None
        pr = self.reader(**{RECENT: (0, [item])}).recent_pulls(REPO)[0]
        self.assertEqual(pr.body, "")
        self.assertIsNone(pr.head_repo)

    def test_malformed_answers_are_unreadable_not_guessed(self):
        bad = [
            pull(number="7"),
            pull(number=True),
            pull(state="merged"),
            pull(draft="false"),
            pull(html_url="http://github.com/x"),
            pull(head={"ref": "x", "sha": "short"}),
            pull(user=None),
            pull(state="closed", merged_at="2026-10-08T12:00:00Z", merge_commit_sha=None),
            "not an object",
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(GitHubUnreadable):
                self.reader(**{RECENT: (0, [item])}).recent_pulls(REPO)
        for answer in ({"message": "x"}, "not json"):
            with self.subTest(answer=answer), self.assertRaises(GitHubUnreadable):
                self.reader(**{RECENT: (0, answer)}).recent_pulls(REPO)

    def test_gh_failures_are_unreadable(self):
        for code in (1, subprocess.TimeoutExpired("gh", 60), FileNotFoundError("gh")):
            with self.subTest(code=code), self.assertRaises(GitHubUnreadable):
                self.reader(**{RECENT: (code, "")}).recent_pulls(REPO)

    def test_push_permission(self):
        for permission, role, expected in (
            ("admin", "admin", True),
            ("write", "write", True),
            ("write", "maintain", True),
            ("read", "triage", False),
            ("read", "read", False),
            ("none", None, False),
        ):
            answer = {"permission": permission, "role_name": role}
            with self.subTest(permission=permission, role=role):
                reader = self.reader(**{PERMISSION: (0, answer)})
                self.assertIs(reader.can_push(REPO, "rnavarrete-factory-bot"), expected)

    def test_permission_answer_without_permission_is_unreadable(self):
        with self.assertRaises(GitHubUnreadable):
            self.reader(**{PERMISSION: (0, {})}).can_push(REPO, "rnavarrete-factory-bot")

    def test_refuses_odd_repo_and_login_names(self):
        reader = self.reader()
        for repo in ("../etc", "owner", "a/b/c", "a b/c", "-X/y?"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                reader.recent_pulls(repo)
        with self.assertRaises(ValueError):
            reader.can_push(REPO, "bot/../x")
        self.assertEqual(self.gh.calls, [])


if __name__ == "__main__":
    unittest.main()
