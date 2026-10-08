"""Which Linear projects the service works for, and on what terms.

One JSON file on the host, outside every checkout, that Rolando changes by
hand (through the one-time onboarding step or a deploy). Each entry maps one
explicitly chosen Linear project to the repository, routine, checks, actions
and attempt budget the factory may use for its tickets. A ticket from any
other project is refused and nothing is dispatched; deleting an entry stops
new work on that project at the next round, including tickets already queued
but not yet started.

The service re-reads the file every round and records its sha256 in the
ledger whenever it changes, so every dispatch can be traced to the mapping
in force at the time.

v1 has one worker routine bound to one repository, so every entry must name
exactly the repository and routine the dispatcher is built for.

Todo intake (ENG-174) reads three more things from it:

- ``approver_linear_user_id`` (top level): Rolando's Linear user id. Only a
  move into Todo recorded by Linear as his counts.
- ``intake_since`` (top level, ISO time): moves before it are never read, so
  onboarding a project doesn't start everything already in Todo.
- per project, ``issues`` (only these ticket keys may start) and
  ``skip_labels`` (a ticket with one of these labels never starts). This is
  how baseline tasks that share a Linear project with factory tasks stay out
  of the factory: list the factory tasks in ``issues``, and label the
  baseline tickets too as a second guard.
- per project, ``protected_paths``: paths a contract approved by a Todo move
  alone may not touch (workflows, agent instructions, build config). Such a
  change still needs Rolando's typed approval.

Bounded repairs (ENG-160) read one more, per project: ``repair_allowance``,
how many repair attempts a Todo move lets the factory start on its own
(0 unless set, always less than ``max_attempts``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from controller import contract as contracts
from controller.attempts.limits import PILOT_LIMITS
from controller.contract.contract import _path as _check_path

FORMAT = "factory-onboarding/v1"
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_TRIG_RE = re.compile(r"^trig_[A-Za-z0-9]+$")
_KEYS = {
    "linear_project_id",
    "name",
    "repository",
    "base_branch",
    "routine_id",
    "allowed_actions",
    "checks",
    "max_attempts",
    "status_issue_id",
    "issues",
    "skip_labels",
    "protected_paths",
    "repair_allowance",
}
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")


class OnboardingError(ValueError):
    """The onboarding file is unusable; the service dispatches nothing."""


@dataclass(frozen=True)
class Project:
    linear_project_id: str
    name: str
    repository: str
    base_branch: str
    routine_id: str
    allowed_actions: frozenset[str]
    checks: frozenset[str]
    """The verification commands a contract may name."""
    max_attempts: int
    status_issue_id: str | None
    """Where factory-wide notices for this project go (outages, pause)."""
    issues: frozenset[str] | None = None
    """If set, only these ticket keys may start (ENG-174)."""
    skip_labels: frozenset[str] = frozenset()
    """Tickets with any of these labels never start (ENG-174)."""
    protected_paths: frozenset[str] = frozenset()
    """Paths a Todo move alone can't approve a change to (ENG-174)."""
    repair_allowance: int = 0
    """How many repair attempts a Todo move in this project allows the factory
    to start on its own after an independently found failure (ENG-160). Zero
    unless set: every repair then needs Rolando's typed go-ahead. Always less
    than ``max_attempts``, and counted inside it."""

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "linear_project_id": self.linear_project_id,
            "name": self.name,
            "repository": self.repository,
            "base_branch": self.base_branch,
            "routine_id": self.routine_id,
            "allowed_actions": sorted(self.allowed_actions),
            "checks": sorted(self.checks),
            "max_attempts": self.max_attempts,
            "repair_allowance": self.repair_allowance,
        }

    def contract_problems(self, contract: Mapping[str, object], task: str) -> list[str]:
        """Why this contract is outside what the project was onboarded for."""
        problems = list(contracts.approval_errors(contract))
        if problems:
            return problems
        if contract.get("task_id") != task:
            problems.append(f"the contract is for task {contract.get('task_id')!r}, not {task!r}")
        if contract.get("repository") != self.repository:
            problems.append(
                f"the contract names {contract.get('repository')!r}; this project works on"
                f" {self.repository}"
            )
        actions = set(contract.get("permitted_actions") or ())  # type: ignore[arg-type]
        if not actions <= self.allowed_actions:
            problems.append(f"actions not allowed here: {sorted(actions - self.allowed_actions)}")
        checks = set(contract.get("verification_commands") or ())  # type: ignore[arg-type]
        if not checks <= self.checks:
            problems.append(f"checks not onboarded: {sorted(checks - self.checks)}")
        budget = contract.get("attempt_budget")
        if not isinstance(budget, int) or budget > self.max_attempts:
            problems.append(f"attempt budget {budget!r} is over this project's {self.max_attempts}")
        return problems

    def source_problems(self, contract: Mapping[str, object], task: str) -> list[str]:
        """``contract_problems``, plus what a Todo move alone can't approve:
        a permitted path that could reach a protected one."""
        return self.contract_problems(contract, task) or self.protected_problems(contract)

    def protected_problems(self, contract: Mapping[str, object]) -> list[str]:
        """Permitted paths that could reach a protected one."""
        paths = contract.get("permitted_paths") or ()
        touched = sorted(
            str(p)
            for p in paths  # type: ignore[union-attr]
            if any(_may_overlap(str(p), q) for q in self.protected_paths)
        )
        if not touched:
            return []
        return [f"paths that may reach protected files need Rolando's typed approval: {touched}"]


def _may_overlap(path: str, protected: str) -> bool:
    """Whether a path or glob could match a file under ``protected``, judged
    by the text before any wildcard; errs towards yes."""
    a, b = path.split("*", 1)[0], protected.split("*", 1)[0]
    return a.startswith(b) or b.startswith(a)


@dataclass(frozen=True)
class Onboarding:
    projects: Mapping[str, Project]
    """By Linear project id."""
    sha256: str
    intake_enabled: bool
    """False until the full path is qualified (ENG-163): Todo moves are then
    recorded as refused, and only already-queued work is processed."""
    approver_linear_user_id: str | None = None
    intake_since: datetime | None = None

    def project(self, linear_project_id: str) -> Project | None:
        return self.projects.get(linear_project_id)


def _strings(value: object, what: str) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise OnboardingError(f"{what} must be a non-empty list of text")
    return frozenset(value)


def parse(raw: bytes, *, repository: str, routine_id: str) -> Onboarding:
    """Check the whole file; any problem refuses all of it."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise OnboardingError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise OnboardingError(f"format must be {FORMAT!r}")
    unknown = set(doc) - {
        "format",
        "intake_enabled",
        "projects",
        "approver_linear_user_id",
        "intake_since",
    }
    if unknown:
        raise OnboardingError(f"unknown keys: {sorted(unknown)}")
    enabled = doc.get("intake_enabled", False)
    if not isinstance(enabled, bool):
        raise OnboardingError("intake_enabled must be true or false")
    approver = doc.get("approver_linear_user_id")
    if approver is not None and (not isinstance(approver, str) or not approver.strip()):
        raise OnboardingError("approver_linear_user_id must be text")
    since = doc.get("intake_since")
    if since is not None:
        try:
            since = datetime.fromisoformat(since) if isinstance(since, str) else None
        except ValueError:
            since = None
        if since is None or since.tzinfo is None:
            raise OnboardingError("intake_since must be an ISO time with a time zone")
    entries = doc.get("projects")
    if not isinstance(entries, list):
        raise OnboardingError("projects must be a list")
    projects: dict[str, Project] = {}
    for i, e in enumerate(entries):
        where = f"projects[{i}]"
        if (
            not isinstance(e, dict)
            or set(e) - _KEYS
            or not {
                "linear_project_id",
                "name",
                "repository",
                "routine_id",
                "allowed_actions",
                "checks",
                "max_attempts",
            }
            <= set(e)
        ):
            raise OnboardingError(f"{where} has missing or unknown keys")
        pid, name = e["linear_project_id"], e["name"]
        if not isinstance(pid, str) or not pid.strip() or not isinstance(name, str):
            raise OnboardingError(f"{where}: linear_project_id and name must be text")
        if pid in projects:
            raise OnboardingError(f"{where}: project {pid} is onboarded twice")
        repo, trig = e["repository"], e["routine_id"]
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
            raise OnboardingError(f"{where}: repository must be owner/name")
        if repo != repository:
            raise OnboardingError(f"{where}: v1 can only work on {repository}, not {repo}")
        if not isinstance(trig, str) or not _TRIG_RE.fullmatch(trig) or trig != routine_id:
            raise OnboardingError(f"{where}: routine must be {routine_id}")
        branch = e.get("base_branch", "main")
        if branch != "main":
            raise OnboardingError(f"{where}: base_branch must be main in v1")
        actions = _strings(e["allowed_actions"], f"{where}.allowed_actions")
        unknown = actions - set(contracts.ACTIONS)
        if unknown:
            raise OnboardingError(f"{where}: unknown actions {sorted(unknown)}")
        checks = _strings(e["checks"], f"{where}.checks")
        budget = e["max_attempts"]
        cap = PILOT_LIMITS.attempts_per_task
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= cap:
            raise OnboardingError(f"{where}: max_attempts must be 1..{cap}")
        allowance = e.get("repair_allowance", 0)
        if (
            isinstance(allowance, bool)
            or not isinstance(allowance, int)
            or not 0 <= allowance < budget
        ):
            raise OnboardingError(
                f"{where}: repair_allowance must be 0..{budget - 1} (less than max_attempts)"
            )
        status = e.get("status_issue_id")
        if status is not None and (not isinstance(status, str) or not status.strip()):
            raise OnboardingError(f"{where}: status_issue_id must be text")
        issues = None
        if "issues" in e:
            issues = _strings(e["issues"], f"{where}.issues")
            bad = sorted(k for k in issues if not _ISSUE_KEY_RE.fullmatch(k))
            if bad:
                raise OnboardingError(f"{where}: not ticket keys: {bad}")
        skip = _strings(e["skip_labels"], f"{where}.skip_labels") if "skip_labels" in e else None
        protected = None
        if "protected_paths" in e:
            protected = _strings(e["protected_paths"], f"{where}.protected_paths")
            for q in protected:
                errors = []
                _check_path(errors, q.removesuffix("/"), f"{where}.protected_paths")
                if errors:
                    raise OnboardingError(errors[0])
        projects[pid] = Project(
            pid,
            name,
            repo,
            branch,
            trig,
            actions,
            checks,
            budget,
            status,
            issues,
            skip or frozenset(),
            protected or frozenset(),
            allowance,
        )
    return Onboarding(projects, hashlib.sha256(raw).hexdigest(), enabled, approver, since)


def load(path: Path, *, repository: str, routine_id: str) -> Onboarding:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise OnboardingError(f"can't read {path}: {e.strerror}") from None
    return parse(raw, repository=repository, routine_id=routine_id)


def empty() -> Onboarding:
    return Onboarding({}, hashlib.sha256(b"").hexdigest(), False)


__all__: Sequence[str] = [
    "FORMAT",
    "Onboarding",
    "OnboardingError",
    "Project",
    "empty",
    "load",
    "parse",
]
