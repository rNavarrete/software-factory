"""A fake GitHub for the loop tests: the honest filter-by-status task's PR.

``World`` answers the REST paths ``controller.loop.collect`` reads, built from the
honest scenario in redteam/fixtures.py. Tests change one thing in it (a field
of the PR, a run, an artifact, a file) and check what the collector and the
loop make of it. Offline: nothing here touches the network.
"""

from __future__ import annotations

import copy
import io
import json
import zipfile
from urllib.parse import unquote, urlsplit

from controller.interfaces import AttemptId, TaskId
from controller.loop.collect import GitHubUnreadable, NotFound
from redteam import fixtures as fx
from verify.review import review_block

REPO = fx.REPO
NUMBER = 7
ATTEMPT = AttemptId(TaskId("filter-by-status"), 1)
RUN_ID = 101
SUITE_ID = 5001
PR_URL = f"https://github.com/{REPO}/pull/{NUMBER}"
WORKER = fx.WORKER

SRC_BASE = "export function addBook() {}\n"
SRC_HEAD = SRC_BASE + "export function filterByStatus() {}\n"
MAIN_BASE = "render();\n"
MAIN_HEAD = "render(); // with a status filter\n"


def evidence(commit: str = fx.HEAD, digest: str | None = None, **changes) -> dict:
    steps = [
        {"name": n, "status": "pass", "exitCode": 0, "logTail": f"{n} ok"}
        for n in ("environment", "format", "lint", "typecheck", "test", "build")
    ]
    e = {
        "schemaVersion": 1,
        "command": "npm run check",
        "candidate": {"commit": commit, "treeClean": True, "branch": None},
        "contractDigest": str(fx.DIGEST) if digest is None else digest,
        "steps": steps,
        "outcome": "pass",
    }
    e.update(changes)
    return e


def control_change(head: str = fx.HEAD, base: str = fx.MAIN, **changes) -> dict:
    r = {"base": base, "head": head, "baseCommit": base, "headCommit": head}
    r.update({"files": [], "flagged": False, "reasons": []})
    r.update(changes)
    return r


def zipped(name: str, data: object) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}.json", json.dumps(data) if not isinstance(data, bytes) else data)
    return buf.getvalue()


def honest_review(**changes) -> str:
    links = [
        {
            "criterion": cid,
            "path": fx.TEST_FILE,
            "test": test,
            "assertion": assertion,
            "why": f"Checks {cid}'s statement directly on filterByStatus.",
        }
        for cid, test, assertion in (
            ("ac1", fx.AC1_TEST, fx.AC1_ASSERT),
            ("ac2", fx.AC2_TEST, fx.AC2_ASSERT),
        )
    ]
    proofs = [
        {
            "criterion": cid,
            "path": fx.TEST_FILE,
            "test": test,
            "outcome": "failed-error",
            "output_excerpt": "TypeError: filterByStatus is not a function",
        }
        for cid, test in (("ac1", fx.AC1_TEST), ("ac2", fx.AC2_TEST))
    ]
    args = dict(links=links, proofs=proofs, limits=[])
    args.update(changes)
    return "Mapping for review.\n\n" + review_block(str(fx.DIGEST), fx.candidate(), **args)


class World:
    """REST answers keyed by path. ``calls`` records every path read."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()
        """Path prefixes that fail as unreadable."""
        self.pr = {
            "number": NUMBER,
            "html_url": PR_URL,
            "user": {"login": WORKER},
            "title": f"[filter-by-status a1 {fx.DIGEST.short}] filter-by-status",
            "body": f"Adds a status filter.\n\n{fx.DIGEST.pr_body_line}\n",
            "head": {
                "sha": fx.HEAD,
                "ref": "claude/filter-by-status-a1",
                "repo": {"full_name": REPO},
            },
            "base": {"sha": fx.MAIN, "ref": "main", "repo": {"full_name": REPO}},
            "state": "open",
            "draft": True,
            "changed_files": 3,
        }
        self.merge_base = fx.BASE
        self.files = [
            {"filename": "src/books.ts", "status": "modified"},
            {"filename": "src/main.ts", "status": "modified"},
            {"filename": fx.TEST_FILE, "status": "modified"},
        ]
        self.contents = {
            ("src/books.ts", fx.HEAD): SRC_HEAD.encode(),
            ("src/books.ts", fx.BASE): SRC_BASE.encode(),
            ("src/main.ts", fx.HEAD): MAIN_HEAD.encode(),
            ("src/main.ts", fx.BASE): MAIN_BASE.encode(),
            (fx.TEST_FILE, fx.HEAD): fx.HEAD_TEST.encode(),
            (fx.TEST_FILE, fx.BASE): fx.BASE_TEST.encode(),
        }
        self.runs = [
            {
                "id": RUN_ID,
                "html_url": fx.CI_URL,
                "head_sha": fx.HEAD,
                "path": fx.CI_WORKFLOW,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-10-08T14:00:00Z",
                "repository": {"full_name": REPO},
                "head_repository": {"full_name": REPO},
                "pull_requests": [{"number": NUMBER, "base": {"sha": fx.MAIN}}],
                "check_suite_id": SUITE_ID,
            }
        ]
        self.suites = {SUITE_ID: {"app": {"slug": "github-actions"}}}
        self.jobs = {
            RUN_ID: [
                {"name": "check", "status": "completed", "conclusion": "success"},
                {"name": "verified", "status": "completed", "conclusion": "success"},
            ]
        }
        self.artifacts = {
            RUN_ID: [
                {"id": 1, "name": f"check-evidence-{fx.HEAD}", "expired": False},
                {"id": 2, "name": f"control-change-{fx.HEAD}", "expired": False},
            ]
        }
        self.blobs = {
            1: zipped("check-evidence", evidence()),
            2: zipped("control-change", control_change()),
        }
        self.comments = [
            {
                "id": 900,
                "user": {"login": "rNavarrete"},
                "html_url": f"{PR_URL}#issuecomment-900",
                "body": honest_review(),
                "updated_at": "2026-10-08T15:00:00Z",
            }
        ]

    def copy(self) -> World:
        return copy.deepcopy(self)

    # --- GitHubApi ---

    def json(self, path: str):
        self._seen(path)
        p = urlsplit(path).path
        q = dict(part.split("=", 1) for part in urlsplit(path).query.split("&") if "=" in part)
        base = f"repos/{REPO}/"
        if not p.startswith(base):
            raise NotFound(path)
        rest = p[len(base) :]
        page = int(q.get("page", "1"))
        if rest == f"pulls/{NUMBER}":
            return copy.deepcopy(self.pr)
        if rest == f"pulls/{NUMBER}/files":
            return copy.deepcopy(self.files[(page - 1) * 100 : page * 100])
        if rest.startswith("compare/"):
            return {"merge_base_commit": {"sha": self.merge_base}}
        if rest == f"issues/{NUMBER}/comments":
            return copy.deepcopy(self.comments[(page - 1) * 100 : page * 100])
        if rest == "actions/workflows/ci.yml/runs":
            runs = [r for r in self.runs if r.get("head_sha") == q.get("head_sha")]
            for r in runs:
                r.setdefault("workflow_id", 1)
            return {"workflow_runs": copy.deepcopy(runs)}
        if rest.startswith("check-suites/"):
            suite = self.suites.get(int(rest.split("/")[1]))
            if suite is None:
                raise NotFound(path)
            return copy.deepcopy(suite)
        if rest.startswith("actions/runs/") and rest.endswith("/jobs"):
            return {"jobs": copy.deepcopy(self.jobs.get(int(rest.split("/")[2]), []))}
        if rest.startswith("actions/runs/") and rest.endswith("/artifacts"):
            run_id = int(rest.split("/")[2])
            arts = copy.deepcopy(self.artifacts.get(run_id, []))
            for a in arts:
                a.setdefault("workflow_run", {"id": run_id})
                a.setdefault("created_at", "2026-10-08T14:01:00Z")
            return {"artifacts": arts}
        raise NotFound(path)

    def raw(self, path: str) -> bytes:
        self._seen(path)
        p = urlsplit(path).path
        q = dict(part.split("=", 1) for part in urlsplit(path).query.split("&") if "=" in part)
        base = f"repos/{REPO}/"
        rest = p[len(base) :]
        if rest.startswith("contents/"):
            key = (unquote(rest[len("contents/") :]), q.get("ref"))
            if key not in self.contents:
                raise NotFound(path)
            return self.contents[key]
        if rest.startswith("actions/artifacts/") and rest.endswith("/zip"):
            blob = self.blobs.get(int(rest.split("/")[2]))
            if blob is None:
                raise NotFound(path)
            return blob
        raise NotFound(path)

    def _seen(self, path: str) -> None:
        self.calls.append(path)
        for prefix in self.fail:
            if path.startswith(prefix):
                raise GitHubUnreadable(f"{path}: HTTP 502")
