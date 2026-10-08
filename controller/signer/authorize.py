"""The signer's one signing job: Rolando's Todo move as the approval (ENG-174).

Rolando chose on 2026-10-08 that his own Todo move in Linear approves the
contract the factory drafts from that ticket, within the project's onboarding
entry, without typing a code. The service asks; this code, inside the signer
process, decides. It trusts nothing the service sends except as a question:

1. It reads the onboarding file itself (a root-owned file in the image the
   service can't change), and refuses unless intake is switched on.
2. It reads the ticket from Linear itself, with its own copy of the Linear
   key, and runs the same rules as intake (``controller.intake.linear.judge``):
   the latest move into Todo must be the very event the service names, made
   by Rolando himself, settled, unedited since, on a ticket the project allows.
3. The contract must be for that ticket's task and stay inside the onboarding
   entry: repository, routine, actions, checks and attempt budget.
4. A Todo move authorizes one contract only. Asking again for the same
   contract renews it; a different contract for the same move is refused.

Only then does it sign a ``source-authorization``: its own record kind, never
a ``human-decision``, valid for ``TTL``, for attempt 1, bound to the move, the
ticket revision, the routine, the onboarding entry and the contract digest.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller import contract as contracts
from controller.approval.approval import (
    APPROVED,
    APPROVER,
    SCOPE,
    SOURCE_AUTHORIZATION,
    ApprovalKey,
    sign_source_authorization,
)
from controller.intake.linear import IntakeBlocked, Ticket, judge, policy_from
from controller.interfaces import LedgerEvent, TaskId
from controller.service.onboarding import Onboarding, OnboardingError
from controller.service.seams import Authorization

TTL = timedelta(minutes=30)


class Refused(Exception):
    """Nothing was signed. ``final`` says whether asking again could help."""

    def __init__(self, reason: str, *, final: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final


class OneContractPerMove:
    """Which contract each Todo move authorized, kept in the signer's own
    folder (the service can't write it)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        try:
            return dict(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return {}

    def bind(self, event_id: str, digest: str) -> None:
        seen = self._read()
        if seen.get(event_id, digest) != digest:
            raise Refused(
                "this Todo move already authorized a different version of the task; a changed"
                " task needs a new Todo move",
                final=True,
            )
        if event_id in seen:
            return
        seen[event_id] = digest
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(seen, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


def policy_sha256(onboarding: Onboarding, project_id: str) -> str:
    p = onboarding.project(project_id)
    assert p is not None
    body = {
        **p.as_mapping(),
        "issues": None if p.issues is None else sorted(p.issues),
        "skip_labels": sorted(p.skip_labels),
        "protected_paths": sorted(p.protected_paths),
        "approver_linear_user_id": onboarding.approver_linear_user_id,
    }
    return hashlib.sha256(contracts.canonical_bytes(body)).hexdigest()


@dataclass
class TodoMoveAuthorizer:
    key: ApprovalKey
    onboarding: Callable[[], Onboarding]
    fetch: Callable[[str], Ticket | None]
    """Reads one ticket from Linear with the signer's own key."""
    viewer: Callable[[], str]
    """Whose Linear key ``fetch`` reads with. It must not be Rolando's own,
    or changes made with that key would read as his moves."""
    seen: OneContractPerMove
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def authorize(
        self, event_id: str, issue_id: str, contract: object, revision: str
    ) -> LedgerEvent:
        """Sign ``contract`` for the Todo move ``event_id``, if Linear still
        shows that move as Rolando's on the exact ticket text (``revision``)
        the contract was drafted from."""
        try:
            config = self.onboarding()
        except OnboardingError as e:
            raise Refused(f"the onboarding file is unusable: {e}", final=False) from None
        if not config.intake_enabled:
            raise Refused("intake is switched off in the onboarding file", final=False)
        try:
            policy = policy_from(config)
        except IntakeBlocked as e:
            raise Refused(str(e), final=False) from None
        now = self.now()
        try:
            viewer = self.viewer()
            ticket = self.fetch(issue_id)
        except OSError as e:
            raise Refused(f"Linear could not be read ({e})", final=False) from None
        if viewer == policy.approver_id:
            raise Refused(
                "the signer's Linear key acts as Rolando, so its own changes would look like"
                " his moves; it needs the factory's own Linear identity",
                final=False,
            )
        if ticket is None:
            raise Refused("the ticket is gone", final=True)
        verdict = judge(ticket, policy, now)
        if not isinstance(verdict, Authorization) or verdict.event_id != event_id:
            reason = getattr(verdict, "reason", None) or (
                "the ticket's latest Todo move is not this one, or it doesn't stand any more"
            )
            raise Refused(f"Linear does not show this move as Rolando's: {reason}", final=True)
        if verdict.revision != revision:
            raise Refused(
                "the ticket's text is not the text the task was drafted from; move it out of"
                " Todo and back to approve the new text",
                final=True,
            )
        if not isinstance(contract, Mapping):
            raise Refused("no contract was given", final=True)
        errors = contracts.approval_errors(contract)
        if errors:
            raise Refused("the contract is not valid: " + "; ".join(errors), final=True)
        project = config.project(verdict.project_id)
        if project is None:
            raise Refused("the ticket's project is not onboarded", final=True)
        problems = project.source_problems(contract, verdict.task.value)
        if problems:
            raise Refused(
                "the contract is outside the project's limits: " + "; ".join(problems), final=True
            )
        frozen = contracts.freeze(contract)
        bound = contracts.binding(frozen)
        digest = str(bound["digest"])
        self.seen.bind(event_id, digest)
        data = {
            "identity": APPROVER,
            "os_user": "factory-signer",
            "authenticated_by": (
                f"Rolando's own move of {verdict.issue_key} to Todo, read from Linear's history"
                f" by the signer (entry {event_id}); no code typed, per his choice of 2026-10-08"
            ),
            "decided_at": now.isoformat(),
            "digest": digest,
            "scope": SCOPE,
            "decision": APPROVED,
            "digest_type": "contract",
            "expires_at": (now + TTL).isoformat(),
            "binding": bound,
            "event_id": event_id,
            "issue_id": verdict.issue_id,
            "issue_key": verdict.issue_key,
            "revision": verdict.revision,
            "linear_actor_id": policy.approver_id,
            "routine_id": project.routine_id,
            "policy_sha256": policy_sha256(config, verdict.project_id),
        }
        event = LedgerEvent(SOURCE_AUTHORIZATION, now, TaskId(verdict.task.value), data=data)
        return sign_source_authorization(event, self.key)


def event_to_json(e: LedgerEvent) -> Mapping[str, object]:
    return {
        "kind": e.kind,
        "at": e.at.isoformat(),
        "task": None if e.task is None else e.task.value,
        "data": json.loads(json.dumps(e.data, default=_plain)),
    }


def event_from_json(raw: Mapping[str, object]) -> LedgerEvent:
    task = raw.get("task")
    return LedgerEvent(
        str(raw["kind"]),
        datetime.fromisoformat(str(raw["at"])),
        None if task is None else TaskId(str(task)),
        data=dict(raw["data"]),  # type: ignore[arg-type]
    )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON: {type(value).__name__}")


__all__ = [
    "TTL",
    "OneContractPerMove",
    "Refused",
    "TodoMoveAuthorizer",
    "event_from_json",
    "event_to_json",
    "policy_sha256",
]
