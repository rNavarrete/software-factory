"""What the factory may put in a contract for one onboarded Linear project.

The onboarding file (``controller/service/onboarding.py``) already says which
repository, actions, checks and attempt budget a project gets. This file adds
what drafting needs on top: which paths a worker may write, which paths are
never offered, which command proves logic criteria, how the base commit is
chosen, and when a ticket is too big. Rolando changes it by hand, like the
onboarding file; ticket text can never change it.

Its sha256 goes into every contract it shapes (in ``notes``), so the contract's
digest binds the policy that was in force.

Standard library only. ``load`` reads one file; the rest does no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

FORMAT = "factory-drafting/v1"
_PATH_RE = re.compile(r"^[A-Za-z0-9._*/-]+$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$")
_KEYS = {
    "linear_project_id",
    "writable_paths",
    "protected_paths",
    "verification_commands",
    "test_command",
    "escalate_to",
    "max_criteria",
    "max_ticket_chars",
    "base",
    "worker",
}
_BASE_KEYS = {"branch", "workflow_path", "job", "lookback"}
# All drafting ever offers, whatever the project allows. Dependencies and
# control files need a risk marker and Rolando's explicit word; deleting files
# isn't offered in v1.
DRAFTABLE_ACTIONS = ("modify-files", "add-files", "add-tests")


class PolicyError(ValueError):
    """The drafting policy is unusable; nothing is drafted."""


@dataclass(frozen=True)
class BasePolicy:
    branch: str
    workflow_path: str
    job: str
    lookback: int
    """How many of the newest commits on ``branch`` may be considered."""


@dataclass(frozen=True)
class ProjectPolicy:
    linear_project_id: str
    writable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    verification_commands: tuple[str, ...]
    test_command: str
    escalate_to: str
    max_criteria: int
    max_ticket_chars: int
    base: BasePolicy
    worker: tuple[str, ...]
    """Capabilities a worker needs for this repository (ENG-154 reads these)."""


@dataclass(frozen=True)
class Policy:
    projects: Mapping[str, ProjectPolicy]
    sha256: str

    def project(self, linear_project_id: str) -> ProjectPolicy | None:
        return self.projects.get(linear_project_id)


def matches(path: str, pattern: str) -> bool:
    """The verifier's glob rule: ``*`` within one segment, ``**`` any number."""

    def walk(parts: list[str], pat: list[str]) -> bool:
        if not pat:
            return not parts
        if pat[0] == "**":
            return any(walk(parts[i:], pat[1:]) for i in range(len(parts) + 1))
        return bool(parts) and fnmatchcase(parts[0], pat[0]) and walk(parts[1:], pat[1:])

    return walk(path.split("/"), pattern.split("/"))


def _example(pattern: str) -> str:
    return "/".join("x" if p in ("*", "**") else p.replace("*", "x") for p in pattern.split("/"))


def overlaps(a: str, b: str) -> bool:
    """Could one path match both globs? Checked both ways on a sample path,
    and by prefix, which is enough for the plain globs policies use."""
    if matches(_example(a), b) or matches(_example(b), a):
        return True
    pa, pb = a.split("**")[0].split("*")[0], b.split("**")[0].split("*")[0]
    return bool(pa and pb) and (pa.startswith(pb) or pb.startswith(pa)) and ("*" in a or "*" in b)


def _strings(value: object, what: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty):
        raise PolicyError(f"{what} must be a list of text")
    if not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyError(f"{what} must be a list of text")
    if len(set(value)) != len(value):
        raise PolicyError(f"{what} repeats an entry")
    return tuple(value)


def _int(value: object, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise PolicyError(f"{what} must be a whole number from {lo} to {hi}")
    return value


def parse(raw: bytes) -> Policy:
    """Check the whole file; any problem refuses all of it."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise PolicyError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise PolicyError(f"format must be {FORMAT!r}")
    if set(doc) - {"format", "projects"}:
        raise PolicyError(f"unknown keys: {sorted(set(doc) - {'format', 'projects'})}")
    entries = doc.get("projects")
    if not isinstance(entries, list):
        raise PolicyError("projects must be a list")
    projects: dict[str, ProjectPolicy] = {}
    for i, e in enumerate(entries):
        where = f"projects[{i}]"
        if not isinstance(e, dict) or set(e) != _KEYS:
            raise PolicyError(f"{where} must have exactly the keys {sorted(_KEYS)}")
        pid = e["linear_project_id"]
        if not isinstance(pid, str) or not pid.strip():
            raise PolicyError(f"{where}.linear_project_id must be text")
        if pid in projects:
            raise PolicyError(f"{where}: project {pid} appears twice")
        writable = _strings(e["writable_paths"], f"{where}.writable_paths")
        protected = _strings(e["protected_paths"], f"{where}.protected_paths")
        for p in writable + protected:
            parts = p.split("/")
            if not _PATH_RE.fullmatch(p) or "" in parts or "." in parts or ".." in parts:
                raise PolicyError(f"{where}: {p!r} is not a repo-relative path or glob")
        clash = sorted({w for w in writable for p in protected if overlaps(w, p)})
        if clash:
            raise PolicyError(f"{where}: writable paths overlap protected ones: {clash}")
        commands = _strings(e["verification_commands"], f"{where}.verification_commands")
        test = e["test_command"]
        if test not in commands:
            raise PolicyError(f"{where}.test_command must be one of verification_commands")
        login = e["escalate_to"]
        if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
            raise PolicyError(f"{where}.escalate_to must be a GitHub login")
        base = e["base"]
        if not isinstance(base, dict) or set(base) != _BASE_KEYS:
            raise PolicyError(f"{where}.base must have exactly the keys {sorted(_BASE_KEYS)}")
        for k in ("branch", "workflow_path", "job"):
            if not isinstance(base[k], str) or not base[k].strip():
                raise PolicyError(f"{where}.base.{k} must be text")
        projects[pid] = ProjectPolicy(
            linear_project_id=pid,
            writable_paths=writable,
            protected_paths=protected,
            verification_commands=commands,
            test_command=test,
            escalate_to=login,
            max_criteria=_int(e["max_criteria"], f"{where}.max_criteria", 1, 20),
            max_ticket_chars=_int(e["max_ticket_chars"], f"{where}.max_ticket_chars", 200, 20000),
            base=BasePolicy(
                base["branch"],
                base["workflow_path"],
                base["job"],
                _int(base["lookback"], f"{where}.base.lookback", 1, 100),
            ),
            worker=_strings(e["worker"], f"{where}.worker", empty=True),
        )
    return Policy(projects, hashlib.sha256(raw).hexdigest())


def load(path: Path) -> Policy:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise PolicyError(f"can't read {path}: {e.strerror}") from None
    return parse(raw)


__all__ = [
    "DRAFTABLE_ACTIONS",
    "FORMAT",
    "BasePolicy",
    "Policy",
    "PolicyError",
    "ProjectPolicy",
    "load",
    "matches",
    "overlaps",
    "parse",
]
