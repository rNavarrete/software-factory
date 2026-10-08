"""The real cloud Routine adapter (ENG-182).

Starts one factory worker with one POST to the routine's ``/fire`` endpoint
(https://platform.claude.com/docs/en/api/claude-code/routines-fire, checked
2026-10-08) and reports what is known about the launch. It never retries and
never treats a launch as completion: the run's outcome is the marker branch
and PR on GitHub (ADR 0002 section 6).

The fire ``text`` is a JSON envelope the routine's saved prompt checks again
(defense in depth). ``build_fire_text`` makes it; ``RoutineAdapter.launch``
re-checks it, together with ``controller.contract.validate`` (ENG-144), and
refuses before any network call if anything is wrong.

The start key is read from macOS Keychain for each launch and never stored,
logged or returned.
"""

from __future__ import annotations

import email.utils
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from controller import contract as contract_format
from controller.interfaces import (
    LAUNCH_TIMEOUT_SECONDS,
    MAX_FIRE_TEXT_CHARS,
    NOT_LAUNCHED_STATUSES,
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchRequest,
    LaunchResult,
)

FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{}/fire"
API_VERSION = "2023-06-01"
"""The only value the fire API accepts."""
BETA = "experimental-cc-routine-2026-04-01"
"""Optional per the API reference; sent anyway (ADR 0002 section 9). Any change
to either header is a requalification trigger."""

KEYCHAIN_SERVICE = "software-factory"
ENVELOPE_VERSION = 1
ENVELOPE_KEYS = frozenset(
    {"factory_payload", "contract", "contract_digest", "attempt", "branch", "pr_title"}
)
CONTRACT_REQUIRED = ("task_id", "base_commit", "permitted_paths", "acceptance_criteria")
MAX_ATTEMPT = 3  # docs/limits.md section 3

_TRIG_ID_RE = re.compile(r"^trig_[A-Za-z0-9]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

ContractValidator = Callable[[Mapping[str, Any], ContractDigest], list[str]]
"""controller/contract/'s ``validate(contract, digest) -> list[str]``: no errors
means the contract is well formed and ``digest`` is its canonical digest."""


class PayloadRejected(ValueError):
    """The fire text failed validation. Nothing was sent."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# --- Payload --------------------------------------------------------------------


def envelope_errors(envelope: object, digest: ContractDigest, attempt: AttemptId) -> list[str]:
    """Structural checks on the fire envelope. The routine prompt repeats them."""
    if not isinstance(envelope, dict):
        return ["fire text is not a JSON object"]
    errors = []
    if set(envelope) != ENVELOPE_KEYS:
        errors.append(f"envelope keys must be exactly {sorted(ENVELOPE_KEYS)}")
    if envelope.get("factory_payload") != ENVELOPE_VERSION:
        errors.append(f"factory_payload must be {ENVELOPE_VERSION}")
    if envelope.get("contract_digest") != digest.value:
        errors.append("contract_digest does not match the request's digest")
    if envelope.get("attempt") != attempt.number or not 1 <= attempt.number <= MAX_ATTEMPT:
        errors.append(f"attempt must be {attempt.number} and at most {MAX_ATTEMPT}")
    if envelope.get("branch") != attempt.branch:
        errors.append(f"branch must be {attempt.branch}")
    if envelope.get("pr_title") != pr_title(attempt, digest):
        errors.append(f"pr_title must be exactly {pr_title(attempt, digest)!r}")

    contract = envelope.get("contract")
    if not isinstance(contract, dict):
        return errors + ["contract must be an object"]
    missing = [k for k in CONTRACT_REQUIRED if k not in contract]
    if missing:
        errors.append(f"contract is missing {missing}")
    if contract.get("task_id") != str(attempt.task):
        errors.append(f"contract.task_id must be {attempt.task}")
    base = contract.get("base_commit")
    if "base_commit" in contract and not (isinstance(base, str) and _COMMIT_RE.match(base)):
        errors.append("contract.base_commit must be a 40-character commit id")
    for key in ("permitted_paths", "acceptance_criteria"):
        if key in contract and not (isinstance(contract[key], list) and contract[key]):
            errors.append(f"contract.{key} must be a non-empty list")
    return errors


def pr_title(attempt: AttemptId, digest: ContractDigest) -> str:
    """The worker's PR title: the marker, then the task id.

    Nothing else goes in it. Free text there would not be covered by the
    contract digest, so it could carry instructions or a forged second line
    that nobody approved."""
    return f"{attempt.pr_title_marker(digest)} {attempt.task}"


def build_fire_text(
    contract: Mapping[str, Any],
    digest: ContractDigest,
    attempt: AttemptId,
    *,
    validate_contract: ContractValidator = contract_format.validate,
) -> str:
    """The fire text for one attempt. Raises PayloadRejected rather than return
    anything the adapter or the worker would refuse.

    ``contract`` may be a plain dict or the read-only form that
    ``controller.contract.loads`` and ``freeze`` return."""
    try:
        plain = json.loads(contract_format.canonical_bytes(contract))
    except (TypeError, ValueError) as e:
        raise PayloadRejected([f"contract is not plain JSON: {e}"]) from None
    envelope = {
        "factory_payload": ENVELOPE_VERSION,
        "contract": plain,
        "contract_digest": digest.value,
        "attempt": attempt.number,
        "branch": attempt.branch,
        "pr_title": pr_title(attempt, digest),
    }
    text = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    errors = _text_errors(text, digest, attempt, validate_contract)
    if errors:
        raise PayloadRejected(errors)
    return text


def _text_errors(
    text: str, digest: ContractDigest, attempt: AttemptId, validate_contract: ContractValidator
) -> list[str]:
    if len(text) > MAX_FIRE_TEXT_CHARS:
        return [f"fire text is {len(text)} chars, limit {MAX_FIRE_TEXT_CHARS}"]
    try:
        contract_format.loads(text)  # refuses duplicate keys, NaN and Infinity
        envelope = json.loads(text)
    except ValueError as e:
        return [f"fire text is not plain JSON: {e}"]
    errors = envelope_errors(envelope, digest, attempt)
    contract = envelope.get("contract") if isinstance(envelope, dict) else None
    if isinstance(contract, dict):
        errors += list(validate_contract(contract, digest))
    return errors


# --- Start key ------------------------------------------------------------------


def keychain_key(trig_id: str, run: Callable[..., Any] = subprocess.run) -> str:
    """Read the routine's start key from macOS Keychain
    (service ``software-factory``, account ``routine-token/<trig_id>``)."""
    out = run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            f"routine-token/{trig_id}",
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise LookupError(f"no Keychain item {KEYCHAIN_SERVICE} / routine-token/{trig_id}")
    return out.stdout.strip()


def _scrub(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[redacted]")
    return _KEY_RE.sub("[redacted]", text)


# --- Fire -----------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The fire API never redirects; following one would re-send or change the POST."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def parse_retry_after(value: str | None, now: float) -> int | None:
    """Retry-After as whole seconds from ``now``: delta-seconds or an HTTP date.
    None when absent or unreadable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0, int(when.timestamp() - now + 0.999))


class RoutineAdapter:
    """RuntimeAdapter for one routine. One POST per ``launch``, no retries."""

    def __init__(
        self,
        trig_id: str,
        validate_contract: ContractValidator = contract_format.validate,
        *,
        start_key: Callable[[str], str] = keychain_key,
        url: str = FIRE_URL,
        timeout: float = LAUNCH_TIMEOUT_SECONDS,
        now: Callable[[], float] = time.time,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if not _TRIG_ID_RE.match(trig_id):
            raise ValueError("routine id must look like trig_...")
        self.trig_id = trig_id
        self._validate_contract = validate_contract
        self._start_key = start_key
        self._url = url.format(trig_id)
        self._timeout = timeout
        self._now = now
        self._opener = opener or urllib.request.build_opener(_NoRedirect)

    def launch(self, request: LaunchRequest) -> LaunchResult:
        """Raises PayloadRejected (before sending) for an invalid request and
        LookupError for a missing start key. Every network, timeout or parse
        failure is returned as OUTCOME_UNKNOWN."""
        attempt = request.run.attempt
        errors = _text_errors(request.text, request.digest, attempt, self._validate_contract)
        if errors:
            raise PayloadRejected(errors)
        key = self._start_key(self.trig_id)
        http_request = urllib.request.Request(
            self._url,
            data=json.dumps({"text": request.text}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "anthropic-version": API_VERSION,
                "anthropic-beta": BETA,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(http_request, timeout=self._timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            return self._from_http_error(e, key)
        except BaseException as e:  # timeout, reset, TLS, Ctrl-C: a session may still start
            result = LaunchResult(
                LaunchOutcome.OUTCOME_UNKNOWN, detail=_scrub(f"{type(e).__name__}: {e}", key)
            )
            if isinstance(e, KeyboardInterrupt):
                raise LaunchInterrupted(result) from e
            return result
        return _from_response(status, raw, key)

    def _from_http_error(self, e: urllib.error.HTTPError, key: str) -> LaunchResult:
        try:
            body = _scrub(e.read().decode(errors="replace"), key)
        except Exception:
            body = None
        retry = None
        detail = f"HTTP {e.code}"
        if e.code == 429:
            retry = parse_retry_after(
                e.headers.get("Retry-After") if e.headers else None, self._now()
            )
            if retry is None:
                detail += "; Retry-After missing or unreadable"
        outcome = (
            LaunchOutcome.NOT_LAUNCHED
            if e.code in NOT_LAUNCHED_STATUSES
            else LaunchOutcome.OUTCOME_UNKNOWN
        )
        return LaunchResult(
            outcome,
            http_status=e.code,
            retry_after_seconds=retry,
            response_body=body,
            detail=detail,
        )


def _from_response(status: int, raw: bytes, key: str) -> LaunchResult:
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("type") == "routine_fire":
        session_id = body.get("claude_code_session_id")
        session_url = body.get("claude_code_session_url")
        if status == 200 and isinstance(session_id, str) and session_id and session_url:
            return LaunchResult(
                LaunchOutcome.LAUNCHED,
                http_status=200,
                session_id=session_id,
                session_url=str(session_url),
            )
    return LaunchResult(
        LaunchOutcome.OUTCOME_UNKNOWN,
        http_status=status,
        response_body=_scrub(raw.decode(errors="replace")[:2000], key),
        detail="no session id in the response",
    )


class LaunchInterrupted(KeyboardInterrupt):
    """Ctrl-C during a fire. ``result`` is the OUTCOME_UNKNOWN to record."""

    def __init__(self, result: LaunchResult) -> None:
        super().__init__("fire interrupted; launch outcome unknown")
        self.result = result


__all__ = [
    "API_VERSION",
    "FIRE_URL",
    "ContractValidator",
    "LaunchInterrupted",
    "PayloadRejected",
    "RoutineAdapter",
    "build_fire_text",
    "envelope_errors",
    "keychain_key",
    "parse_retry_after",
    "pr_title",
]
