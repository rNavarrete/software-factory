"""ENG-156 on the host: the HTTPS GitHub reader the reviewer uses, and its wiring."""

import argparse
import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from controller.loop.collect import GitHubUnreadable, NotFound
from controller.service.github_http import HttpGitHubApi
from controller.service.main import _reviewer

TOKEN = "ghp_" + "x" * 36


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def redirect(url, code=302):
    headers = Message()
    headers["Location"] = url
    return urllib.error.HTTPError("u", code, "Found", headers, io.BytesIO(b""))


class Opener:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return Resp(a)


class HttpGitHubApiTests(unittest.TestCase):
    def api(self, *answers):
        self.opener = Opener(*answers)
        return HttpGitHubApi(lambda: TOKEN, opener=self.opener)

    def test_json_and_raw_send_the_token_to_github_only(self):
        api = self.api(b'{"a": 1}', b"file text")
        self.assertEqual(api.json("repos/o/r/pulls/7"), {"a": 1})
        self.assertEqual(api.raw("repos/o/r/contents/x?ref=abc"), b"file text")
        first, second = self.opener.requests
        self.assertEqual(first.full_url, "https://api.github.com/repos/o/r/pulls/7")
        self.assertEqual(first.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(second.get_header("Accept"), "application/vnd.github.raw")
        self.assertEqual(first.get_method(), "GET")

    def test_paths_outside_repos_are_refused(self):
        api = self.api()
        for path in ("user", "repos/o/r/../../user", "https://evil/x", "repos/o"):
            with self.assertRaises(GitHubUnreadable, msg=path):
                api.json(path)
        self.assertEqual(self.opener.requests, [])

    def test_404_is_not_found(self):
        err = urllib.error.HTTPError("u", 404, "nf", Message(), io.BytesIO(b"{}"))
        with self.assertRaises(NotFound):
            self.api(err).json("repos/o/r/pulls/9")

    def test_artifact_download_follows_github_storage_without_the_token(self):
        url = "https://productionresultssa1.blob.core.windows.net/a.zip?sig=s"
        api = self.api(redirect(url), b"PK zip")
        self.assertEqual(api.raw("repos/o/r/actions/artifacts/5/zip"), b"PK zip")
        follow = self.opener.requests[1]
        self.assertEqual(follow.full_url, url)
        self.assertIsNone(follow.get_header("Authorization"))

    def test_redirects_elsewhere_are_refused(self):
        for url in ("https://evil.example/a.zip", "http://x.blob.core.windows.net/a"):
            with self.assertRaises(GitHubUnreadable):
                self.api(redirect(url)).raw("repos/o/r/actions/artifacts/5/zip")
        # Only artifact downloads follow a redirect at all.
        url = "https://x.blob.core.windows.net/a"
        with self.assertRaises(GitHubUnreadable):
            self.api(redirect(url)).json("repos/o/r/pulls/7")

    def test_errors_never_carry_the_token(self):
        body = io.BytesIO(('{"message": "bad ' + TOKEN + '"}').encode())
        err = urllib.error.HTTPError("u", 500, "x", Message(), body)
        with self.assertRaises(GitHubUnreadable) as cm:
            self.api(err).json("repos/o/r/pulls/7")
        self.assertNotIn(TOKEN, str(cm.exception))


class Secrets:
    def get(self, name):
        return {"github-token": TOKEN, "reviewer-token": "sk-ant-oat01-x"}[name]


class WiringTests(unittest.TestCase):
    def args(self, **kw):
        return argparse.Namespace(**{"reviewer_routine": None, "reviewer_login": None, **kw})

    def test_without_a_reviewer_routine_the_stand_in_is_used(self):
        self.assertIsNone(_reviewer(self.args(), Secrets(), None, Path("/nonexistent")))

    def test_routine_and_login_go_together(self):
        with self.assertRaises(SystemExit):
            _reviewer(self.args(reviewer_login="factory-verifier"), Secrets(), None, Path("/x"))

    def test_the_worker_or_rolando_can_never_be_the_reviewer(self):
        for login in ("rnavarrete-factory-bot", "rNavarrete"):
            with self.assertRaises(ValueError):
                _reviewer(
                    self.args(reviewer_routine="trig_01ABC", reviewer_login=login),
                    Secrets(),
                    None,
                    Path("/x"),
                )

    def test_a_configured_reviewer_is_the_automatic_one(self):
        from controller.review.reviewer import AutoReviewer

        with tempfile.TemporaryDirectory() as d:
            r = _reviewer(
                self.args(reviewer_routine="trig_01ABC", reviewer_login="factory-verifier"),
                Secrets(),
                None,
                Path(d),
            )
        self.assertIsInstance(r, AutoReviewer)


if __name__ == "__main__":
    unittest.main()
