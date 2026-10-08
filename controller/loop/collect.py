"""Collect the evidence for one worker PR from GitHub, read-only (ENG-145).

``verify.criteria`` and ``verify.assertions`` only read what they are given and
do no I/O (tests/test_verify_criteria.py holds them to that), so the reading
lives here, in the controller.
This module is what gives it to them: for one pull request in the pilot repo
it reads, through ``gh api`` under Rolando's own login,

- the PR itself: where it merges (repository and branch), where its head
  lives, its title, body, author and state;
- where the candidate's branch starts (the merge base), from GitHub's compare;
- every changed path (both names of a rename), and the text of every one of
  those files at the candidate commit and at the merge base, not just the
  tests, so a changed import, a new hook or a suppression comment anywhere is
  seen;
- the newest run of the trusted CI workflow for this exact head commit, which
  app posted it, the conclusion of its ``verified`` job, and the
  ``check-evidence`` and ``control-change`` artifacts that run uploaded.

Everything read here is untrusted. Nothing in a title, body, artifact or file
is ever read as an instruction or a decision; the collector only compares it
with what the approved contract and the controller expect, and labels each
check result with the commit and base GitHub's record of the run gives, never
what a file says about itself.

``Collected.problems`` are reasons the PR cannot be judged at all (it merges
somewhere else, comes from a fork, a file could not be read, CI posted no
evidence). ``Collected.pending`` means CI has not finished for this head yet,
so nothing is judged. Either way the PR is not ready for review; the
verifiers' own gates come on top.

Standard library only. Tests use any object with the ``GitHubApi`` methods.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from controller.adapter import routine
from controller.interfaces import AttemptId, ContractDigest
from verify.assertions import ControlChangeReport, TestSource
from verify.criteria import (
    DEFAULT_POLICY,
    Candidate,
    CheckResult,
    Source,
    TrustPolicy,
    WriterClaim,
    results_from_check_evidence,
)
from verify.review import Comment

PILOT_REPO = "rNavarrete/factory-pilot-demo"
BASE_BRANCH = "main"
VERIFIED_JOB = "verified"
"""The one job the pilot's main ruleset requires; it passes only when every
check job reported success."""
GH_TIMEOUT_SECONDS = 60
MAX_PAGES = 30
"""GitHub lists at most 3000 files for a PR (30 pages of 100)."""
MAX_FILE_BYTES = 2_000_000
MAX_ARTIFACT_BYTES = 5_000_000

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}(?:\[bot\])?$", re.ASCII)
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class GitHubUnreadable(Exception):
    """GitHub could not be read, or answered with something unexpected."""


class NotFound(GitHubUnreadable):
    """GitHub answered 404: the thing asked for does not exist (or is hidden)."""


class GitHubApi(Protocol):
    def json(self, path: str) -> Any:
        """GET ``path`` from the REST API and parse it. Raises NotFound on 404."""
        ...

    def raw(self, path: str) -> bytes:
        """GET ``path`` as raw bytes (file contents, artifact zips). NotFound on 404."""
        ...


class GhApi:
    """``GitHubApi`` over the ``gh`` command, as Rolando's login. Read-only: GET only."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        gh: str = "gh",
    ) -> None:
        self._run = run
        self._gh = gh

    def json(self, path: str) -> Any:
        out = self._get(path, "application/vnd.github+json")
        try:
            return json.loads(out)
        except ValueError as e:
            raise GitHubUnreadable(f"gh api {path} returned unreadable JSON") from e

    def raw(self, path: str) -> bytes:
        return self._get(path, "application/vnd.github.raw")

    def _get(self, path: str, accept: str) -> bytes:
        try:
            result = self._run(
                [self._gh, "api", "--method", "GET", "-H", f"Accept: {accept}", path],
                capture_output=True,
                check=False,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise GitHubUnreadable(f"gh api {path} did not run: {e}") from e
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            if "HTTP 404" in err:
                raise NotFound(f"gh api {path}: not found")
            raise GitHubUnreadable(f"gh api {path} failed: {err.strip()[:500]}")
        return result.stdout


@dataclass(frozen=True)
class CiRun:
    """The trusted CI run the evidence came from, as GitHub records it."""

    id: int
    url: str
    status: str
    conclusion: str | None
    head_sha: str
    base_sha: str | None
    """The PR base the run was for (its ``pull_requests`` entry), if GitHub says."""
    repository: str
    workflow_path: str
    app: str
    verified: str | None
    """The ``verified`` job's conclusion in this run; None if it has none yet."""


@dataclass(frozen=True)
class Collected:
    """What the collector read for one PR. Inputs for the two verifiers."""

    pr_number: int
    pr_url: str
    author: str
    draft: bool
    candidate: Candidate | None
    """None when the PR could not be read well enough to describe a candidate."""
    results: tuple[CheckResult, ...] = ()
    control_change: ControlChangeReport | None = None
    sources: tuple[TestSource, ...] = ()
    claims: tuple[WriterClaim, ...] = ()
    ci: CiRun | None = None
    comments: tuple[Comment, ...] = ()
    """The PR's conversation comments, where the independent mapper posts its review."""
    problems: tuple[str, ...] = ()
    """Why this PR cannot be judged, each a full sentence. Empty when collection held."""
    pending: bool = False
    """CI for this exact head has not finished; check again later."""

    @property
    def usable(self) -> bool:
        return self.candidate is not None and not self.problems and not self.pending


def collect(
    api: GitHubApi,
    contract: Mapping[str, object],
    approved: ContractDigest,
    attempt: AttemptId,
    pr_number: int,
    *,
    repo: str = PILOT_REPO,
    base_branch: str = BASE_BRANCH,
    policy: TrustPolicy = DEFAULT_POLICY,
) -> Collected:
    """Read PR ``pr_number`` in ``repo`` and everything the verifiers need about it.

    ``approved`` is the digest from Rolando's approval record, never the PR's
    own claim. Raises GitHubUnreadable only when the PR itself can't be read;
    any later read that fails becomes a problem, so a half-read PR is never
    judged.
    """
    if not _REPO_RE.fullmatch(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise ValueError(f"not a PR number: {pr_number!r}")
    pr = api.json(f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pr, Mapping):
        raise GitHubUnreadable(f"PR #{pr_number} answer is not an object")

    url = _get(pr, "html_url")
    author = _get(pr, "user", "login")
    title, body = _get(pr, "title"), _get(pr, "body")
    head_sha, head_ref = _get(pr, "head", "sha"), _get(pr, "head", "ref")
    head_repo = _get(pr, "head", "repo", "full_name")
    base_sha, base_ref = _get(pr, "base", "sha"), _get(pr, "base", "ref")
    base_repo = _get(pr, "base", "repo", "full_name")
    state, draft = _get(pr, "state"), _get(pr, "draft")
    changed_count = _get(pr, "changed_files")
    shell = dict(
        pr_number=pr_number,
        pr_url=url if isinstance(url, str) else f"https://github.com/{repo}/pull/{pr_number}",
        author=author if isinstance(author, str) else "",
        draft=draft is True,
    )
    malformed = [
        name
        for name, ok in (
            ("html_url", isinstance(url, str) and url.startswith("https://")),
            ("user.login", isinstance(author, str)),
            ("title", isinstance(title, str)),
            ("body", body is None or isinstance(body, str)),
            ("head.sha", isinstance(head_sha, str) and _SHA_RE.fullmatch(head_sha) is not None),
            ("head.ref", isinstance(head_ref, str)),
            ("base.sha", isinstance(base_sha, str) and _SHA_RE.fullmatch(base_sha) is not None),
            ("base.ref", isinstance(base_ref, str)),
            ("base.repo.full_name", isinstance(base_repo, str)),
            ("state", state in ("open", "closed")),
            ("draft", isinstance(draft, bool)),
            ("changed_files", _count(changed_count)),
        )
        if not ok
    ]
    if malformed:
        return Collected(
            **shell,
            candidate=None,
            problems=(f"GitHub's answer for PR #{pr_number} is missing {', '.join(malformed)}.",),
        )
    body = body or ""

    problems: list[str] = []
    # Where the PR merges and where its head lives. Only work proposed to the
    # pilot's protected main, from a branch in the pilot repo itself, counts.
    if not _same_repo(base_repo, repo):
        problems.append(f"PR #{pr_number} merges into {base_repo}, not {repo}.")
    if base_ref != base_branch:
        problems.append(f"PR #{pr_number} merges into branch {base_ref!r}, not {base_branch!r}.")
    if not isinstance(head_repo, str) or not _same_repo(head_repo, repo):
        problems.append(
            f"PR #{pr_number}'s branch lives in {head_repo or 'a deleted repository'}, not {repo}."
        )
    if head_ref != attempt.branch:
        problems.append(f"PR #{pr_number}'s branch is {head_ref!r}, not {attempt.branch!r}.")
    want_title = routine.pr_title(attempt, approved)
    if title != want_title:
        problems.append(f"PR #{pr_number}'s title is {title!r}, not exactly {want_title!r}.")
    if ContractDigest.from_pr_body(body) != approved:
        problems.append(
            f"PR #{pr_number}'s body has no single Contract-Digest line naming the approved"
            f" contract {approved.short}."
        )
    if state != "open":
        problems.append(f"PR #{pr_number} is closed.")
    # is_worker deliberately says yes to a malformed login (the refusing
    # direction for evidence); here a yes accepts the PR, so check the form too.
    if not (_LOGIN_RE.fullmatch(author) and policy.is_worker(author)):
        problems.append(
            f"PR #{pr_number} was opened by {author!r}, not the worker account, so it is not"
            " this attempt's output."
        )

    merge_base, paths = None, ()
    try:
        merge_base = _merge_base(api, repo, base_sha, head_sha)
        paths = _changed_paths(api, repo, pr_number, changed_count)
    except GitHubUnreadable as e:
        problems.append(f"Could not read what PR #{pr_number} changes: {e}.")
    if merge_base is None:
        return Collected(**shell, candidate=None, problems=tuple(problems))

    candidate = Candidate(
        repository=base_repo,
        head_commit=head_sha,
        base_commit=base_sha,
        merge_base=merge_base,
        branch=head_ref,
        pr_title=title,
        pr_body=body,
        changed_paths=paths,
    )
    sources: list[TestSource] = []
    for path in paths:
        for commit in (head_sha, merge_base):
            try:
                sources.append(TestSource(path, commit, _file_text(api, repo, path, commit)))
            except GitHubUnreadable as e:
                problems.append(f"Could not read {path} at {commit[:12]}: {e}.")

    comments: tuple[Comment, ...] = ()
    try:
        comments = _comments(api, repo, pr_number)
    except GitHubUnreadable as e:
        problems.append(f"Could not read PR #{pr_number}'s comments: {e}.")

    ci, results, control, pending = None, (), None, False
    try:
        ci = _ci_run(api, repo, pr_number, head_sha, policy)
    except GitHubUnreadable as e:
        problems.append(f"Could not read CI for {head_sha[:12]}: {e}.")
    else:
        # No trusted run for this head yet, or it hasn't finished: judge nothing.
        pending = ci is None or ci.status != "completed" or ci.verified is None
    if ci is not None and not pending:
        try:
            results, control, ci_problems = _ci_evidence(api, repo, ci, head_sha, approved)
        except GitHubUnreadable as e:
            ci_problems = [f"Could not read the CI run's artifacts: {e}."]
        problems += ci_problems
        if ci.verified != "success":
            problems.append(
                f"The trusted CI run ({ci.url}) did not pass: its {VERIFIED_JOB} job"
                f" concluded {ci.verified}."
            )

    return Collected(
        **shell,
        candidate=candidate,
        results=results,
        control_change=control,
        sources=tuple(sources),
        claims=(WriterClaim(text=body),) if body.strip() else (),
        ci=ci,
        comments=comments,
        problems=tuple(problems),
        pending=pending,
    )


# --- Reads ------------------------------------------------------------------------


def _merge_base(api: GitHubApi, repo: str, base: str, head: str) -> str:
    answer = api.json(f"repos/{repo}/compare/{base}...{head}?per_page=1")
    sha = _get(answer, "merge_base_commit", "sha")
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise GitHubUnreadable("compare gave no merge base")
    return sha


def _changed_paths(api: GitHubApi, repo: str, number: int, expected: int) -> tuple[str, ...]:
    """Every path the PR touches, both names of a rename, in GitHub's order."""
    files: list[Mapping[str, object]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = api.json(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(batch, list) or not all(isinstance(f, Mapping) for f in batch):
            raise GitHubUnreadable("the files answer is not a list of files")
        files += batch
        if len(batch) < 100:
            break
    if len(files) != expected:
        # GitHub stops listing at 3000 files; a list shorter than the PR says
        # is not every changed file, so nothing about it can be trusted.
        raise GitHubUnreadable(f"GitHub listed {len(files)} of the PR's {expected} changed files")
    out: list[str] = []
    for f in files:
        names = [f.get("filename")]
        if f.get("previous_filename") is not None:
            names.append(f.get("previous_filename"))
        for name in names:
            if not isinstance(name, str) or not name or name.startswith("/") or "\0" in name:
                raise GitHubUnreadable(f"a changed file has no usable name: {name!r}")
            if name not in out:
                out.append(name)
    return tuple(out)


def _file_text(api: GitHubApi, repo: str, path: str, commit: str) -> str | None:
    """The file's text at ``commit``; None when it does not exist there."""
    try:
        data = api.raw(f"repos/{repo}/contents/{quote(path)}?ref={commit}")
    except NotFound:
        return None
    if len(data) > MAX_FILE_BYTES:
        raise GitHubUnreadable(f"{len(data)} bytes is over the {MAX_FILE_BYTES}-byte limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise GitHubUnreadable("not UTF-8 text, so it cannot be checked") from e


def _comments(api: GitHubApi, repo: str, number: int) -> tuple[Comment, ...]:
    out: list[Comment] = []
    for page in range(1, MAX_PAGES + 1):
        batch = api.json(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise GitHubUnreadable("the comments answer is not a list")
        for c in batch:
            cid, author = _get(c, "id"), _get(c, "user", "login")
            url, body, updated = _get(c, "html_url"), _get(c, "body"), _get(c, "updated_at")
            if not (
                isinstance(cid, int)
                and isinstance(author, str)
                and isinstance(url, str)
                and url.startswith("https://")
                and isinstance(body, str)
                and isinstance(updated, str)
            ):
                raise GitHubUnreadable("a comment in the answer is malformed")
            out.append(Comment(cid, author, url, body, updated))
        if len(batch) < 100:
            break
    return tuple(out)


def _ci_run(api: GitHubApi, repo: str, number: int, head: str, policy: TrustPolicy) -> CiRun | None:
    """The newest run of the trusted workflow for exactly ``head``, or None.

    Runs are listed by the workflow file, then each is checked against what
    GitHub records about it: the exact workflow path, the repository it ran
    in and the head's repository, the head commit, and the app behind its
    check suite. A run that fails any of these is not the trusted run."""
    workflow = policy.workflow_path.rsplit("/", 1)[-1]
    answer = api.json(
        f"repos/{repo}/actions/workflows/{quote(workflow)}/runs"
        f"?head_sha={head}&event=pull_request&per_page=100"
    )
    runs = _get(answer, "workflow_runs")
    if not isinstance(runs, list):
        raise GitHubUnreadable("the workflow runs answer has no list of runs")
    trusted = [
        r
        for r in runs
        if isinstance(r, Mapping)
        and r.get("head_sha") == head
        and r.get("path") == policy.workflow_path
        and r.get("event") == "pull_request"
        and _same_repo(_get(r, "repository", "full_name"), repo)
        and _same_repo(_get(r, "head_repository", "full_name"), repo)
        and isinstance(r.get("id"), int)
    ]
    if not trusted:
        return None
    run = max(trusted, key=lambda r: (str(r.get("created_at") or ""), r["id"]))
    run_id = run["id"]
    url = run.get("html_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise GitHubUnreadable(f"run {run_id} has no link")
    base_sha = None
    for p in run.get("pull_requests") or ():
        if isinstance(p, Mapping) and p.get("number") == number:
            sha = _get(p, "base", "sha")
            base_sha = sha if isinstance(sha, str) and _SHA_RE.fullmatch(sha) else None
    suite = run.get("check_suite_id")
    app = ""
    if isinstance(suite, int) and not isinstance(suite, bool):
        slug = _get(api.json(f"repos/{repo}/check-suites/{suite}"), "app", "slug")
        app = slug if isinstance(slug, str) else ""
    jobs = _get(api.json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"), "jobs")
    if not isinstance(jobs, list):
        raise GitHubUnreadable(f"run {run_id}'s jobs answer has no list of jobs")
    named = [j for j in jobs if isinstance(j, Mapping) and j.get("name") == VERIFIED_JOB]
    verified = None
    if len(named) == 1 and named[0].get("status") == "completed":
        c = named[0].get("conclusion")
        verified = c if isinstance(c, str) else "unknown"
    elif len(named) > 1:
        verified = "ambiguous (more than one job with that name)"
    elif not named and run.get("status") == "completed":
        verified = "missing"
    return CiRun(
        id=run_id,
        url=url,
        status=str(run.get("status")),
        conclusion=run.get("conclusion") if isinstance(run.get("conclusion"), str) else None,
        head_sha=head,
        base_sha=base_sha,
        repository=str(_get(run, "repository", "full_name")),
        workflow_path=str(run.get("path")),
        app=app,
        verified=verified,
    )


def _ci_evidence(
    api: GitHubApi, repo: str, ci: CiRun, head: str, approved: ContractDigest
) -> tuple[tuple[CheckResult, ...], ControlChangeReport | None, list[str]]:
    problems: list[str] = []
    answer = api.json(f"repos/{repo}/actions/runs/{ci.id}/artifacts?per_page=100")
    artifacts = _get(answer, "artifacts")
    if not isinstance(artifacts, list):
        raise GitHubUnreadable(f"run {ci.id}'s artifacts answer has no list")
    if ci.base_sha is None:
        problems.append(
            f"GitHub does not say which base the CI run ({ci.url}) checked against,"
            " so its results can't be matched to this PR."
        )
        return (), None, problems

    results: tuple[CheckResult, ...] = ()
    evidence = _artifact_json(api, repo, artifacts, ci, f"check-evidence-{head}", "check-evidence")
    if isinstance(evidence, str):
        problems.append(evidence)
    else:
        claimed = evidence.get("contractDigest")
        if claimed != approved.value:
            problems.append(
                f"The CI evidence names contract {str(claimed)[:12]!r}, not the approved"
                f" {approved.short}."
            )
        errors = evidence.get("contractClaimErrors")
        if errors:
            problems.append(f"CI found the PR's contract claim malformed: {errors}.")
        try:
            results = results_from_check_evidence(
                evidence,
                base_commit=ci.base_sha,
                source=Source.CI,
                url=ci.url,
                repository=ci.repository,
                workflow_path=ci.workflow_path,
                app=ci.app,
            )
        except ValueError as e:
            problems.append(f"The CI evidence could not be read: {e}.")

    control = None
    report = _artifact_json(api, repo, artifacts, ci, f"control-change-{head}", "control-change")
    if isinstance(report, str):
        problems.append(report)
    else:
        try:
            control = ControlChangeReport.from_json(
                report,
                url=ci.url,
                repository=ci.repository,
                workflow_path=ci.workflow_path,
                app=ci.app,
            )
        except (TypeError, ValueError) as e:
            problems.append(f"The control-change report could not be read: {e}.")
    return results, control, problems


def _artifact_json(
    api: GitHubApi, repo: str, artifacts: list, ci: CiRun, name: str, member: str
) -> Mapping[str, object] | str:
    """The JSON file ``<member>.json`` from the run's artifact ``name``, or why not."""
    found = [
        a
        for a in artifacts
        if isinstance(a, Mapping)
        and a.get("name") == name
        and a.get("expired") is not True
        and isinstance(a.get("id"), int)
        and _get(a, "workflow_run", "id") == ci.id
    ]
    if not found:
        return f"The CI run ({ci.url}) has no {name} artifact."
    newest = max(found, key=lambda a: (str(a.get("created_at") or ""), a["id"]))
    try:
        blob = api.raw(f"repos/{repo}/actions/artifacts/{newest['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            info = z.getinfo(f"{member}.json")
            if info.file_size > MAX_ARTIFACT_BYTES:
                return f"{name} in {ci.url} is too large to read."
            data = json.loads(z.read(info).decode("utf-8"))
    except (
        GitHubUnreadable,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        UnicodeDecodeError,
        ValueError,
        NotImplementedError,  # a compression method zipfile can't read
        RuntimeError,  # an encrypted member
        OSError,
        EOFError,
        zlib.error,
    ) as e:
        return f"Could not read {member}.json from {name} in {ci.url}: {e}."
    if not isinstance(data, Mapping):
        return f"{member}.json in {ci.url} is not a JSON object."
    return data


# --- Helpers ----------------------------------------------------------------------


def _get(value: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _same_repo(a: object, b: str) -> bool:
    """GitHub owner and repository names are case-insensitive."""
    return isinstance(a, str) and a.lower() == b.lower()
