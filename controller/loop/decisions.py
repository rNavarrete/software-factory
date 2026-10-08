"""Rolando's review inputs and active minutes, kept in the ledger (ENG-145).

Two things only Rolando can supply for a candidate: what he saw when he
followed an ``observable-behavior`` criterion's steps (an ``Observation``),
and that he looked at one flag the assertion map raised (a ``Clearance``).
The loop asks for both at his terminal and writes each as a human decision,
signed with the operator key the approval records use, bound to the exact
contract digest and revision. Only authentic records for that revision are
read back, so nothing on GitHub (a PR comment saying "observed: pass") and no
unsigned row can stand in for him.

Active minutes go in as ``human-time`` entries: the loop times every prompt it
shows him, and he can add time spent elsewhere (reading a PR on GitHub) with
``python3 -m controller.loop time``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from controller.approval import APPROVER, ApprovalKey, ApprovalRefused, Confirm
from controller.approval.approval import _sign, authentic
from controller.interfaces import AttemptId, ContractDigest, LedgerEvent, LedgerStore, TaskId
from controller.ledger import kinds, records
from verify.assertions import Clearance
from verify.criteria import Candidate, Observation, Verdict

SCOPE = "candidate-review"
"""Rolando's review inputs. Never read as a dispatch approval (that scope is
``contract-dispatch``) and never as a PR approval, merge or release."""
OBSERVED, CLEARED = "observed", "cleared-flag"
MIN_MINUTES = 0.01


class ReviewDecisions:
    def __init__(
        self,
        store: LedgerStore,
        key: ApprovalKey,
        *,
        identity: str = APPROVER,
        os_user: str = "",
    ) -> None:
        self._store = store
        self._key = key
        self._identity = identity
        self._os_user = os_user

    def observe(
        self,
        attempt: AttemptId,
        digest: ContractDigest,
        candidate: Candidate,
        criterion: str,
        verdict: Verdict,
        seen: str,
        limitations: str,
        now: datetime,
    ) -> None:
        if verdict not in (Verdict.PASS, Verdict.FAIL):
            raise ValueError("an observation is pass or fail")
        self._write(
            attempt,
            digest,
            candidate,
            OBSERVED,
            now,
            criterion=criterion,
            verdict=verdict.value,
            seen=seen,
            limitations=limitations,
        )

    def clear(
        self,
        attempt: AttemptId,
        digest: ContractDigest,
        candidate: Candidate,
        flag: str,
        note: str,
        now: datetime,
    ) -> None:
        self._write(attempt, digest, candidate, CLEARED, now, flag=flag, note=note)

    def observations(
        self, attempt: AttemptId, digest: ContractDigest, candidate: Candidate
    ) -> tuple[Observation, ...]:
        return tuple(
            Observation(
                criterion=b["criterion"],
                commit=b["commit"],
                base_commit=b["base_commit"],
                observer=self._identity,
                verdict=Verdict(b["verdict"]),
                seen=b["seen"],
                limitations=b["limitations"],
            )
            for b in self._bindings(attempt, digest, candidate, OBSERVED)
        )

    def clearances(
        self, attempt: AttemptId, digest: ContractDigest, candidate: Candidate
    ) -> tuple[Clearance, ...]:
        return tuple(
            Clearance(
                flag=b["flag"],
                contract_digest=digest.value,
                commit=b["commit"],
                base_commit=b["base_commit"],
                by=self._identity,
                note=b["note"],
            )
            for b in self._bindings(attempt, digest, candidate, CLEARED)
        )

    def _write(self, attempt, digest, candidate, decision, now, **fields) -> None:
        binding = {
            "digest": digest.value,
            "repository": candidate.repository,
            "commit": candidate.head_commit,
            "base_commit": candidate.base_commit,
            **fields,
        }
        event = LedgerEvent(
            kinds.HUMAN_DECISION,
            now,
            attempt.task,
            attempt,
            data={
                "identity": self._identity,
                "os_user": self._os_user,
                "authenticated_by": "operator-key-at-terminal",
                "decided_at": now.isoformat(),
                "scope": SCOPE,
                "decision": decision,
                "digest_type": "contract",
                "digest": digest.value,
                "expires_at": None,
                "binding": binding,
            },
        )
        signed = _sign(event, self._key)
        with self._store.writer_lock():
            (stored,) = self._store.append(signed)
        if not authentic(stored.event, [self._key], frozenset({self._identity})):
            raise ApprovalRefused(
                "the ledger changed this record as it wrote it (it blanks text that looks like"
                " a secret), so it will never count. Reword it and enter it again."
            )

    def _bindings(self, attempt, digest, candidate, decision) -> list[dict]:
        out = []
        for s in self._store.events(attempt.task):
            e = s.event
            d = e.data
            if (
                e.kind != kinds.HUMAN_DECISION
                or e.attempt != attempt
                or d.get("scope") != SCOPE
                or d.get("decision") != decision
                or d.get("digest") != digest.value
                or not authentic(e, [self._key], frozenset({self._identity}))
            ):
                continue
            b = d.get("binding")
            if (
                b is None
                or b.get("repository") != candidate.repository
                or b.get("commit") != candidate.head_commit
                or b.get("base_commit") != candidate.base_commit
            ):
                continue
            out.append(dict(b))
        return out


@dataclass
class Timer:
    """Times each prompt shown to Rolando and records it as his active minutes."""

    store: LedgerStore
    clock: Callable[[], datetime]
    entered_by: str = "controller (timed prompt)"
    task: TaskId | None = None
    interactive: Callable[[], bool] = lambda: True
    """Whether a person is at the terminal. With nobody there (a piped or
    scheduled run) prompts are refused at once, and no minutes are made up."""

    def record(
        self, minutes: float, activity: str, reason: str | None = None, *, entered_by=None
    ) -> None:
        event = records.human_time(
            round(max(minutes, MIN_MINUTES), 2),
            activity[:200],
            entered_by or self.entered_by,
            self.clock(),
            task=self.task,
            intervention_reason=reason,
        )
        with self.store.writer_lock():
            self.store.append(event)

    def timed(self, ask: Callable[[], object], activity: str, reason: str | None = None):
        if not self.interactive():
            return ask()
        start = self.clock()
        try:
            return ask()
        finally:
            self.record((self.clock() - start).total_seconds() / 60, activity, reason)

    def confirm(self, inner: Confirm) -> Confirm:
        """``inner`` (the approval code prompt), timed."""

        def confirm(summary: str, code: str, **kw) -> bool:
            first = summary.strip().splitlines()[0] if summary.strip() else "confirm a decision"
            return bool(self.timed(lambda: inner(summary, code, **kw), f"typed a code: {first}"))

        return confirm


def time_entries(store: LedgerStore, task: TaskId | None = None) -> Sequence[str]:
    """Human-time entries as CSV lines (header first)."""
    stored = store.events(task) if task is not None else store.events()
    return records.human_time_csv(stored).splitlines()
