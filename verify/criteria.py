"""Per-criterion verification of one candidate against its approved contract (ENG-156).

The verifier answers one question for Rolando: for this exact candidate commit,
built on this exact base, against this exact approved contract, which
acceptance criteria are shown to hold by evidence someone other than the worker
collected? Each criterion gets ``pass``, ``fail`` or ``unknown``; anything not
shown is ``unknown``, never a guessed pass.

What counts as evidence:

- ``CheckResult``: a verification command's result, either from a run of the
  trusted CI workflow on the candidate's repository, or re-run by a reviewer
  who is not the worker. A result for any other commit or base is stale and
  ignored. CI results are ignored if the candidate edits the workflow or check
  scripts, because then the candidate wrote its own check.
- ``Observation``: what a reviewer (not the worker) saw when following an
  ``observable-behavior`` or ``human-review`` criterion's steps on the
  candidate. A pass must say what was seen and what the observation does not
  cover.

What never counts: ``WriterClaim``, the worker's own account of its work. Claims
are listed next to the verdict so a contradiction is visible, and nothing else.

The verifier only reads. It does no I/O, changes none of its inputs, and its
report has no way to approve, merge or release: ``ready`` means "ready for
Rolando to review", and the decision stays his (ADR 0001 section 4). Whoever
collects the evidence (a script reading GitHub, or a person) does so with
read-only access; that boundary is ENG-143's.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase

from controller.contract import validate
from controller.interfaces import AttemptId, ContractDigest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_EXCERPT = 2000


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    """Not shown either way: missing, stale, untrusted or conflicting evidence."""


class Source(Enum):
    CI = "ci"
    """Run by the trusted CI workflow; trusted only per ``TrustPolicy``."""
    RERUN = "rerun"
    """Re-run by hand by a reviewer on a clean checkout of the candidate."""


def _commit(value: str, what: str) -> None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{what} must be a full 40-character lowercase commit id")


@dataclass(frozen=True)
class TrustPolicy:
    """Which evidence sources the verifier believes.

    Defaults fit the pilot repo: its CI is ``.github/workflows/ci.yml`` run by
    GitHub Actions, and its check scripts live under ``scripts/``.
    """

    workflow_path: str = ".github/workflows/ci.yml"
    app: str = "github-actions"
    check_paths: tuple[str, ...] = (".github/", "scripts/")
    """If the candidate changes anything here, its CI results are not trusted:
    on a pull request, GitHub runs the candidate's own copy of the workflow."""
    worker_logins: frozenset[str] = frozenset({"rnavarrete-factory-bot"})
    """Accounts the worker acts as. Nothing they report or observe is evidence."""


@dataclass(frozen=True)
class Candidate:
    """The PR under review, as read from GitHub by the evidence collector."""

    repository: str
    head_commit: str
    """The exact candidate commit (the PR head, not GitHub's merge ref)."""
    base_commit: str
    """The tip of the PR's base branch the evidence must be for."""
    merge_base: str
    """Where the candidate's branch actually starts."""
    branch: str
    pr_title: str
    pr_body: str
    changed_paths: tuple[str, ...]
    """Every path the PR adds, modifies, deletes or renames (both names)."""

    def __post_init__(self) -> None:
        _commit(self.head_commit, "head_commit")
        _commit(self.base_commit, "base_commit")
        _commit(self.merge_base, "merge_base")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))


@dataclass(frozen=True)
class CheckResult:
    """One verification command's result on one commit."""

    command: str
    """Exactly as written in the contract's ``verification_commands``."""
    commit: str
    base_commit: str
    exit_code: int | None
    """None if the command did not finish (cancelled, timed out, never ran)."""
    source: Source
    url: str
    """Where anyone can see the run or its log: the CI run, or the rerun's log."""
    output_excerpt: str = ""
    repository: str = ""
    """CI only: the repository the workflow ran in."""
    workflow_path: str = ""
    """CI only: the workflow file the run came from."""
    app: str = ""
    """CI only: the app that posted the run."""
    by: str = ""
    """Rerun only: the reviewer's GitHub login."""

    def __post_init__(self) -> None:
        _commit(self.commit, "commit")
        _commit(self.base_commit, "base_commit")
        if not isinstance(self.source, Source):
            raise ValueError("source must be a Source")


@dataclass(frozen=True)
class Observation:
    """What a reviewer saw when checking one criterion by hand."""

    criterion: str
    commit: str
    base_commit: str
    observer: str
    """The reviewer's GitHub login."""
    verdict: Verdict
    seen: str
    """What was actually done and observed, specific enough to repeat."""
    limitations: str
    """What this observation does not show (browsers not tried, edge cases...)."""

    def __post_init__(self) -> None:
        _commit(self.commit, "commit")
        _commit(self.base_commit, "base_commit")
        if self.verdict not in (Verdict.PASS, Verdict.FAIL):
            raise ValueError("an observation records pass or fail; leave it out if unsure")


@dataclass(frozen=True)
class WriterClaim:
    """Something the worker said about its own work. Never evidence."""

    text: str
    criterion: str | None = None
    """The criterion it is about, or None for a general claim ("all tests pass")."""
    says: Verdict | None = None


@dataclass(frozen=True)
class Citation:
    """A piece of evidence a verdict rests on, and what it does not show."""

    url: str
    detail: str
    limitations: str


@dataclass(frozen=True)
class CriterionVerdict:
    criterion: str
    statement: str
    evidence_type: str
    verdict: Verdict
    reasons: tuple[str, ...]
    """Why this verdict: for unknown and fail, what is missing or wrong."""
    citations: tuple[Citation, ...] = ()
    writer_claims: tuple[str, ...] = ()
    """The worker's claims about this criterion, with whether evidence agrees."""


@dataclass(frozen=True)
class Gate:
    """A whole-candidate condition that must hold before any verdict counts."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Report:
    contract_digest: str
    repository: str
    candidate_commit: str
    base_commit: str
    gates: tuple[Gate, ...]
    criteria: tuple[CriterionVerdict, ...]
    ignored: tuple[str, ...] = ()
    """Evidence that was supplied but not used (stale, untrusted), and why."""

    @property
    def blockers(self) -> tuple[str, ...]:
        out = [f"{g.name}: {g.detail}" for g in self.gates if not g.ok]
        out += [
            f"{c.criterion} is {c.verdict.value}: {'; '.join(c.reasons)}"
            for c in self.criteria
            if c.verdict is not Verdict.PASS
        ]
        if not self.criteria:
            out.append("no acceptance criteria were checked")
        return tuple(out)

    @property
    def ready(self) -> bool:
        """Ready for Rolando's review: every gate holds and every criterion passed.

        This is not an approval. Only Rolando approves, merges and releases.
        """
        return not self.blockers

    def still_current(self, candidate: Candidate) -> list[str]:
        """Why this report no longer describes ``candidate``; empty if it still does.

        A new push or a moved base invalidates the report until the checks are
        rerun on the new revision and the verifier runs again.
        """
        reasons = []
        if candidate.head_commit != self.candidate_commit:
            reasons.append(
                f"candidate moved from {self.candidate_commit} to {candidate.head_commit}"
            )
        if candidate.base_commit != self.base_commit:
            reasons.append(f"base moved from {self.base_commit} to {candidate.base_commit}")
        if candidate.repository != self.repository:
            reasons.append(f"repository is {candidate.repository}, report is for {self.repository}")
        return reasons


DEFAULT_POLICY = TrustPolicy()


# --- The verifier ---------------------------------------------------------------


def verify(
    contract: Mapping[str, object],
    approved_digest: ContractDigest | str,
    candidate: Candidate,
    results: Iterable[CheckResult] = (),
    observations: Iterable[Observation] = (),
    claims: Iterable[WriterClaim] = (),
    policy: TrustPolicy = DEFAULT_POLICY,
) -> Report:
    """Verify every acceptance criterion of ``contract`` on ``candidate``.

    ``approved_digest`` must come from Rolando's approval record, never from
    the PR. Inputs are read, never changed.
    """
    results = tuple(results)
    observations = tuple(observations)
    claims = tuple(claims)
    want = str(approved_digest)
    ignored: list[str] = []

    contract_errors = validate(contract, approved_digest)
    gates = [
        Gate(
            "contract",
            not contract_errors,
            "matches the approved digest" if not contract_errors else "; ".join(contract_errors),
        )
    ]
    if contract_errors:
        # Without a valid approved contract there is nothing to verify against.
        return Report(
            want,
            candidate.repository,
            candidate.head_commit,
            candidate.base_commit,
            tuple(gates),
            (),
        )

    gates += _candidate_gates(contract, ContractDigest(want), candidate)

    trusted = _trusted_results(results, candidate, policy, ignored)
    gates.append(_commands_gate(contract, trusted))
    usable_obs = _usable_observations(observations, candidate, policy, ignored)

    verdicts = tuple(
        _criterion(c, trusted, usable_obs, claims) for c in contract["acceptance_criteria"]
    )
    known = {v.criterion for v in verdicts}
    for claim in claims:
        if claim.criterion is not None and claim.criterion not in known:
            ignored.append(
                f"writer claim about unknown criterion {claim.criterion!r}: {claim.text!r}"
            )
    for o in observations:
        if o.criterion not in known:
            ignored.append(f"observation by {o.observer} names unknown criterion {o.criterion!r}")

    return Report(
        contract_digest=want,
        repository=candidate.repository,
        candidate_commit=candidate.head_commit,
        base_commit=candidate.base_commit,
        gates=tuple(gates),
        criteria=verdicts,
        ignored=tuple(ignored),
    )


def _candidate_gates(contract, approved: ContractDigest, candidate: Candidate) -> list[Gate]:
    gates = []
    repo_ok = candidate.repository == contract["repository"]
    gates.append(
        Gate(
            "repository",
            repo_ok,
            candidate.repository
            if repo_ok
            else f"candidate is in {candidate.repository}, "
            f"contract is for {contract['repository']}",
        )
    )

    attempt = AttemptId.from_branch(candidate.branch)
    if attempt is None or str(attempt.task) != contract["task_id"]:
        gates.append(
            Gate(
                "branch",
                False,
                f"{candidate.branch!r} is not a marker branch for task {contract['task_id']}",
            )
        )
    else:
        gates.append(Gate("branch", True, candidate.branch))

    parsed = AttemptId.from_pr_title(candidate.pr_title)
    if parsed is None:
        gates.append(Gate("pr title", False, "PR title does not start with a task marker"))
    else:
        title_attempt, short = parsed
        problems = []
        if short != approved.short:
            problems.append(f"marker digest {short} is not the approved {approved.short}")
        if title_attempt != attempt:
            problems.append(f"marker names {title_attempt}, branch names {attempt}")
        gates.append(Gate("pr title", not problems, "; ".join(problems) or "marker matches"))

    body = ContractDigest.from_pr_body(candidate.pr_body)
    if body is None:
        gates.append(Gate("pr body", False, "no single valid Contract-Digest line"))
    else:
        gates.append(
            Gate(
                "pr body",
                body == approved,
                "Contract-Digest matches"
                if body == approved
                else f"Contract-Digest {body} is not the approved digest",
            )
        )

    # G-A11: the candidate starts from the approved base commit.
    base_ok = candidate.merge_base == contract["base_commit"]
    gates.append(
        Gate(
            "base",
            base_ok,
            f"starts from {candidate.merge_base}"
            if base_ok
            else f"starts from {candidate.merge_base}, approved base is {contract['base_commit']}",
        )
    )

    # G-A10: every changed file is inside the permitted paths.
    outside = [p for p in candidate.changed_paths if not _permitted(p, contract["permitted_paths"])]
    if not candidate.changed_paths:
        gates.append(Gate("scope", False, "the candidate changes no files"))
    else:
        gates.append(
            Gate(
                "scope",
                not outside,
                "all changed files are permitted"
                if not outside
                else "outside the permitted paths: " + ", ".join(outside),
            )
        )
    return gates


def _permitted(path: str, patterns: Sequence[str]) -> bool:
    return any(_glob(path.split("/"), pattern.split("/")) for pattern in patterns)


def _glob(parts: Sequence[str], pattern: Sequence[str]) -> bool:
    """Match path segments: ``*`` stays within one segment, a ``**`` segment
    matches any number of segments."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_glob(parts[i:], pattern[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and _glob(parts[1:], pattern[1:])


def _changes_checks(candidate: Candidate, policy: TrustPolicy) -> list[str]:
    hits = []
    for path in candidate.changed_paths:
        if path == policy.workflow_path or any(
            path.startswith(p) if p.endswith("/") else path == p for p in policy.check_paths
        ):
            hits.append(path)
    return hits


def _trusted_results(results, candidate, policy, ignored) -> tuple[CheckResult, ...]:
    edited_checks = _changes_checks(candidate, policy)
    keep = []
    for r in results:
        label = f"{r.source.value} result for {r.command!r} on {r.commit[:12]} ({r.url})"
        if r.commit != candidate.head_commit:
            ignored.append(f"stale: {label} is not for candidate {candidate.head_commit[:12]}")
            continue
        if r.base_commit != candidate.base_commit:
            ignored.append(f"stale: {label} ran against base {r.base_commit[:12]}")
            continue
        if not r.url:
            ignored.append(f"untrusted: {label} has no link to its run")
            continue
        if r.source is Source.CI:
            why = []
            if r.app != policy.app:
                why.append(f"posted by {r.app or 'nobody'}, not {policy.app}")
            if r.workflow_path != policy.workflow_path:
                why.append(f"from workflow {r.workflow_path or '?'}, not {policy.workflow_path}")
            if r.repository != candidate.repository:
                why.append(f"ran in {r.repository or '?'}, not {candidate.repository}")
            if edited_checks:
                why.append("the candidate changes its own checks: " + ", ".join(edited_checks))
            if why:
                ignored.append(f"untrusted: {label}: " + "; ".join(why))
                continue
        else:
            if not r.by or r.by in policy.worker_logins:
                ignored.append(f"untrusted: {label} was not re-run by an independent reviewer")
                continue
        keep.append(r)
    return tuple(keep)


def _usable_observations(observations, candidate, policy, ignored) -> tuple[Observation, ...]:
    keep = []
    for o in observations:
        label = f"observation of {o.criterion} by {o.observer or 'nobody'}"
        if not o.observer or o.observer in policy.worker_logins:
            ignored.append(f"untrusted: {label}: the worker's own observation is not evidence")
        elif o.commit != candidate.head_commit or o.base_commit != candidate.base_commit:
            ignored.append(f"stale: {label} was made on {o.commit[:12]}/{o.base_commit[:12]}")
        else:
            keep.append(o)
    return tuple(keep)


def _commands_gate(contract, trusted: Sequence[CheckResult]) -> Gate:
    """Every verification command in the contract passed on this exact revision."""
    problems = []
    for command in contract["verification_commands"]:
        verdict, _, why = _command_verdict(command, trusted)
        if verdict is not Verdict.PASS:
            problems.append(f"{command}: {why}")
    return Gate(
        "verification commands",
        not problems,
        "; ".join(problems) or "every verification command passed on this revision",
    )


def _command_verdict(command: str, trusted: Sequence[CheckResult]):
    runs = [r for r in trusted if r.command == command]
    if not runs:
        return Verdict.UNKNOWN, (), "no trusted result on this revision"
    failed = [r for r in runs if r.exit_code not in (0, None)]
    if failed:
        return (
            Verdict.FAIL,
            tuple(_cite(r) for r in failed),
            f"exited {failed[0].exit_code} ({failed[0].url})",
        )
    if any(r.exit_code is None for r in runs):
        r = next(r for r in runs if r.exit_code is None)
        return Verdict.UNKNOWN, (), f"did not finish ({r.url})"
    return Verdict.PASS, tuple(_cite(r) for r in runs), "passed"


def _cite(r: CheckResult) -> Citation:
    who = "trusted CI" if r.source is Source.CI else f"rerun by {r.by}"
    excerpt = r.output_excerpt[-_MAX_EXCERPT:]
    detail = f"`{r.command}` exited {r.exit_code} on {r.commit} ({who})"
    if excerpt:
        detail += f"\n{excerpt}"
    return Citation(
        r.url,
        detail,
        "Shows the command's result on this exact revision. It does not show which "
        "test, if any, checks this criterion; that is the assertion map's job.",
    )


def _criterion(c, trusted, observations, claims) -> CriterionVerdict:
    cid, ev = c["id"], c["evidence"]
    kind = ev["type"]
    if kind == "automated-check":
        verdict, citations, why = _command_verdict(ev["command"], trusted)
        reasons = () if verdict is Verdict.PASS else (f"`{ev['command']}` {why}",)
    else:
        verdict, citations, reasons = _observed(cid, ev, observations)
    return CriterionVerdict(
        criterion=cid,
        statement=c["statement"],
        evidence_type=kind,
        verdict=verdict,
        reasons=tuple(reasons),
        citations=tuple(citations),
        writer_claims=_claims_for(cid, verdict, claims),
    )


def _observed(cid, ev, observations):
    mine = [o for o in observations if o.criterion == cid]
    if ev["type"] == "human-review":
        others = [o for o in mine if o.observer != ev["reviewer"]]
        mine = [o for o in mine if o.observer == ev["reviewer"]]
        if others and not mine:
            names = ", ".join(sorted({o.observer for o in others}))
            return (
                Verdict.UNKNOWN,
                (),
                (f"the contract names {ev['reviewer']} as reviewer; observed only by {names}",),
            )
    if not mine:
        return Verdict.UNKNOWN, (), ("nobody independent has checked it on this revision",)
    failed = [o for o in mine if o.verdict is Verdict.FAIL]
    if failed:
        return (
            Verdict.FAIL,
            tuple(Citation("", f"{o.observer}: {o.seen}", o.limitations) for o in failed),
            tuple(f"{o.observer} saw it fail: {o.seen}" for o in failed),
        )
    complete = [o for o in mine if o.seen.strip() and o.limitations.strip()]
    if not complete:
        return (
            Verdict.UNKNOWN,
            (),
            ("a pass was recorded without saying what was seen and what it does not cover",),
        )
    return (
        Verdict.PASS,
        tuple(Citation("", f"{o.observer}: {o.seen}", o.limitations) for o in complete),
        (),
    )


def _claims_for(cid: str, verdict: Verdict, claims: Sequence[WriterClaim]) -> tuple[str, ...]:
    out = []
    for claim in claims:
        if claim.criterion not in (cid, None):
            continue
        note = f"writer said: {claim.text!r} (not evidence)"
        if claim.says is not None and claim.says is not verdict:
            note += f"; independent evidence gives {verdict.value}"
        out.append(note)
    return tuple(out)


# --- Reading the pilot's check evidence ----------------------------------------

# The pilot's scripts/check.mjs step names, and the contract commands they are.
PILOT_STEP_COMMANDS = {
    "typecheck": "npm run typecheck",
    "test": "npm test",
    "build": "npm run build",
}


def results_from_check_evidence(
    evidence: Mapping[str, object],
    *,
    base_commit: str,
    source: Source,
    url: str,
    repository: str = "",
    workflow_path: str = "",
    app: str = "",
    by: str = "",
) -> tuple[CheckResult, ...]:
    """Turn the pilot's ``check-evidence.json`` (schemaVersion 1) into results.

    The file only says what ran; the caller says where it came from (``source``,
    ``url`` and, for CI, the run's repository, workflow and app, read from
    GitHub's record of the run, not from the file). The commit is the one the
    file itself names, so evidence for another commit comes out stale. The
    overall outcome is reported as ``npm run check``.
    """
    if evidence.get("schemaVersion") != 1:
        raise ValueError("unsupported check evidence: schemaVersion must be 1")
    candidate = evidence.get("candidate")
    commit = candidate.get("commit") if isinstance(candidate, Mapping) else None
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("check evidence names no candidate commit")
    common = dict(
        commit=commit,
        base_commit=base_commit,
        source=source,
        url=url,
        repository=repository,
        workflow_path=workflow_path,
        app=app,
        by=by,
    )
    steps = evidence.get("steps")
    steps = steps if isinstance(steps, list | tuple) else ()
    out = []
    for name, command in PILOT_STEP_COMMANDS.items():
        matches = [s for s in steps if isinstance(s, Mapping) and s.get("name") == name]
        if len(matches) != 1:
            continue  # missing or duplicated: no result, so the command stays unknown
        step = matches[0]
        out.append(
            CheckResult(
                command=command,
                exit_code=_step_exit(step),
                output_excerpt=str(step.get("logTail") or ""),
                **common,
            )
        )
    outcome = evidence.get("outcome")
    out.append(
        CheckResult(
            command="npm run check",
            exit_code=0 if outcome == "pass" else 1 if outcome == "fail" else None,
            output_excerpt=f"outcome: {outcome}",
            **common,
        )
    )
    return tuple(out)


def _step_exit(step: Mapping[str, object]) -> int | None:
    status, code = step.get("status"), step.get("exitCode")
    if status == "pass" and code == 0:
        return 0
    if status == "fail":
        return code if isinstance(code, int) and not isinstance(code, bool) and code else 1
    return None  # error, timeout or anything unexpected: did not finish


# --- Rendering ------------------------------------------------------------------


def render(report: Report) -> str:
    """The report as Markdown, for a PR comment or Rolando's review."""
    lines = [
        "## Criterion verification",
        "",
        f"- Contract digest: `{report.contract_digest}`",
        f"- Candidate commit: `{report.candidate_commit}`",
        f"- Base commit: `{report.base_commit}`",
        f"- Ready for Rolando's review: **{'yes' if report.ready else 'no'}** "
        "(this is not an approval)",
        "",
        "| Criterion | Verdict | Evidence or reason |",
        "|---|---|---|",
    ]
    for c in report.criteria:
        if c.verdict is Verdict.PASS:
            why = "; ".join(f"{x.url or x.detail.splitlines()[0]}" for x in c.citations)
        else:
            why = "; ".join(c.reasons)
        lines.append(f"| {c.criterion} | {c.verdict.value} | {_cell(why)} |")
    lines += ["", "### Gates", ""]
    lines += [f"- {'ok' if g.ok else 'BLOCKED'}: {g.name}: {g.detail}" for g in report.gates]
    for c in report.criteria:
        lines += ["", f"### {c.criterion}: {c.verdict.value}", "", c.statement]
        for x in c.citations:
            lines += ["", f"- Evidence: {x.url}".rstrip(), "", "```", x.detail, "```"]
            lines.append(f"- Limitations: {x.limitations}")
        lines += [f"- {r}" for r in c.reasons]
        lines += [f"- {w}" for w in c.writer_claims]
    if report.ignored:
        lines += ["", "### Evidence not used", ""]
        lines += [f"- {i}" for i in report.ignored]
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "Candidate",
    "CheckResult",
    "Citation",
    "CriterionVerdict",
    "Gate",
    "Observation",
    "Report",
    "Source",
    "TrustPolicy",
    "Verdict",
    "WriterClaim",
    "results_from_check_evidence",
    "render",
    "verify",
]
