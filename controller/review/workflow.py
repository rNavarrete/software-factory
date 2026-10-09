"""The Codex review job, run as a protected GitHub Actions workflow (ENG-156).

The review of one exact revision runs as one ``workflow_dispatch`` run of
``.github/workflows/codex-review.yml`` in the factory repository. The worker
has no access to that repository, so it can't edit the workflow, its prompt,
its output schema or this code, and it can't start a run. Inside the run:

- ``review``: checks the request, checks out the pilot PR at the exact head,
  removes agent instruction files (``AGENTS*.md``, ``.codex/``) from the
  working tree, and runs the official Codex GitHub Action (pinned) read-only,
  with the OpenAI API key held by the action's proxy and sudo dropped. No
  pilot script, install hook or test is run in this job; Codex reads the
  pilot's files, which are data to it, not instructions it must follow.
- ``proofs``: runs the tests the review linked, from the PR's head, against the
  base's product code (the failure proofs). No secrets, no token permissions;
  the pilot's code runs as a separate user that can't use sudo or write the
  job's outputs. The outcomes are what the worker's own test code reported
  in-process, so the worker's code can influence them; a test that wasn't run
  or whose result can't be read makes the whole result incomplete.
- ``publish``: wraps both outputs, with the run's own identity, into one
  ``factory-review-result`` artifact. It runs no pilot code and gets no key.

Nothing here believes what a run says about itself. ``WorkflowResults`` takes
the run's identity from GitHub's own records of the run (repository, workflow
file, event, branch, who started it, the workflow's commit, which must be on
the factory's protected main) and only then reads its artifact, whose copies
of those values must agree. The request it answers must be exactly the one the
controller claimed in the ledger (same key, same request hash). Anything
missing, malformed, stale, mismatched or unauthenticated is never a pass:
it is ignored (a look-alike) or reported as incomplete.

``WorkflowDispatchRuntime`` starts a run. It makes one POST and never retries;
the answer is launched, not-launched or launch-outcome-unknown, as for every
other launch. A lost answer is reconciled by finding the run by its key, never
by launching again.

Standard library only.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from controller.adapter.routine import LaunchInterrupted, parse_retry_after
from controller.interfaces import LaunchOutcome, LaunchResult
from controller.loop.collect import GitHubApi, GitHubUnreadable, NotFound
from controller.review.runtime import ENVELOPE, ReviewTooLarge
from verify.assertions import AssertionLink, FailureProof, FailureProofLimit, ProofOutcome
from verify.criteria import _login_key
from verify.findings import Finding, from_reviewer
from verify.review import Review

FACTORY_REPO = "rNavarrete/software-factory"
WORKFLOW_PATH = ".github/workflows/codex-review.yml"
ARTIFACT = "factory-review-result"
RESULT_FILE = "result.json"
RESULT_SCHEMA = "factory-review-result/v1"
IDENTITY = "codex-review"
"""The name the review's links, proofs and findings carry. It is never read
from anything the run wrote: every result under it comes from a run that
passed ``WorkflowResults``'s checks."""
RUN_NAME = "codex-review {key}"
NOT_RUN = "not-run"
"""The proof job's outcome for a linked test it couldn't run or read."""
MAX_REQUEST_CHARS = 60_000
"""GitHub caps a dispatch's inputs at 65,535 characters in all."""
MAX_RESULT_BYTES = 4 * 1024 * 1024
CLOCK_SKEW = timedelta(minutes=5)
MAX_RUN_PAGES = 5

# The areas every review must say it examined (codex_prompt.md).
AREAS = (
    "behavior",
    "criteria-coverage",
    "regressions-and-edge-cases",
    "scope",
    "tests-and-controls",
    "security-and-secrets",
    "claims",
)
CATEGORIES = ("code", "test", "scope", "product", "security")

_KEY_RE = re.compile(r"^rv-[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EFFORT_RE = re.compile(r"^[a-z]{0,16}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/@+-]{1,300}$")
_TEST_PATH_RE = re.compile(r"^tests/[A-Za-z0-9._/-]+\.test\.(?:ts|tsx|js|mjs)$")
"""The only files a link may name, and so the only files the proof job copies
from the PR's head into the base's code: the pilot's test files."""


@dataclass(frozen=True)
class WorkflowConfig:
    """Where the trusted review workflow lives and who may start it."""

    dispatchers: frozenset[str]
    """The GitHub logins whose runs count: the owner of the factory's
    dispatch token (Rolando). Never the worker."""
    model: str
    """The OpenAI model the review uses. Set in the service's settings; a
    supported identifier from OpenAI's Codex model list."""
    effort: str = ""
    """Reasoning effort; empty for the model's default."""
    repository: str = FACTORY_REPO
    workflow: str = WORKFLOW_PATH
    ref: str = "main"
    identity: str = IDENTITY
    worker_logins: frozenset[str] = frozenset({"rnavarrete-factory-bot"})

    def __post_init__(self) -> None:
        if not self.dispatchers:
            raise ValueError("the review workflow needs at least one trusted dispatcher")
        workers = {_login_key(w) for w in self.worker_logins}
        for login in self.dispatchers:
            key = _login_key(login)
            if key is None or login.lower().endswith("[bot]") or key in workers:
                raise ValueError(f"dispatcher {login!r} can't start trusted reviews")
        if not _MODEL_RE.fullmatch(self.model):
            raise ValueError(f"model {self.model!r} is not a model identifier")
        if not _EFFORT_RE.fullmatch(self.effort):
            raise ValueError(f"effort {self.effort!r} is not an effort level")
        if not _REPO_RE.fullmatch(self.repository) or not self.workflow.startswith(
            ".github/workflows/"
        ):
            raise ValueError("the review workflow must be a workflow file in a repository")
        if _login_key(self.identity) is None or self.identity.lower() in workers:
            raise ValueError(f"identity {self.identity!r} can't name the review")

    @property
    def workflow_file(self) -> str:
        return self.workflow.rsplit("/", 1)[-1]

    def run_name(self, key: str) -> str:
        return RUN_NAME.format(key=key)


def request_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- starting a run ---------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class WorkflowDispatchRuntime:
    """A ``ReviewRuntime`` that starts one run of the trusted review workflow.

    The token needs Actions read and write on the factory repository only.
    One POST to the one dispatch URL; never a retry, never a redirect.
    """

    def __init__(
        self,
        config: WorkflowConfig,
        token: Callable[[], str],
        opener: Callable[..., object] | None = None,
        timeout: float = 30,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._token = token
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._timeout = timeout
        self._clock = clock or (lambda: datetime.now(UTC).timestamp())

    @property
    def url(self) -> str:
        c = self._config
        return (
            f"https://api.github.com/repos/{c.repository}/actions/workflows/"
            f"{c.workflow_file}/dispatches"
        )

    def launch(self, text: str) -> LaunchResult:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("envelope") != ENVELOPE:
            raise ValueError("not a review job envelope")
        key = data.get("key")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("the review job has no valid request key")
        if len(text) > MAX_REQUEST_CHARS:
            raise ReviewTooLarge(f"review job text is {len(text)} chars, limit {MAX_REQUEST_CHARS}")
        c = self._config
        body = {
            "ref": c.ref,
            "inputs": {"key": key, "request": text, "model": c.model, "effort": c.effort},
            "return_run_details": True,
        }
        token = self._token()
        if not token:
            raise LookupError("no review dispatch token is set")
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "software-factory-service",
            },
        )
        try:
            with self._open(req, timeout=self._timeout) as resp:  # type: ignore[attr-defined]
                status, raw = resp.status, resp.read(65536)
        except urllib.error.HTTPError as e:
            return self._refused(e, token)
        except BaseException as e:  # timeout, reset, Ctrl-C: the run may still have started
            result = LaunchResult(
                LaunchOutcome.OUTCOME_UNKNOWN,
                detail=_scrub(f"{type(e).__name__}: {e}", token),
            )
            if isinstance(e, KeyboardInterrupt):
                raise LaunchInterrupted(result) from e
            return result
        return self._accepted(status, raw, key)

    def _accepted(self, status: int, raw: bytes, key: str) -> LaunchResult:
        c = self._config
        runs = f"https://github.com/{c.repository}/actions/workflows/{c.workflow_file}"
        if status == 204:
            # Accepted without run details: the run is found later by its key.
            return LaunchResult(
                LaunchOutcome.LAUNCHED,
                http_status=200,
                session_id=f"dispatch:{key}",
                session_url=runs,
                detail="dispatch accepted (HTTP 204); the run is found by its key",
            )
        if status == 200:
            try:
                details = json.loads(raw)
                run_id = details["workflow_run_id"]
            except (ValueError, KeyError, TypeError):
                details, run_id = {}, None
            if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0:
                url = f"https://github.com/{c.repository}/actions/runs/{run_id}"
                return LaunchResult(
                    LaunchOutcome.LAUNCHED,
                    http_status=200,
                    session_id=str(run_id),
                    session_url=url,
                    detail=f"run {run_id}",
                )
            return LaunchResult(
                LaunchOutcome.LAUNCHED,
                http_status=200,
                session_id=f"dispatch:{key}",
                session_url=runs,
                detail="dispatch accepted; the run is found by its key",
            )
        return LaunchResult(LaunchOutcome.OUTCOME_UNKNOWN, http_status=status)

    def _refused(self, e: urllib.error.HTTPError, token: str) -> LaunchResult:
        try:
            body = _scrub(e.read(4096).decode("utf-8", "replace"), token)
        except Exception:
            body = None
        code = e.code
        headers = e.headers or {}
        limited = code == 429 or (
            code == 403
            and (
                headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in (body or "").lower()
            )
        )
        if limited:
            retry = parse_retry_after(headers.get("Retry-After"), self._clock())
            if retry is None and headers.get("X-RateLimit-Reset", "").isdigit():
                retry = max(0, int(headers["X-RateLimit-Reset"]) - int(self._clock()))
            return LaunchResult(
                LaunchOutcome.NOT_LAUNCHED,
                http_status=429,
                retry_after_seconds=retry,
                response_body=body,
                detail=f"GitHub rate limit (HTTP {code})",
            )
        from controller.interfaces import classify

        reason = _github_message(body)
        return LaunchResult(
            classify(code, None),
            http_status=code,
            response_body=body,
            detail=f'HTTP {code}, GitHub said "{reason}"' if reason else f"HTTP {code}",
        )


def check_start_permission(
    token: str,
    opener: Callable[..., object] | None = None,
    repository: str = FACTORY_REPO,
    workflow: str = WORKFLOW_PATH,
    ref: str = "main",
    timeout: float = 30,
) -> str | None:
    """Whether ``token`` may start the review workflow, without starting it:
    None if it may, else the reason in plain words.

    The request leaves out the workflow's required inputs, so GitHub can
    never start a run from it. GitHub checks the token's permission first
    (refused: HTTP 401, 403 or 404) and only then the inputs (HTTP 422), so
    a 422 means the token could have started a real review."""
    if not token:
        return "no review token is set"
    name = workflow.rsplit("/", 1)[-1]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/{name}/dispatches",
        data=json.dumps({"ref": ref, "inputs": {}}).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "software-factory-service",
        },
    )
    open_ = opener or urllib.request.build_opener(_NoRedirect()).open
    try:
        with open_(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
            status = resp.status
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return None
        try:
            body = _scrub(e.read(4096).decode("utf-8", "replace"), token)
        except Exception:
            body = None
        reason = _github_message(body)
        said = f', GitHub said "{reason}"' if reason else ""
        if e.code in (401, 403, 404):
            return (
                f"GitHub refused it (HTTP {e.code}{said}). The token needs Actions: Read and"
                f" write on {repository}."
            )
        return f"GitHub answered HTTP {e.code}{said}; try again in a minute"
    except Exception as e:
        return _scrub(f"GitHub couldn't be reached ({type(e).__name__}: {e})", token)
    return f"GitHub answered HTTP {status} to a request it should have refused"


def _github_message(body: str | None) -> str:
    """GitHub's one-line reason for a refusal (its JSON ``message``), so the
    ticket note says why, e.g. a token without the needed permission."""
    try:
        message = json.loads(body or "").get("message")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(message, str):
        return ""
    return " ".join("".join(ch for ch in message if ch.isprintable()).split())[:200]


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "[redacted]") if token else text


# --- reading a run's result -------------------------------------------------------


class ResultState(Enum):
    ABSENT = "absent"
    """No authenticated run for this request yet."""
    RUNNING = "running"
    READY = "ready"
    """An authenticated, complete result: ``review`` holds it."""
    INCOMPLETE = "incomplete"
    """An authenticated run that gave no usable result. Never a pass."""


@dataclass(frozen=True)
class ResultRequest:
    """What the controller asked for, from its own ledger and the collector."""

    key: str
    repository: str
    pr: int
    digest: str
    head: str
    base: str
    merge_base: str
    claimed: Sequence[datetime]
    """When each launch for this key was claimed."""
    requests: frozenset[str]
    """sha256 of each request text dispatched for this key. Empty only for a
    claim recorded before request hashes were kept: then nothing counts."""
    criteria: Mapping[str, str]
    """Each acceptance criterion's id and evidence type, from the contract."""


@dataclass(frozen=True)
class Found:
    state: ResultState
    review: Review | None = None
    url: str | None = None
    reason: str = ""
    ignored: tuple[str, ...] = ()


@dataclass
class _Cache:
    on_main: dict[str, bool] = field(default_factory=dict)
    results: dict[tuple[int, int, int], Mapping] = field(default_factory=dict)


class WorkflowResults:
    """Finds and authenticates the review run for one request."""

    def __init__(self, api: GitHubApi, config: WorkflowConfig) -> None:
        self._api = api
        self._config = config
        self._cache = _Cache()

    def find(self, request: ResultRequest) -> Found:
        c = self._config
        if not request.claimed:
            return Found(ResultState.ABSENT)
        ignored: list[str] = []
        authentic = []
        title = c.run_name(request.key)
        for run in self._runs(request):
            if not isinstance(run, Mapping) or run.get("display_title") != title:
                continue
            why = self._not_authentic(run, request)
            if why:
                ignored.append(f"run {run.get('id')!r} ignored: {why}")
            else:
                authentic.append(run)
        if not authentic:
            return Found(ResultState.ABSENT, ignored=tuple(ignored))
        authentic.sort(key=lambda r: (str(r.get("created_at")), int(r["id"])))
        done = [r for r in authentic if r.get("status") == "completed"]
        good = [r for r in done if r.get("conclusion") == "success"]
        if good:
            # The newest run that finished cleanly answers the request. A
            # later run (started by hand with the same key) that failed or is
            # still going doesn't hide it.
            run = good[-1]
        elif len(done) < len(authentic):
            run = [r for r in authentic if r.get("status") != "completed"][-1]
            return Found(ResultState.RUNNING, url=_run_url(run), ignored=tuple(ignored))
        else:
            run = done[-1]
            return Found(
                ResultState.INCOMPLETE,
                url=_run_url(run),
                reason=f"the review run ended as {run.get('conclusion')!r}, without a result",
                ignored=tuple(ignored),
            )
        ignored += [
            f"run {r['id']} ignored: run {run['id']} answered the same request"
            for r in authentic
            if r is not run
        ]
        url = _run_url(run)
        try:
            result = self._artifact(run)
            review = to_review(result, run, request, c)
        except _Incomplete as e:
            return Found(ResultState.INCOMPLETE, url=url, reason=str(e), ignored=tuple(ignored))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            # Anything the checks above didn't foresee is still never a pass.
            return Found(
                ResultState.INCOMPLETE,
                url=url,
                reason=f"the result can't be read ({type(e).__name__})",
                ignored=tuple(ignored),
            )
        return Found(ResultState.READY, review=review, url=url, ignored=tuple(ignored))

    def _runs(self, request: ResultRequest) -> list:
        """Every dispatched run of the workflow since the first claim, reading
        up to ``MAX_RUN_PAGES`` pages. A listing that can't be read, or is
        longer than that, raises GitHubUnreadable: a stored verdict is never
        dropped because one page was missing."""
        c = self._config
        since = (min(request.claimed) - CLOCK_SKEW).astimezone(UTC).strftime("%Y-%m-%d")
        runs: list = []
        for page in range(1, MAX_RUN_PAGES + 1):
            listing = self._api.json(
                f"repos/{c.repository}/actions/workflows/{c.workflow_file}/runs"
                f"?event=workflow_dispatch&per_page=100&page={page}&created=%3E%3D{since}"
            )
            items = listing.get("workflow_runs") if isinstance(listing, Mapping) else None
            if not isinstance(items, list):
                raise GitHubUnreadable("the review workflow's runs could not be read")
            runs += items
            if len(items) < 100:
                return runs
        raise GitHubUnreadable("the review workflow has too many runs to read")

    def _not_authentic(self, run: Mapping, request: ResultRequest) -> str:
        """Why GitHub's own record of ``run`` doesn't make it the trusted
        workflow's answer to this request; empty when it does."""
        c = self._config
        repo = (run.get("repository") or {}).get("full_name")
        head_repo = (run.get("head_repository") or {}).get("full_name")
        if repo != c.repository or head_repo != c.repository:
            return f"it ran in {repo!r}, not {c.repository}"
        if run.get("path") != c.workflow:
            return f"it ran {run.get('path')!r}, not {c.workflow}"
        if run.get("event") != "workflow_dispatch":
            return f"it was started by {run.get('event')!r}, not by the controller"
        if run.get("head_branch") != c.ref:
            return f"it ran from branch {run.get('head_branch')!r}, not {c.ref}"
        allowed = {_login_key(d) for d in c.dispatchers}
        for who in ("actor", "triggering_actor"):
            login = (run.get(who) or {}).get("login")
            if _login_key(login) not in allowed:
                return f"its {who} is {login!r}, not a trusted dispatcher"
        sha = run.get("head_sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            return "it has no workflow commit"
        created = _time(run.get("created_at"))
        if created is None or created < min(request.claimed) - CLOCK_SKEW:
            return "it was started before the controller asked for this review"
        if not isinstance(run.get("id"), int) or not isinstance(run.get("run_attempt"), int):
            return "it has no run id"
        if not self._on_main(sha):
            return f"its workflow commit {sha[:12]} is not on the factory's {c.ref}"
        return ""

    def _on_main(self, sha: str) -> bool:
        if sha not in self._cache.on_main:
            c = self._config
            # Any other GitHubUnreadable propagates: the round is tried again,
            # and a verdict already recorded is kept meanwhile.
            try:
                cmp = self._api.json(f"repos/{c.repository}/compare/{sha}...{c.ref}")
            except NotFound:
                cmp = None  # a commit the factory doesn't have
            status = cmp.get("status") if isinstance(cmp, Mapping) else None
            self._cache.on_main[sha] = status in ("ahead", "identical")
        return self._cache.on_main[sha]

    def _artifact(self, run: Mapping) -> Mapping:
        c = self._config
        listing = self._api.json(
            f"repos/{c.repository}/actions/runs/{run['id']}/artifacts?per_page=100"
        )
        items = listing.get("artifacts") if isinstance(listing, Mapping) else None
        if not isinstance(items, list):
            raise _Incomplete("the review run's artifacts could not be listed")
        named = [a for a in items if isinstance(a, Mapping) and a.get("name") == ARTIFACT]
        if len(named) != 1:
            raise _Incomplete(f"the review run has {len(named)} {ARTIFACT} artifacts, not one")
        art = named[0]
        if art.get("expired") is not False:
            raise _Incomplete("the review run's result has expired or can't be read")
        if (art.get("workflow_run") or {}).get("id") != run["id"]:
            raise _Incomplete("the result artifact belongs to another run")
        aid = art.get("id")
        if not isinstance(aid, int):
            raise _Incomplete("the result artifact has no id")
        cache_key = (int(run["id"]), int(run["run_attempt"]), aid)
        if cache_key not in self._cache.results:
            blob = self._api.raw(f"repos/{c.repository}/actions/artifacts/{aid}/zip")
            self._cache.results[cache_key] = _unzip(blob)
        return self._cache.results[cache_key]


class _Incomplete(Exception):
    pass


def _unzip(blob: bytes) -> Mapping:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            if names != [RESULT_FILE]:
                raise _Incomplete(f"the result artifact holds {names!r}, not just {RESULT_FILE}")
            info = z.getinfo(RESULT_FILE)
            if info.file_size > MAX_RESULT_BYTES:
                raise _Incomplete("the result file is too large")
            raw = z.read(RESULT_FILE)
    except (zipfile.BadZipFile, OSError, ValueError, http.client.HTTPException) as e:
        raise _Incomplete(f"the result artifact can't be opened ({type(e).__name__})") from None
    try:
        data = json.loads(raw, parse_constant=_no_constant)
    except ValueError:
        raise _Incomplete("the result file is not JSON") from None
    if not isinstance(data, dict):
        raise _Incomplete("the result file is not a JSON object")
    return data


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not allowed")


def _run_url(run: Mapping) -> str:
    return f"https://github.com/{run['repository']['full_name']}/actions/runs/{run['id']}"


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


# --- turning a result into review evidence ----------------------------------------


def to_review(
    result: Mapping, run: Mapping, request: ResultRequest, config: WorkflowConfig
) -> Review:
    """The review evidence in an authenticated run's result, or _Incomplete.

    Provenance comes from ``run`` (GitHub's record); the result's own copies
    must agree with it and with ``request``. The model's output is only ever
    the mapping and the findings: the verifiers still decide the verdict."""
    if result.get("schema") != RESULT_SCHEMA:
        raise _Incomplete("the result is not a factory review result")
    prov = _obj(result, "provenance")
    expect = {
        "repository": config.repository,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "workflow_sha": run["head_sha"],
        "event": "workflow_dispatch",
    }
    for name, value in expect.items():
        if prov.get(name) != value:
            raise _Incomplete(f"the result's {name} doesn't match the run GitHub recorded")
    want_ref = f"{config.repository}/{config.workflow}@refs/heads/{config.ref}"
    if prov.get("workflow_ref") != want_ref:
        raise _Incomplete("the result was written by another workflow")
    req = _obj(result, "request")
    if not request.requests or req.get("sha256") not in request.requests:
        raise _Incomplete("the result answers a request the controller didn't send")
    for name, value in (
        ("key", request.key),
        ("repository", request.repository),
        ("pr", request.pr),
        ("contract_digest", request.digest),
        ("head", request.head),
        ("base", request.base),
        ("merge_base", request.merge_base),
    ):
        if req.get(name) != value:
            raise _Incomplete(f"the result's {name} doesn't match the request")
    jobs = _obj(result, "jobs")
    if jobs.get("review") != "success":
        raise _Incomplete(f"the Codex review job ended as {jobs.get('review')!r}")
    error = result.get("review_error")
    if error:
        raise _Incomplete(f"the Codex output was unusable: {str(error)[:300]}")
    model = _obj(result, "model")
    if model.get("model") != config.model or model.get("effort") != config.effort:
        raise _Incomplete("the review ran with another model or effort than the factory's setting")
    out = _obj(result, "review")
    problem = output_problem(out, request)
    if problem:
        raise _Incomplete(f"the Codex output is incomplete: {problem}")
    by = config.identity
    url = _run_url(run)
    head, base, digest = request.head, request.base, request.digest
    try:
        links = tuple(
            AssertionLink(
                criterion=x["criterion"],
                path=x["path"],
                test=x["test"],
                assertion=x["assertion"],
                contract_digest=digest,
                commit=head,
                base_commit=base,
                mapper=by,
                why=x["why"],
            )
            for x in out["links"]
        )
        linked = {link.criterion for link in links}
        if links and jobs.get("proofs") != "success":
            raise _Incomplete(f"the failure-proof job ended as {jobs.get('proofs')!r}")
        if links and result.get("proofs_error"):
            why = str(result["proofs_error"])[:300]
            raise _Incomplete(f"the failure proofs couldn't run: {why}")
        limits = tuple(
            FailureProofLimit(
                criterion=x["criterion"],
                contract_digest=digest,
                commit=head,
                base_commit=base,
                by=by,
                reason=x["reason"],
            )
            for x in out["limits"]
            # A criterion with a linked test needs its proof: the reviewer
            # can't excuse it with a reason instead.
            if x["criterion"] not in linked
        )
        proofs = _proofs(result, links, request, by, url)
        findings = _findings(out, request, by)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise _Incomplete(f"the Codex output can't be used ({e})") from None
    return Review(links, proofs, limits, url, (), findings, by)


def output_problem(out: Mapping, request: ResultRequest) -> str:
    """Why the model's structured output isn't a complete review; empty if it is.

    The schema in the workflow asks for this shape, but nothing outside the
    run enforces it, so it is checked again here."""
    if out.get("key") != request.key or out.get("commit") != request.head:
        return "it is for another request or commit"
    if out.get("complete") is not True:
        return "the reviewer said its review is not complete"
    for name in ("criteria", "links", "limits", "findings", "areas", "unreviewed_context"):
        value = out.get(name)
        if not isinstance(value, list):
            return f"{name} is missing"
    seen: dict[str, str] = {}
    for c in out["criteria"]:
        if not isinstance(c, Mapping) or not isinstance(c.get("criterion"), str):
            return "a criterion entry is malformed"
        if c.get("verdict") not in ("met", "not-met", "cannot-tell"):
            return f"criterion {c.get('criterion')!r} has no verdict"
        if not _text(c.get("reasoning")):
            return f"criterion {c['criterion']!r} gives no reasoning"
        if c["criterion"] in seen:
            return f"criterion {c['criterion']!r} is judged twice"
        seen[c["criterion"]] = c["verdict"]
    missing = sorted(set(request.criteria) - set(seen))
    if missing:
        return f"criteria {', '.join(missing)} were not judged"
    extra = sorted(set(seen) - set(request.criteria))
    if extra:
        return f"criteria {', '.join(extra)} are not in the contract"
    for cid, verdict in seen.items():
        if verdict == "cannot-tell" and request.criteria[cid] == "automated-check":
            return f"the reviewer couldn't tell whether {cid} is met"
    areas = {}
    for a in out["areas"]:
        if not isinstance(a, Mapping) or a.get("area") not in AREAS:
            return "an area entry is malformed"
        areas[a["area"]] = a.get("examined") is True and _text(a.get("note"))
    unexamined = [a for a in AREAS if not areas.get(a)]
    if unexamined:
        return f"it did not examine {', '.join(unexamined)}"
    for name, keys in (
        ("links", ("criterion", "path", "test", "assertion", "why")),
        ("limits", ("criterion", "reason")),
    ):
        for x in out[name]:
            if not isinstance(x, Mapping) or not all(_text(x.get(k)) for k in keys):
                return f"a {name[:-1]} entry is malformed"
            if name == "links" and not (
                _PATH_RE.fullmatch(x["path"]) and _TEST_PATH_RE.fullmatch(x["path"])
            ):
                return f"a link names {x['path'][:100]!r}, which is not a test file"
    for f in out["findings"]:
        if not isinstance(f, Mapping):
            return "a finding is malformed"
    if not all(_text(x) for x in out["unreviewed_context"]):
        return "an unreviewed-context entry is malformed"
    return ""


def _findings(out: Mapping, request: ResultRequest, by: str) -> tuple[Finding, ...]:
    items = []
    for f in out["findings"]:
        item = {k: f.get(k) for k in ("category", "severity", "summary", "suggested_action")}
        evidence = f.get("evidence")
        where = f.get("path")
        if isinstance(where, str) and where:
            line = f.get("line")
            where += f":{line}" if isinstance(line, int) and not isinstance(line, bool) else ""
            evidence = f"{where}: {evidence}"
        item["evidence"] = evidence
        if f.get("criterion") is not None:
            item["criterion"] = f["criterion"]
        if f.get("id") is not None:
            item["id"] = f["id"]
        items.append(item)
    judged = {c["criterion"]: c for c in out["criteria"]}
    raised = {i.get("criterion") for i in items if i.get("severity") == "blocking"}
    for cid, c in sorted(judged.items()):
        if c["verdict"] == "not-met" and cid not in raised:
            # A criterion judged unmet always leaves a blocking finding.
            items.append(
                {
                    "category": "code",
                    "severity": "blocking",
                    "criterion": cid,
                    "summary": f"{cid} is not met",
                    "evidence": c["reasoning"],
                    "suggested_action": f"Make the change meet {cid} as the contract states it.",
                }
            )
    for text in out["unreviewed_context"]:
        # Context nobody could read is never treated as reviewed.
        items.append(
            {
                "category": "product",
                "severity": "blocking",
                "summary": "The review could not see context the task refers to",
                "evidence": text,
                "suggested_action": "Capture the referenced material for the review, or"
                " confirm it isn't needed.",
            }
        )
    return from_reviewer(items, reviewer=by, commit=request.head)


def _proofs(
    result: Mapping,
    links: Iterable[AssertionLink],
    request: ResultRequest,
    by: str,
    url: str,
) -> tuple[FailureProof, ...]:
    """The failure proofs the proof job reported, only for tests the review
    linked. Every linked test must have exactly one: a test that wasn't run or
    whose result couldn't be read makes the whole result incomplete, so a
    measurement problem is never mistaken for a failing test."""
    raw = result.get("proofs")
    if not isinstance(raw, list):
        raw = []
    linked = {(link.criterion, link.path, link.test) for link in links}
    out = []
    seen = set()
    for p in raw:
        if not isinstance(p, Mapping):
            continue
        ident = (p.get("criterion"), p.get("path"), p.get("test"))
        if ident not in linked or ident in seen:
            continue
        if p.get("outcome") == NOT_RUN:
            raise _Incomplete(
                f"the linked test {ident[2]!r} in {ident[1]} wasn't run:"
                f" {str(p.get('output_excerpt') or '')[:200]}"
            )
        try:
            outcome = ProofOutcome(p.get("outcome"))
        except ValueError:
            raise _Incomplete(f"the proof for {ident[2]!r} has no outcome") from None
        seen.add(ident)
        out.append(
            FailureProof(
                criterion=ident[0],
                path=ident[1],
                test=ident[2],
                contract_digest=request.digest,
                tests_commit=request.head,
                code_commit=request.merge_base,
                outcome=outcome,
                by=by,
                url=url,
                output_excerpt=str(p.get("output_excerpt") or "")[:2000],
            )
        )
    if seen != linked:
        missing = sorted(t for _, _, t in linked - seen)
        raise _Incomplete(f"no failure proof for {', '.join(repr(t) for t in missing[:5])}")
    return tuple(out)


def _obj(data: Mapping, name: str) -> Mapping:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise _Incomplete(f"the result has no {name}")
    return value


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "ARTIFACT",
    "AREAS",
    "FACTORY_REPO",
    "IDENTITY",
    "MAX_REQUEST_CHARS",
    "RESULT_SCHEMA",
    "WORKFLOW_PATH",
    "Found",
    "ResultRequest",
    "ResultState",
    "WorkflowConfig",
    "WorkflowDispatchRuntime",
    "WorkflowResults",
    "output_problem",
    "request_hash",
    "to_review",
]
