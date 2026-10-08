"""The single-task contract: format, validation, canonical bytes and digest.

A contract is a JSON object describing one bounded task. Rolando approves a
contract by its digest, the sha256 of its canonical bytes, so any change to
any field (including scope, base commit and attempt budget) needs a new
approval, even if the ``version`` label is reused.

The field-by-field rules are in schema/contract-v1.schema.json, kept in step
with this module by tests/test_contract.py. This module is the authority: the
schema file documents the format for people and other tools.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType

from controller.interfaces import ContractDigest, TaskId

FORMAT = "factory-contract/v1"

EVIDENCE_TYPES = ("automated-check", "observable-behavior", "human-review")
STATUSES = ("ready", "needs-clarification")
ACTIONS = (
    "modify-files",
    "add-files",
    "delete-files",
    "add-tests",
    "add-dependency",
    "change-control-files",
)
RISK_MARKERS = (
    "control-change",
    "new-dependency",
    "user-data",
    "security",
    "external-service",
    "data-migration",
)
# An action that is only allowed when the contract also carries the marker.
ACTION_NEEDS_MARKER = {
    "add-dependency": "new-dependency",
    "change-control-files": "control-change",
}
MAX_ATTEMPT_BUDGET = 3  # docs/limits.md: 3 attempts per task, including the first.

REQUIRED = (
    "format",
    "task_id",
    "version",
    "goal",
    "inputs",
    "repository",
    "base_commit",
    "permitted_paths",
    "permitted_actions",
    "risk_markers",
    "acceptance_criteria",
    "verification_commands",
    "attempt_budget",
    "escalate_to",
    "depends_on",
)
OPTIONAL = ("notes",)
# The fields an approval is bound to besides the digest (ADR 0001 section 5).
BOUND_FIELDS = (
    "repository",
    "base_commit",
    "permitted_paths",
    "permitted_actions",
    "attempt_budget",
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_LOGIN = r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}"
_REPO_RE = re.compile(rf"^{_LOGIN}/[A-Za-z0-9._-]{{1,100}}$")
_CRITERION_ID_RE = re.compile(r"^ac[1-9][0-9]*$")
_LOGIN_RE = re.compile(rf"^{_LOGIN}$")
_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._*/-]+$")
_MAX_TEXT = 4000
_MAX_DEPTH = 16
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


# --- Canonical bytes and digest ----------------------------------------------


def canonical_bytes(contract: Mapping[str, object]) -> bytes:
    """The bytes a contract's digest is taken over.

    UTF-8 JSON with keys sorted at every level, no whitespace between tokens,
    and non-ASCII characters written as themselves. Two contracts with the same
    content always give the same bytes, whatever their key order or layout.
    Raises ValueError for anything that is not plain JSON (floats included:
    the format has none, and their text form is not canonical).
    """
    _require_plain_json(contract, "contract")
    text = json.dumps(_thaw(contract), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def digest(contract: Mapping[str, object]) -> ContractDigest:
    """The contract's canonical digest: sha256 of ``canonical_bytes``."""
    return ContractDigest.of(canonical_bytes(contract))


def loads(text: str | bytes) -> Mapping[str, object]:
    """Parse contract JSON into a read-only contract.

    Rejects duplicate keys, NaN and Infinity, and any top level that is not an
    object, so what was approved can't be read two ways. Raises ValueError
    (json.JSONDecodeError is one) for anything it refuses.
    """

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [k for k, _ in pairs]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate keys in contract JSON: {dupes}")
        return dict(pairs)

    def bad_constant(name: str) -> object:
        raise ValueError(f"{name} is not allowed in contract JSON")

    try:
        value = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=bad_constant)
    except RecursionError:
        raise ValueError("contract JSON is nested too deeply") from None
    if not isinstance(value, dict):
        raise ValueError("contract JSON must be an object")
    return freeze(value)


def freeze(contract: Mapping[str, object]) -> Mapping[str, object]:
    """A deep read-only copy: objects become read-only mappings, lists tuples.

    An approved contract is held this way so nothing in the controller can
    change it after its digest was taken.
    """
    _require_plain_json(contract, "contract")
    frozen = _freeze(contract)
    assert isinstance(frozen, Mapping)
    return frozen


def binding(contract: Mapping[str, object]) -> Mapping[str, object]:
    """What an approval of this contract is bound to: its digest plus the bound
    fields. If any of them differ, the approval does not match (ADR 0001
    section 5, item 1). Raises ValueError if the contract does not validate.
    """
    errors = structure_errors(contract)
    if errors:
        raise ValueError("contract is not valid: " + "; ".join(errors))
    bound = {k: contract[k] for k in BOUND_FIELDS}
    bound["digest"] = str(digest(contract))
    bound["task_id"] = contract["task_id"]
    bound["version"] = contract["version"]
    return freeze(bound)


# --- Validation ---------------------------------------------------------------


def validate(contract: object, expected_digest: ContractDigest | str) -> list[str]:
    """Every reason this contract can't be approved or dispatched; empty if none.

    Checks the structure, that no criterion still needs clarification, and that
    the contract's canonical digest equals ``expected_digest``. The routine
    adapter calls this before every launch. Errors come in a fixed order.
    """
    errors = approval_errors(contract)
    if isinstance(expected_digest, ContractDigest):
        want = expected_digest.value
    elif isinstance(expected_digest, str) and _DIGEST_RE.fullmatch(expected_digest):
        want = expected_digest
    else:
        return errors + ["expected digest must be 64 lowercase hex characters"]
    if isinstance(contract, Mapping) and not _plain_json_errors(contract, "contract"):
        got = str(digest(contract))
        if got != want:
            errors.append(f"contract digest is {got}, expected {want}")
    return errors


def approval_errors(contract: object) -> list[str]:
    """Reasons Rolando can't approve this contract yet: structural errors, plus
    any acceptance criterion marked needs-clarification."""
    errors = structure_errors(contract)
    if errors:
        return errors
    assert isinstance(contract, Mapping)
    for i, c in enumerate(contract["acceptance_criteria"]):
        if c["status"] == "needs-clarification":
            errors.append(
                f"acceptance_criteria[{i}] ({c['id']}) needs clarification: {c['clarification']}"
            )
    return errors


def structure_errors(contract: object) -> list[str]:
    """Deterministic structural checks only (the schema). Empty if well formed."""
    if not isinstance(contract, Mapping):
        return ["contract must be a JSON object"]
    errors = _plain_json_errors(contract, "contract")
    if errors:
        return errors

    keys = set(contract)
    for k in REQUIRED:
        if k not in keys:
            errors.append(f"{k} is required")
    for k in sorted(keys - set(REQUIRED) - set(OPTIONAL)):
        errors.append(f"{k} is not a contract field")

    def has(k: str) -> bool:
        return k in contract

    if has("format") and contract["format"] != FORMAT:
        errors.append(f"format must be {FORMAT!r}")
    if has("task_id"):
        try:
            TaskId(contract["task_id"])  # type: ignore[arg-type]
        except (ValueError, TypeError):
            errors.append(
                "task_id must be lowercase letters, digits and single hyphens, at most 64 chars"
            )
    if has("version"):
        _text(errors, contract["version"], "version", max_len=40)
    if has("goal"):
        _text(errors, contract["goal"], "goal")
    if has("inputs"):
        _list(errors, contract["inputs"], "inputs", _text, allow_empty=True)
    if has("repository") and not (
        isinstance(contract["repository"], str) and _REPO_RE.fullmatch(contract["repository"])
    ):
        errors.append("repository must be 'owner/name'")
    if has("base_commit"):
        _commit(errors, contract["base_commit"], "base_commit")
    if has("permitted_paths"):
        _list(errors, contract["permitted_paths"], "permitted_paths", _path)
    if has("permitted_actions"):
        _list(errors, contract["permitted_actions"], "permitted_actions", _enum(ACTIONS))
    if has("risk_markers"):
        _list(
            errors, contract["risk_markers"], "risk_markers", _enum(RISK_MARKERS), allow_empty=True
        )
    if has("verification_commands"):
        _list(errors, contract["verification_commands"], "verification_commands", _text)
    if has("acceptance_criteria"):
        _criteria(errors, contract)
    if has("attempt_budget"):
        b = contract["attempt_budget"]
        if isinstance(b, bool) or not isinstance(b, int) or not 1 <= b <= MAX_ATTEMPT_BUDGET:
            errors.append(f"attempt_budget must be a whole number from 1 to {MAX_ATTEMPT_BUDGET}")
    if has("escalate_to") and not (
        isinstance(contract["escalate_to"], str) and _LOGIN_RE.fullmatch(contract["escalate_to"])
    ):
        errors.append("escalate_to must be a GitHub login")
    if has("depends_on"):
        _list(errors, contract["depends_on"], "depends_on", _dependency, allow_empty=True)
    if has("notes"):
        _text(errors, contract["notes"], "notes")

    actions = contract.get("permitted_actions")
    markers = contract.get("risk_markers")
    if isinstance(actions, list | tuple) and isinstance(markers, list | tuple):
        for action, marker in ACTION_NEEDS_MARKER.items():
            if action in actions and marker not in markers:
                errors.append(f"permitted action {action} needs risk marker {marker}")
    return errors


# --- Field checks ---------------------------------------------------------------


def _text(errors: list[str], value: object, where: str, max_len: int = _MAX_TEXT) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where} must be non-empty text")
    elif len(value) > max_len:
        errors.append(f"{where} is longer than {max_len} characters")


def _commit(errors: list[str], value: object, where: str) -> None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        errors.append(
            f"{where} must be a full 40-character lowercase commit id, not a branch or tag"
        )


def _path(errors: list[str], value: object, where: str) -> None:
    if not isinstance(value, str) or not _PATH_CHARS_RE.fullmatch(value):
        errors.append(f"{where} must be a repo-relative path or glob (letters, digits, . _ - * /)")
        return
    parts = value.split("/")
    if value.startswith("/") or "" in parts or ".." in parts or "." in parts:
        errors.append(f"{where} must be a repo-relative path with no empty, '.' or '..' parts")


def _enum(allowed: tuple[str, ...]):
    def check(errors: list[str], value: object, where: str) -> None:
        if value not in allowed:
            errors.append(f"{where} must be one of {list(allowed)}, got {value!r}")

    return check


def _dependency(errors: list[str], value: object, where: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{where} must be an object")
        return
    if set(value) != {"repository", "commit", "reason"}:
        errors.append(f"{where} must have exactly the keys commit, reason and repository")
        return
    if not (isinstance(value["repository"], str) and _REPO_RE.fullmatch(value["repository"])):
        errors.append(f"{where}.repository must be 'owner/name'")
    _commit(errors, value["commit"], f"{where}.commit")
    _text(errors, value["reason"], f"{where}.reason")


def _list(errors, value, where, check, allow_empty: bool = False) -> None:
    if not isinstance(value, list | tuple):
        errors.append(f"{where} must be a list")
        return
    if not value and not allow_empty:
        errors.append(f"{where} must not be empty")
    seen = []
    for i, item in enumerate(value):
        check(errors, item, f"{where}[{i}]")
        if item in seen:
            errors.append(f"{where}[{i}] repeats an earlier entry")
        seen.append(item)


_EVIDENCE_FIELDS = {
    "automated-check": ("command",),
    "observable-behavior": ("steps", "expected"),
    "human-review": ("reviewer", "question"),
}


def _criteria(errors: list[str], contract: Mapping[str, object]) -> None:
    value = contract["acceptance_criteria"]
    if not isinstance(value, list | tuple):
        errors.append("acceptance_criteria must be a list")
        return
    if not value:
        errors.append("acceptance_criteria must not be empty")
    commands = contract.get("verification_commands")
    commands = commands if isinstance(commands, list | tuple) else ()
    ids = []
    for i, c in enumerate(value):
        where = f"acceptance_criteria[{i}]"
        if not isinstance(c, Mapping):
            errors.append(f"{where} must be an object")
            continue
        allowed = {"id", "statement", "evidence", "status", "clarification"}
        for k in ("id", "statement", "evidence", "status"):
            if k not in c:
                errors.append(f"{where}.{k} is required")
        for k in sorted(set(c) - allowed):
            errors.append(f"{where}.{k} is not a criterion field")
        if "id" in c:
            if not (isinstance(c["id"], str) and _CRITERION_ID_RE.fullmatch(c["id"])):
                errors.append(f"{where}.id must look like ac1, ac2, ...")
            elif c["id"] in ids:
                errors.append(f"{where}.id {c['id']} is used twice")
            else:
                ids.append(c["id"])
        if "statement" in c:
            _text(errors, c["statement"], f"{where}.statement")
        status = c.get("status")
        if "status" in c and status not in STATUSES:
            errors.append(f"{where}.status must be one of {list(STATUSES)}")
        if status == "needs-clarification":
            if "clarification" not in c:
                errors.append(f"{where}.clarification is required when it needs clarification")
            else:
                _text(errors, c["clarification"], f"{where}.clarification")
        elif "clarification" in c and status == "ready":
            errors.append(f"{where}.clarification is only allowed when it needs clarification")
        if "evidence" in c:
            _evidence(errors, c["evidence"], f"{where}.evidence", commands)


def _evidence(errors: list[str], ev: object, where: str, commands) -> None:
    if not isinstance(ev, Mapping):
        errors.append(f"{where} must be an object")
        return
    kind = ev.get("type")
    if not isinstance(kind, str) or kind not in _EVIDENCE_FIELDS:
        errors.append(f"{where}.type must be one of {list(EVIDENCE_TYPES)}, got {kind!r}")
        return
    need = _EVIDENCE_FIELDS[kind]
    for k in need:
        if k not in ev:
            errors.append(f"{where}.{k} is required for {kind} evidence")
    for k in sorted(set(ev) - {"type", *need}):
        errors.append(f"{where}.{k} is not a field of {kind} evidence")
    for k in need:
        if k in ev:
            _text(errors, ev[k], f"{where}.{k}")
    if kind == "automated-check" and isinstance(ev.get("command"), str):
        if ev["command"] not in commands:
            errors.append(f"{where}.command must be one of verification_commands")
    if kind == "human-review" and "reviewer" in ev:
        if not (isinstance(ev["reviewer"], str) and _LOGIN_RE.fullmatch(ev["reviewer"])):
            errors.append(f"{where}.reviewer must be a GitHub login")


# --- Plain JSON -----------------------------------------------------------------


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates
        return False
    return True


def _plain_json_errors(value: object, where: str, depth: int = 0) -> list[str]:
    """Why ``value`` is not plain JSON that canonical_bytes can encode; empty if it is.

    Bounds nesting and integer size so nothing later can raise on odd input.
    """
    if depth > _MAX_DEPTH:
        return [f"{where} is nested more than {_MAX_DEPTH} levels deep"]
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [] if _encodable(value) else [f"{where} is not valid Unicode text"]
    if isinstance(value, int):
        return [] if abs(value) < 2**63 else [f"{where} is too large a number"]
    if isinstance(value, float):
        return [f"{where} is a float; contracts use only whole numbers"]
    if isinstance(value, Mapping):
        errs = []
        for k, v in value.items():
            if not isinstance(k, str) or not _encodable(k):
                errs.append(f"{where} has a key that is not valid text")
            else:
                errs += _plain_json_errors(v, f"{where}.{k}", depth + 1)
        return errs
    if isinstance(value, list | tuple):
        errs = []
        for i, v in enumerate(value):
            errs += _plain_json_errors(v, f"{where}[{i}]", depth + 1)
        return errs
    return [f"{where} is a {type(value).__name__}, not a JSON value"]


def _require_plain_json(value: object, where: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a JSON object")
    errs = _plain_json_errors(value, where)
    if errs:
        raise ValueError("; ".join(errs))


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(v) for v in value]
    return value
