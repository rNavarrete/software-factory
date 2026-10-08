"""Rolando's approval, bound to one exact contract (ENG-151, ADR 0001 section 5).

Every decision this module writes is a ledger event signed with an operator key
kept in the macOS Keychain of Rolando's login, and is written only after he
types a code at a terminal. Nothing here reads PR text, comments, worker
output or any network input, so none of those can make an approval.

What gets signed: who decided, when, the OS user, the exact contract digest, a
fresh decision id, the decision and its scope, the expiry, the bound fields
(repository, base commit, paths, actions, budget, version), and which attempt
or run it covers. Free text (the failure a repair names, clearing evidence,
notes) is stored beside it but not signed, because the ledger redacts
secret-looking text before writing and that must not break the signature.

Rules ``Approvals.check`` applies at dispatch, reading the ledger afresh:

- Only the newest signed approval of a task counts; approving a revised
  contract retires the old version.
- A rejection or revocation of a contract cancels every approval of it made
  before it; a fresh approval afterwards is needed.
- A signed record counts once: a copy appended later (same decision id) is
  ignored, so an old approval can't be replayed past a revocation.
- Attempt 1 needs the approval. Attempt n needs, besides, a signed repair
  go-ahead for exactly attempt n on exactly this contract, made after attempt
  n-1 started, and n within the contract's budget.
- Repair, re-fire and clearing records count only if signed, bound to the
  contract their attempt ran, and made after what they decide on (a clearing
  names the exact fire it clears).

Threat model: the ledger lives on Rolando's Mac and only the controller writes
it. The signature stops anything that can append records without the operator
key (a bug, a future callback, a copied record) from creating or stretching a
decision. It does not protect the records the gate itself writes (fire results,
reservations); something able to forge those already controls the controller.

Approving a contract authorizes dispatch only. It never approves the PR, a
merge or a release (ADR 0001 section 5 items 2 and 3); the release gate is the
pilot repo's ``release`` environment (ENG-142, ENG-143).

Standard library only.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO

from controller import contract as contracts
from controller.attempts import events as ev
from controller.attempts.policy import LedgerView, Notice, check_dispatch
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LedgerEvent,
    LedgerStore,
    RunId,
    StoredEvent,
    TaskId,
)

APPROVER = "rNavarrete"
"""The only person who may approve in v1 (ADR 0001 section 2)."""

HUMAN_DECISION = "human-decision"
"""The ledger's kind for a person's decision bound to a digest (controller/ledger/kinds.py)."""

SCOPE = "contract-dispatch"
"""The only scope this module writes or reads. It authorizes dispatching the
worker on the contract, nothing else: no PR approval, merge or release."""

APPROVED, REJECTED, REVOKED = "approved", "rejected", "revoked"

DEFAULT_TTL = timedelta(days=3)
MAX_TTL = timedelta(days=14)

KEYCHAIN_SERVICE = "software-factory-approval"

_MAC_DOMAIN = b"software-factory/approval-mac/v1\n"
_COMMON = ("identity", "os_user", "decided_at", "digest", "decision_id", "key_id")
_SIGNED_FIELDS = {
    HUMAN_DECISION: (
        *_COMMON,
        "scope",
        "decision",
        "digest_type",
        "expires_at",
        "binding_sha256",
    ),
    ev.REPAIR_AUTHORIZED: _COMMON,
    ev.REFIRE_AUTHORIZED: _COMMON,
    ev.ATTEMPT_CLEARED: (*_COMMON, "basis"),
}
_URL_BASES = (ev.ClearingBasis.COMPLETED, ev.ClearingBasis.TERMINATED)

# Gate blocks that depend on Rolando's decisions; check() reports these, read
# from the ledger with every unauthenticated decision left out.
_GATE_CODES = frozenset(
    {
        "unresolved-attempt",
        "repair-not-authorized",
        "refire-not-authorized",
        "refire-not-allowed",
        "attempt-cap",
    }
)


class ApprovalRefused(Exception):
    """An approval action was not confirmed or is not allowed. Nothing was written."""


# --- The operator key ------------------------------------------------------------


class ApprovalKey(Protocol):
    """Signs decision records. Only Rolando's login can read the real key."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str: ...


class StaticKey:
    """A key held in memory. Used by tests and by KeychainKey once loaded."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("an approval key needs at least 32 bytes")
        self._secret = bytes(secret)
        self._id = hashlib.sha256(b"software-factory approval key id\0" + self._secret).hexdigest()

    @property
    def key_id(self) -> str:
        """A public fingerprint of the key, recorded on every decision."""
        return self._id[:16]

    def sign(self, payload: bytes) -> str:
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return f"StaticKey(key_id={self.key_id!r})"


class KeychainKey:
    """The operator key from the macOS Keychain (64 hex chars, 32 bytes).

    Create it once, on the Mac, with::

        security add-generic-password -s software-factory-approval -a "$USER" \
            -T "" -w "$(openssl rand -hex 32)"

    ``-T ""`` trusts no program, so macOS asks Rolando to allow every read of the
    key (once per controller run, since the key is then kept in memory).
    Without it, any program running as his login could read the key silently.
    """

    def __init__(
        self,
        account: str | None = None,
        service: str = KEYCHAIN_SERVICE,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._account = account or getpass.getuser()
        self._service = service
        self._run = run
        self._key: StaticKey | None = None

    def _load(self) -> StaticKey:
        if self._key is None:
            result = self._run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    self._account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            secret = result.stdout.strip() if result.returncode == 0 else ""
            if len(secret) != 64 or any(c not in "0123456789abcdef" for c in secret):
                raise ApprovalRefused(
                    f"no usable approval key in the Keychain (service {self._service!r},"
                    f" account {self._account!r})"
                )
            self._key = StaticKey(bytes.fromhex(secret))
        return self._key

    @property
    def key_id(self) -> str:
        return self._load().key_id

    def sign(self, payload: bytes) -> str:
        return self._load().sign(payload)

    def __repr__(self) -> str:
        return f"KeychainKey(service={self._service!r}, account={self._account!r})"


# --- Confirmation at the terminal -----------------------------------------------

Confirm = Callable[[str, str], bool]
"""``confirm(summary, code)``: show the summary; True only if the person typed ``code``."""


def tty_confirm(
    summary: str, code: str, *, stdin: TextIO | None = None, out: TextIO | None = None
) -> bool:
    """Ask at the terminal. Refuses piped input outright.

    This shows a person is at a terminal; it cannot tell a person from a script
    driving a pseudo-terminal. The Keychain prompt is what proves it is Rolando.
    """
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stderr if out is None else out
    try:
        if not stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    out.write(f"{summary}\n\nType {code} to confirm, anything else to cancel: ")
    out.flush()
    typed = stdin.readline().strip()
    return hmac.compare_digest(typed.encode(), code.encode())


# --- Signing ----------------------------------------------------------------------


def _binding_sha256(binding: object) -> str | None:
    if not isinstance(binding, Mapping):
        return None
    return hashlib.sha256(contracts.canonical_bytes(binding)).hexdigest()


def _payload(
    kind: str,
    task: TaskId | None,
    attempt: AttemptId | None,
    run: RunId | None,
    data: Mapping[str, object],
) -> bytes | None:
    """The signed bytes for an event, or None if a signed field is missing."""
    names = _SIGNED_FIELDS.get(kind)
    if names is None or any(n not in data for n in names):
        return None
    fields = {n: data[n] for n in names}
    if not all(v is None or isinstance(v, str) for v in fields.values()):
        return None
    body = {
        "kind": kind,
        "task": None if task is None else str(task),
        "attempt": None if attempt is None else str(attempt),
        "run": None if run is None else str(run),
        "fields": fields,
    }
    # ASCII escapes keep any string, even an unpaired surrogate, encodable.
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _MAC_DOMAIN + text.encode("ascii")


def _sign(event: LedgerEvent, key: ApprovalKey) -> LedgerEvent:
    data = {**event.data, "key_id": key.key_id}
    data.setdefault("decision_id", secrets.token_hex(16))
    if event.kind == HUMAN_DECISION:
        data["binding_sha256"] = _binding_sha256(data.get("binding"))
    payload = _payload(event.kind, event.task, event.attempt, event.run, data)
    assert payload is not None, "a signed field is missing"
    data["mac"] = key.sign(payload)
    return LedgerEvent(event.kind, event.at, event.task, event.attempt, event.run, data)


def authentic(
    event: LedgerEvent, keys: ApprovalKey | Sequence[ApprovalKey], approvers: frozenset[str]
) -> bool:
    """True if ``event`` was signed with one of ``keys`` and names one of
    ``approvers``. Never raises: a malformed record is simply not authentic."""
    keyring = {k.key_id: k for k in ((keys,) if hasattr(keys, "sign") else keys)}  # type: ignore[union-attr]
    d = event.data
    try:
        mac, key_id, identity = d.get("mac"), d.get("key_id"), d.get("identity")
        if not (isinstance(mac, str) and isinstance(key_id, str) and isinstance(identity, str)):
            return False
        if key_id not in keyring or identity not in approvers:
            return False
        decision_id = d.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            return False
        if _time(d.get("decided_at")) is None:
            return False
        if event.kind == HUMAN_DECISION:
            binding = d.get("binding")
            if d.get("binding_sha256") != _binding_sha256(binding):
                return False
            if isinstance(binding, Mapping) and binding.get("digest") != d.get("digest"):
                return False
        payload = _payload(event.kind, event.task, event.attempt, event.run, d)
        if payload is None:
            return False
        return hmac.compare_digest(keyring[key_id].sign(payload), mac)
    except (TypeError, ValueError, UnicodeError):
        return False


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("times must be timezone-aware")


# --- The dispatch verdict ---------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Whether Rolando's decisions allow firing ``run`` for this exact contract.

    This covers approvals only. Holds, usage, rate limits and caps are the
    attempt gate's, so ``approved`` does not mean the fire will happen.
    """

    digest: ContractDigest | None
    run: RunId | None
    """The run dispatch would fire, if the decisions allow it."""
    blocks: tuple[Notice, ...]

    @property
    def approved(self) -> bool:
        return not self.blocks and self.run is not None


class Approvals:
    """Rolando's decisions about contracts: write them, and check them at dispatch.

    ``store`` is the ledger. ``key`` signs and verifies (KeychainKey on the Mac);
    ``retired_keys`` only verify, so records signed before a key change still
    count. ``confirm`` asks Rolando to type a code before anything is written.
    ``contracts`` (optional) keeps each approved contract's exact bytes.
    """

    def __init__(
        self,
        store: LedgerStore,
        key: ApprovalKey,
        *,
        confirm: Confirm = tty_confirm,
        identity: str = APPROVER,
        os_user: str | None = None,
        approvers: frozenset[str] = frozenset({APPROVER}),
        retired_keys: Sequence[ApprovalKey] = (),
        contracts: ContractStore | None = None,
    ) -> None:
        if identity not in approvers:
            raise ValueError(f"{identity!r} is not an approver")
        self._store = store
        self._key = key
        self._keys = (key, *retired_keys)
        self._confirm = confirm
        self._identity = identity
        self._os_user = os_user or getpass.getuser()
        self._approvers = approvers
        self._contracts = contracts

    # --- writing decisions ---

    def approve(
        self, contract: Mapping[str, object], now: datetime, *, ttl: timedelta = DEFAULT_TTL
    ) -> StoredEvent:
        """Approve dispatching exactly this contract until ``now + ttl``. Any
        earlier approval of another version of the task stops counting."""
        _aware(now)
        errors = contracts.approval_errors(contract)
        if errors:
            raise ApprovalRefused("contract can't be approved: " + "; ".join(errors))
        contract = contracts.freeze(contract)
        if not timedelta(0) < ttl <= MAX_TTL:
            raise ApprovalRefused(f"an approval lasts more than 0 and at most {MAX_TTL}")
        try:
            expires = now + ttl
        except OverflowError:
            raise ApprovalRefused("expiry is past the end of the calendar") from None
        bound = contracts.binding(contract)
        digest = ContractDigest(str(bound["digest"]))
        task = TaskId(str(contract["task_id"]))
        summary = _contract_summary("Approve dispatch of this contract", bound, expires)
        self._ask(summary, digest.short)
        if self._contracts is not None and self._contracts.save(contract) != digest:
            raise ApprovalRefused("the stored contract does not match the approved digest")
        data = self._common(digest, now) | {
            "scope": SCOPE,
            "decision": APPROVED,
            "digest_type": "contract",
            "expires_at": expires.isoformat(),
            "binding": bound,
        }
        return self._append(LedgerEvent(HUMAN_DECISION, now, task, data=data))

    def reject(self, contract: Mapping[str, object], reason: str, now: datetime) -> StoredEvent:
        """Reject this exact contract. Any earlier approval of it stops counting."""
        _aware(now)
        bound = contracts.binding(contract)
        digest = ContractDigest(str(bound["digest"]))
        return self._withdraw(TaskId(str(bound["task_id"])), digest, REJECTED, reason, now)

    def revoke(
        self, task: TaskId, digest: ContractDigest, reason: str, now: datetime
    ) -> StoredEvent:
        """Withdraw every earlier approval of this contract. A later fresh
        approval is needed to dispatch it again."""
        _aware(now)
        return self._withdraw(task, digest, REVOKED, reason, now)

    def authorize_repair(
        self, contract: Mapping[str, object], number: int, failure: str, now: datetime
    ) -> StoredEvent:
        """Go-ahead for repair attempt ``number`` (2 or more) on this exact
        contract, naming the failure of the attempt before it."""
        _aware(now)
        bound = contracts.binding(contract)
        digest = ContractDigest(str(bound["digest"]))
        attempt = AttemptId(TaskId(str(bound["task_id"])), number)
        budget = bound["attempt_budget"]
        assert isinstance(budget, int)
        if number < 2:
            raise ApprovalRefused("attempt 1 needs the contract approval, not a repair")
        if number > budget:
            raise ApprovalRefused(f"attempt {number} is over this contract's budget of {budget}")
        if not failure.strip():
            raise ApprovalRefused("a repair names the failure it repairs")
        view = LedgerView.build(self._store.events())
        prior = AttemptId(attempt.task, number - 1)
        if prior not in view.attempts:
            raise ApprovalRefused(f"{prior} was never started, so there is no failure to repair")
        summary = (
            f"Authorize repair attempt {attempt} on contract {digest.value}.\n"
            f"Failure of {prior}: {failure}"
        )
        self._ask(summary, digest.short)
        base = ev.repair_authorized(attempt, failure, self._identity, now)
        return self._append(_extend(base, self._common(digest, now)))

    def authorize_refire(
        self, contract: Mapping[str, object], prior: RunId, now: datetime
    ) -> StoredEvent:
        """Decision to fire ``prior``'s attempt again after it came back
        definitely not launched."""
        _aware(now)
        digest = contracts.digest(contract)
        view = LedgerView.build(self._store.events())
        state = view.attempts.get(prior.attempt)
        last = None if state is None else state.last_fire
        if state is None or last is None or last.run != prior:
            raise ApprovalRefused(f"{prior} is not the latest recorded fire of its attempt")
        if last.outcome is not LaunchOutcome.NOT_LAUNCHED:
            raise ApprovalRefused(f"{prior} was not definitely not-launched; it can't be re-fired")
        if state.digest != digest.value:
            raise ApprovalRefused(f"{prior.attempt} ran a different contract")
        summary = f"Fire {prior.attempt} again after {prior} was not launched ({digest.value})."
        self._ask(summary, digest.short)
        base = ev.refire_authorized(prior, self._identity, now)
        return self._append(_extend(base, self._common(digest, now)))

    def record_clearing(
        self, attempt: AttemptId, basis: ev.ClearingBasis, evidence: str, now: datetime
    ) -> StoredEvent:
        """A clearing record (ADR 0002 section 6.1) for the latest fire of an
        attempt that may have left a session running. ``evidence`` is what
        ``basis`` needs: an https session URL for completed or terminated."""
        _aware(now)
        if not evidence.strip():
            raise ApprovalRefused("a clearing record needs its evidence")
        if basis in _URL_BASES and not evidence.startswith("https://"):
            raise ApprovalRefused(f"a {basis.value} clearing needs the session's https URL")
        view = LedgerView.build(self._store.events())
        state = view.attempts.get(attempt)
        if state is None or not state.digest or state.last_fire is None:
            raise ApprovalRefused(f"{attempt} was never started")
        digest = ContractDigest(state.digest)
        run = state.last_fire.run
        summary = f"Clear {run} of contract {digest.value} ({basis.value}): {evidence}"
        self._ask(summary, digest.short)
        base = ev.attempt_cleared(attempt, basis, evidence, self._identity, now)
        data = {**base.data, **self._common(digest, now)}
        return self._append(LedgerEvent(base.kind, now, attempt.task, attempt, run, data))

    # --- checking at dispatch ---

    def check(
        self, contract: Mapping[str, object], now: datetime, *, refire_of: RunId | None = None
    ) -> Verdict:
        """May dispatch fire this exact contract now, as far as Rolando's
        decisions go? Reads the ledger afresh.

        Without ``refire_of`` this is the task's next attempt; with it, firing
        that run's attempt again. Holds, usage and rate limits are left to
        ``AttemptGate.reserve``, which dispatch must still call, with the same
        digest and ``refire_of``, and whose returned run must equal
        ``verdict.run``. The two calls are not one transaction: a decision
        recorded between them is seen at the next dispatch, not this one.
        """
        _aware(now)
        errors = contracts.approval_errors(contract)
        if errors:
            return Verdict(None, None, (Notice("contract-invalid", "; ".join(errors)),))
        contract = contracts.freeze(contract)
        digest = contracts.digest(contract)
        task = TaskId(str(contract["task_id"]))
        stored = self._store.events()
        blocks: list[Notice] = []

        standing = self._approval_block(stored, task, digest, now)
        if standing is not None:
            blocks.append(standing)

        trusted = self.trusted_events(stored, digest)
        decision = check_dispatch(trusted, task, digest, now, refire_of=refire_of)
        blocks += [b for b in decision.blocks if b.code in _GATE_CODES]
        run = decision.run
        budget = contract["attempt_budget"]
        if run is not None and isinstance(budget, int) and run.attempt.number > budget:
            blocks.append(
                Notice(
                    "over-budget",
                    f"Attempt {run.attempt.number} is over this contract's budget of {budget};"
                    " going on needs a fresh approval with a new budget.",
                )
            )
        return Verdict(digest, run, tuple(blocks))

    def trusted_events(
        self, stored: Sequence[StoredEvent], digest: ContractDigest
    ) -> list[StoredEvent]:
        """The ledger with every repair, re-fire and clearing decision left out
        unless it is signed, counted once, bound to the right contract, and made
        after what it decides on. ``digest`` is the contract being dispatched:
        repairs only count for it."""
        reserved: dict[AttemptId, str] = {}
        latest_fire: dict[AttemptId, RunId] = {}
        results: set[RunId] = set()
        out = []
        for item, signed in self._signed(stored):
            e = item.event
            d = e.data
            keep = True
            if e.kind == ev.ATTEMPT_RESERVED and e.attempt is not None:
                reserved.setdefault(e.attempt, str(d.get("digest", "")))
            elif e.kind == ev.FIRE_INTENT and e.run is not None:
                latest_fire[e.run.attempt] = e.run
            elif e.kind == ev.FIRE_RESULT and e.run is not None:
                results.add(e.run)
            elif e.kind == ev.REPAIR_AUTHORIZED:
                prior = None
                if e.attempt is not None and e.attempt.number >= 2:
                    prior = AttemptId(e.attempt.task, e.attempt.number - 1)
                keep = signed and prior in reserved and d.get("digest") == digest.value
            elif e.kind == ev.REFIRE_AUTHORIZED:
                keep = (
                    signed
                    and e.attempt in reserved
                    and e.run in results
                    and d.get("digest") == reserved[e.attempt]
                )
            elif e.kind == ev.ATTEMPT_CLEARED:
                keep = (
                    signed
                    and e.attempt in reserved
                    and e.run is not None
                    and latest_fire.get(e.attempt) == e.run
                    and d.get("digest") == reserved[e.attempt]
                )
            if keep:
                out.append(item)
        return out

    # --- internals ---

    def _signed(self, stored: Sequence[StoredEvent]) -> Iterator[tuple[StoredEvent, bool]]:
        """Every event in seq order, with whether it is a signed decision seen
        for the first time. A later copy of a signed decision is not signed."""
        seen: set[str] = set()
        for item in sorted(stored, key=lambda s: s.seq):
            e = item.event
            signed = e.kind in _SIGNED_FIELDS and authentic(e, self._keys, self._approvers)
            if signed:
                decision_id = str(e.data["decision_id"])
                signed = decision_id not in seen
                seen.add(decision_id)
            yield item, signed

    def _approval_block(
        self, stored: Sequence[StoredEvent], task: TaskId, digest: ContractDigest, now: datetime
    ) -> Notice | None:
        newest: str | None = None  # digest of the task's newest signed approval
        approvals: list[tuple[int, datetime, datetime]] = []
        withdrawals: list[tuple[int, datetime | None, str]] = []
        unsigned = False
        for item, signed in self._signed(stored):
            e = item.event
            d = e.data
            if e.kind != HUMAN_DECISION or e.task != task or d.get("scope") != SCOPE:
                continue
            ours = d.get("digest") == digest.value
            decision = d.get("decision")
            if decision in (REJECTED, REVOKED):
                if ours:
                    # Counted even if unsigned: a withdrawal can only stop dispatch.
                    # Only a signed one's own time counts, so an unsigned one can't
                    # reach forward and cancel approvals made after it.
                    at = _time(d.get("decided_at")) if signed else None
                    withdrawals.append((item.seq, at, str(decision)))
                continue
            if decision != APPROVED:
                continue
            decided, expires = _time(d.get("decided_at")), _time(d.get("expires_at"))
            if (
                not signed
                or decided is None
                or expires is None
                or not timedelta(0) < expires - decided <= MAX_TTL
            ):
                unsigned = unsigned or ours
                continue
            newest = str(d["digest"])
            if ours:
                approvals.append((item.seq, decided, expires))

        def withdrawn(seq: int, decided: datetime) -> bool:
            return any(
                w_seq > seq or (w_at is not None and decided <= w_at)
                for w_seq, w_at, _ in withdrawals
            )

        live = [
            (decided, expires) for seq, decided, expires in approvals if not withdrawn(seq, decided)
        ]
        if newest is not None and newest != digest.value:
            return Notice(
                "approval-for-different-contract",
                "Rolando's newest approval for this task is for a different version of the"
                " contract. Any change (content, scope, base commit or budget) needs a fresh"
                " approval.",
            )
        if any(decided <= now < expires for decided, expires in live):
            return None
        if any(now < decided for decided, _ in live):
            return Notice("approval-not-yet-valid", "The approval is dated after now.")
        if live:
            return Notice("approval-expired", "The approval has expired; approve it again.")
        if withdrawals:
            last = withdrawals[-1][2]
            return Notice(f"approval-{last}", f"Rolando {last} this exact contract.")
        if unsigned:
            return Notice(
                "approval-unauthenticated",
                "An approval record for this contract is not signed with the operator key.",
            )
        return Notice("approval-missing", f"No approval of contract {digest.value}.")

    def _withdraw(
        self, task: TaskId, digest: ContractDigest, decision: str, reason: str, now: datetime
    ) -> StoredEvent:
        if not reason.strip():
            raise ApprovalRefused(f"say why it is {decision}")
        self._ask(
            f"{decision.capitalize()}: contract {digest.value} ({task}). {reason}", digest.short
        )
        data = self._common(digest, now) | {
            "scope": SCOPE,
            "decision": decision,
            "digest_type": "contract",
            "expires_at": None,
            "note": reason,
        }
        return self._append(LedgerEvent(HUMAN_DECISION, now, task, data=data))

    def _common(self, digest: ContractDigest, now: datetime) -> dict[str, object]:
        return {
            "identity": self._identity,
            "os_user": self._os_user,
            "authenticated_by": (
                f"operator key {self._key.key_id} from the Keychain of OS user"
                f" {self._os_user}, confirmed by typing the code at a terminal"
            ),
            "decided_at": now.isoformat(),
            "digest": digest.value,
        }

    def _ask(self, summary: str, code: str) -> None:
        if not self._confirm(summary, code):
            raise ApprovalRefused("not confirmed at the terminal; nothing was recorded")

    def _append(self, event: LedgerEvent) -> StoredEvent:
        signed = _sign(event, self._key)
        with self._store.writer_lock():
            (stored,) = self._store.append(signed)
        return stored


def _extend(event: LedgerEvent, extra: Mapping[str, object]) -> LedgerEvent:
    return LedgerEvent(
        event.kind, event.at, event.task, event.attempt, event.run, {**event.data, **extra}
    )


def _contract_summary(title: str, bound: Mapping[str, object], expires: datetime) -> str:
    lines = [f"{title}:"]
    for k in ("task_id", "version", "repository", "base_commit", "attempt_budget", "digest"):
        lines.append(f"  {k}: {bound[k]}")
    for k in ("permitted_paths", "permitted_actions"):
        values = bound[k]
        assert isinstance(values, tuple)
        lines.append(f"  {k}: {', '.join(map(str, values))}")
    lines.append(f"  expires: {expires.isoformat()}")
    lines.append("This authorizes dispatch only, not the PR, a merge or a release.")
    return "\n".join(lines)


# --- Approved contracts on disk -------------------------------------------------------


class ContractStore:
    """Approved contracts kept by digest (ADR 0002 section 3): one read-only
    (0400) file per contract holding its canonical bytes, in a 0700 folder
    outside every git checkout."""

    def __init__(self, root: Path | str | None = None) -> None:
        default = Path.home() / ".software-factory" / "contracts"
        self.root = Path(root) if root is not None else default
        here = self.root.resolve()
        for parent in (here, *here.parents):
            if (parent / ".git").exists():
                raise ValueError(f"{self.root} is inside the git checkout at {parent}")

    def path(self, digest: ContractDigest) -> Path:
        return self.root / f"{digest.value}.json"

    def save(self, contract: Mapping[str, object]) -> ContractDigest:
        data = contracts.canonical_bytes(contract)
        digest = ContractDigest.of(data)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.path(digest)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(f"{path} does not hold the contract it is named for") from None
            return digest
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return digest

    def load(self, digest: ContractDigest) -> Mapping[str, object]:
        contract = contracts.loads(self.path(digest).read_bytes())
        if contracts.digest(contract) != digest:
            raise ValueError(f"{self.path(digest)} does not hold the contract it is named for")
        return contract
