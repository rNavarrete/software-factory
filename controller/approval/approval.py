"""Rolando's approval, bound to one exact contract (ENG-151, ADR 0001 section 5).

Every decision this module writes is a ledger event signed with an operator key
that lives in the macOS Keychain of Rolando's login, and is written only after
he types the contract's short digest at a real terminal. Nothing here reads PR
text, comments, worker output or any network input, so none of those can make
an approval.

What gets signed: who decided, when, the OS user, the exact contract digest,
the decision and its scope, the expiry, and which attempt or run it covers.
Free text (the failure a repair names, clearing evidence, notes) is stored
beside it but not signed, because the ledger redacts secret-looking text before
writing and that must not break the signature.

``Approvals.check`` is the dispatch-time question "has Rolando approved exactly
this contract for exactly this attempt?". It re-reads the ledger every time. It
does not replace the attempt gate: dispatch (ENG-176) calls ``check`` first and
``AttemptGate.reserve`` right after, and both must pass.

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
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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
_COMMON = ("identity", "os_user", "decided_at", "digest", "key_id")
_SIGNED_FIELDS = {
    HUMAN_DECISION: (*_COMMON, "scope", "decision", "digest_type", "expires_at"),
    ev.REPAIR_AUTHORIZED: _COMMON,
    ev.REFIRE_AUTHORIZED: _COMMON,
    ev.ATTEMPT_CLEARED: (*_COMMON, "basis"),
}

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
            -w "$(openssl rand -hex 32)"
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
    """Ask at the terminal. Refuses piped or scripted input outright."""
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
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _MAC_DOMAIN + text.encode("utf-8")


def _sign(event: LedgerEvent, key: ApprovalKey) -> LedgerEvent:
    data = {**event.data, "key_id": key.key_id}
    payload = _payload(event.kind, event.task, event.attempt, event.run, data)
    assert payload is not None, "an unsigned field is missing"
    data["mac"] = key.sign(payload)
    return LedgerEvent(event.kind, event.at, event.task, event.attempt, event.run, data)


def authentic(event: LedgerEvent, key: ApprovalKey, approvers: frozenset[str]) -> bool:
    """True if ``event`` was signed with ``key`` and names one of ``approvers``."""
    d = event.data
    mac = d.get("mac")
    if not isinstance(mac, str) or d.get("key_id") != key.key_id:
        return False
    if d.get("identity") not in approvers:
        return False
    payload = _payload(event.kind, event.task, event.attempt, event.run, d)
    if payload is None or _time(d.get("decided_at")) is None:
        return False
    return hmac.compare_digest(key.sign(payload), mac)


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
    """Whether Rolando's decisions allow firing ``run`` for this exact contract."""

    digest: ContractDigest | None
    run: RunId | None
    """The run dispatch would fire, if the decisions allow it."""
    blocks: tuple[Notice, ...]

    @property
    def allowed(self) -> bool:
        return not self.blocks and self.run is not None


class Approvals:
    """Rolando's decisions about contracts: write them, and check them at dispatch.

    ``store`` is the ledger. ``key`` signs and verifies (KeychainKey on the Mac).
    ``confirm`` asks Rolando to type a code before anything is written.
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
        contracts: ContractStore | None = None,
    ) -> None:
        if identity not in approvers:
            raise ValueError(f"{identity!r} is not an approver")
        self._store = store
        self._key = key
        self._confirm = confirm
        self._identity = identity
        self._os_user = os_user or getpass.getuser()
        self._approvers = approvers
        self._contracts = contracts

    # --- writing decisions ---

    def approve(
        self, contract: Mapping[str, object], now: datetime, *, ttl: timedelta = DEFAULT_TTL
    ) -> StoredEvent:
        """Approve dispatching exactly this contract until ``now + ttl``."""
        _aware(now)
        errors = contracts.approval_errors(contract)
        if errors:
            raise ApprovalRefused("contract can't be approved: " + "; ".join(errors))
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
        if self._contracts is not None:
            self._contracts.save(contract)
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
        return self._withdraw(TaskId(str(contract["task_id"])), digest, REJECTED, reason, now)

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
        attempt = AttemptId(TaskId(str(contract["task_id"])), number)
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
        """A clearing record (ADR 0002 section 6.1) for an attempt that may have
        left a session running. ``evidence`` is what ``basis`` needs."""
        _aware(now)
        if not evidence.strip():
            raise ApprovalRefused("a clearing record needs its evidence")
        view = LedgerView.build(self._store.events())
        state = view.attempts.get(attempt)
        if state is None or not state.digest:
            raise ApprovalRefused(f"{attempt} was never started")
        digest = ContractDigest(state.digest)
        summary = f"Clear {attempt} ({basis.value}): {evidence}"
        self._ask(summary, str(attempt))
        base = ev.attempt_cleared(attempt, basis, evidence, self._identity, now)
        return self._append(_extend(base, self._common(digest, now)))

    # --- checking at dispatch ---

    def check(
        self, contract: Mapping[str, object], now: datetime, *, refire_of: RunId | None = None
    ) -> Verdict:
        """May dispatch fire this exact contract now? Reads the ledger afresh.

        Without ``refire_of`` this is the task's next attempt; with it, firing
        that run's attempt again. Holds, usage and rate limits are left to
        ``AttemptGate.reserve``, which dispatch must still call.
        """
        _aware(now)
        errors = contracts.approval_errors(contract)
        if errors:
            return Verdict(None, None, (Notice("contract-invalid", "; ".join(errors)),))
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
        unless it is signed, bound to the right contract, and made after what it
        decides on. ``digest`` is the contract being dispatched: repairs only
        count for it."""
        reserved: dict[AttemptId, tuple[int, str]] = {}
        results: dict[RunId, int] = {}
        out = []
        for item in sorted(stored, key=lambda s: s.seq):
            e = item.event
            d = e.data
            keep = True
            if e.kind == ev.ATTEMPT_RESERVED and e.attempt is not None:
                reserved.setdefault(e.attempt, (item.seq, str(d.get("digest", ""))))
            elif e.kind == ev.FIRE_RESULT and e.run is not None:
                results.setdefault(e.run, item.seq)
            elif e.kind == ev.REPAIR_AUTHORIZED:
                prior = None
                if e.attempt is not None and e.attempt.number >= 2:
                    prior = reserved.get(AttemptId(e.attempt.task, e.attempt.number - 1))
                keep = prior is not None and d.get("digest") == digest.value and self._authentic(e)
            elif e.kind == ev.REFIRE_AUTHORIZED:
                at = reserved.get(e.attempt) if e.attempt is not None else None
                keep = (
                    e.run is not None
                    and at is not None
                    and e.run in results
                    and d.get("digest") == at[1]
                    and self._authentic(e)
                )
            elif e.kind == ev.ATTEMPT_CLEARED:
                at = reserved.get(e.attempt) if e.attempt is not None else None
                keep = at is not None and d.get("digest") == at[1] and self._authentic(e)
            if keep:
                out.append(item)
        return out

    # --- internals ---

    def _authentic(self, event: LedgerEvent) -> bool:
        return authentic(event, self._key, self._approvers)

    def _approval_block(
        self, stored: Sequence[StoredEvent], task: TaskId, digest: ContractDigest, now: datetime
    ) -> Notice | None:
        standing: list[tuple[datetime, datetime]] = []
        withdrawn = None
        unsigned = other_contract = False
        for item in sorted(stored, key=lambda s: s.seq):
            e = item.event
            d = e.data
            if e.kind != HUMAN_DECISION or e.task != task or d.get("scope") != SCOPE:
                continue
            if d.get("digest") != digest.value:
                other_contract = other_contract or d.get("decision") == APPROVED
                continue
            if d.get("decision") in (REJECTED, REVOKED):
                # Counted even if unsigned: a withdrawal can only stop dispatch.
                standing.clear()
                withdrawn = d["decision"]
            elif d.get("decision") == APPROVED:
                decided = _time(d.get("decided_at"))
                expires = _time(d.get("expires_at"))
                if (
                    not self._authentic(e)
                    or decided is None
                    or expires is None
                    or not timedelta(0) < expires - decided <= MAX_TTL
                ):
                    unsigned = True
                    continue
                standing.append((decided, expires))
        if any(decided <= now < expires for decided, expires in standing):
            return None
        if any(now < decided for decided, _ in standing):
            return Notice("approval-not-yet-valid", "The approval is dated after now.")
        if standing:
            return Notice("approval-expired", "The approval has expired; approve it again.")
        if withdrawn is not None:
            return Notice(f"approval-{withdrawn}", f"Rolando {withdrawn} this exact contract.")
        if unsigned:
            return Notice(
                "approval-unauthenticated",
                "An approval record for this contract is not signed with the operator key.",
            )
        if other_contract:
            return Notice(
                "approval-for-different-contract",
                "Rolando approved a different version of this task's contract. Any change"
                " (content, scope, base commit or budget) needs a fresh approval.",
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
    lines.append(f"  permitted_paths: {', '.join(map(str, bound['permitted_paths']))}")  # type: ignore[arg-type]
    lines.append(f"  permitted_actions: {', '.join(map(str, bound['permitted_actions']))}")  # type: ignore[arg-type]
    lines.append(f"  expires: {expires.isoformat()}")
    lines.append("This authorizes dispatch only, not the PR, a merge or a release.")
    return "\n".join(lines)


# --- Approved contracts on disk -------------------------------------------------------


class ContractStore:
    """Approved contracts kept by digest (ADR 0002 section 3): one read-only file
    per contract holding its canonical bytes, outside every git checkout."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = (
            Path(root) if root is not None else Path.home() / ".software-factory" / "contracts"
        )
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
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
