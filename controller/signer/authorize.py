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
   Its repair allowance comes only from onboarding, is bounded by the task's
   budget, and cannot change on renewal. Positive repair terms also pin the
   ticket revision and policy, and require a move since policy activation.

Only then does it sign a ``source-authorization``: its own record kind, never
a ``human-decision``, valid for ``TTL``, for attempt 1, bound to the move, the
ticket revision, the routine, the onboarding entry and the contract digest.

Its second job (ENG-160) is ``use_repair_allowance``: the same checks on the
move again, then a ``source-repair-authorized`` record for one repair attempt,
within the allowance this move was bound to when it was first authorized, and
only while the project's policy is still the one that move was signed under.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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
    sign_source_repair,
)
from controller.attempts.events import SOURCE_REPAIR_AUTHORIZED
from controller.intake.linear import IntakeBlocked, Ticket, judge, policy_from
from controller.interfaces import AttemptId, LedgerEvent, TaskId
from controller.repair import findings as repair_findings
from controller.service.onboarding import Onboarding, OnboardingError
from controller.service.seams import Authorization

TTL = timedelta(minutes=30)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class Refused(Exception):
    """Nothing was signed. ``final`` says whether asking again could help."""

    def __init__(self, reason: str, *, final: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final


class OneContractPerMove:
    """Contract and repair terms per Todo move, in the signer's own folder.

    Called serially by the single signer server. The service cannot write
    this file. Legacy digest-only entries carry zero repair allowance.
    Entries named ``<move>#repair-a<n>`` record which failed commit each
    repair attempt under that move was for (ENG-160).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, object]:
        try:
            seen = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(seen, dict):
            raise ValueError("the signer's move record is not an object")
        return seen

    def terms_of(self, event_id: str) -> Mapping[str, object] | None:
        """What this move was bound to when first authorized, if it was. A
        legacy digest-only entry has no repair allowance."""
        bound = self._read().get(event_id)
        if isinstance(bound, str):
            return {"digest": bound, "repair_allowance": 0}
        return bound if isinstance(bound, dict) else None

    def bind_repair(self, event_id: str, attempt: int, candidate: str) -> None:
        """Repair ``attempt`` under this move is for ``candidate`` (the
        contract, PR and failed commit) and no other, even after a restart."""
        key = f"{event_id}#repair-a{attempt}"
        seen = self._read()
        if seen.get(key, candidate) != candidate:
            raise Refused(
                "this Todo move already allowed this repair attempt for another failed commit",
                final=True,
            )
        if key not in seen:
            seen[key] = candidate
            self._save(seen)
        self._sync_dir()

    def bind(
        self,
        event_id: str,
        digest: str,
        *,
        repair_allowance: int,
        terms: str,
        revision: str,
    ) -> None:
        """``terms`` is ``repair_terms_sha256`` of the project: only the repair
        settings are pinned, so an unrelated onboarding edit (the next pilot
        ticket added to ``issues``) never blocks renewing a queued move."""
        seen = self._read()
        previous = seen.get(event_id)
        previous_digest = previous.get("digest") if isinstance(previous, dict) else previous
        if event_id in seen and previous_digest != digest:
            raise Refused(
                "this Todo move already authorized a different version of the task; a changed"
                " task needs a new Todo move",
                final=True,
            )
        bound = {
            "digest": digest,
            "repair_allowance": repair_allowance,
            "repair_terms_sha256": terms,
            "revision": revision,
        }
        if event_id in seen:
            # Old records contain only a digest; they retain zero repairs.
            old_allowance = previous.get("repair_allowance") if isinstance(previous, dict) else 0
            if (
                type(old_allowance) is not int
                or old_allowance != repair_allowance
                or (repair_allowance and previous != bound)
            ):
                raise Refused(
                    "this Todo move already bound different repair terms; move the ticket out"
                    " of Todo and back to approve the current policy and allowance",
                    final=True,
                )
        else:
            seen[event_id] = bound
            # Persist the binding before signing. A lost reply must never let
            # the same move acquire different repair terms after a restart.
            self._save(seen)
        # Also retry this when a previous replace succeeded but its directory
        # sync failed. No signed answer escapes until the record is durable.
        self._sync_dir()

    def _save(self, seen: Mapping[str, object]) -> None:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as f:
                tmp = Path(f.name)
                json.dump(seen, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _sync_dir(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


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


def repair_terms_sha256(project) -> str:
    """The repair settings a Todo move is bound to (ENG-160), and nothing else
    from the onboarding file."""
    since = project.repair_allowance_since
    body = {
        "repair_allowance": project.repair_allowance,
        "repair_allowance_since": since.isoformat() if since else None,
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
        verdict, config, policy, project = self._standing_move(event_id, issue_id, revision)
        now = self.now()
        frozen = self._within(contract, project, verdict.task.value)
        bound = contracts.binding(frozen)
        digest = str(bound["digest"])
        allowance = min(project.repair_allowance, int(frozen["attempt_budget"]) - 1)
        if allowance and (
            project.repair_allowance_since is None
            or verdict.moved_at < project.repair_allowance_since
        ):
            raise Refused(
                "this Todo move predates the repair policy; move the ticket out of Todo and"
                " back to approve its allowance",
                final=True,
            )
        policy_digest = policy_sha256(config, verdict.project_id)
        self.seen.bind(
            event_id,
            digest,
            repair_allowance=allowance,
            terms=repair_terms_sha256(project),
            revision=revision,
        )
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
            "policy_sha256": policy_digest,
            "repair_allowance": allowance,
        }
        event = LedgerEvent(SOURCE_AUTHORIZATION, now, TaskId(verdict.task.value), data=data)
        return sign_source_authorization(event, self.key)

    def use_repair_allowance(
        self,
        event_id: str,
        issue_id: str,
        contract: object,
        revision: str,
        attempt: int,
        prior_pr: int,
        prior_head: str,
        failure: str,
        findings: object,
    ) -> LedgerEvent:
        """Sign the go-ahead for repair attempt ``attempt`` under the repair
        allowance of the Todo move ``event_id`` (ENG-160).

        It checks again, with Linear and the onboarding file, everything the
        move's own authorization needed, and that this move already authorized
        exactly this contract. Then: the project gives Todo moves a repair
        allowance that covers this attempt, the attempt fits the contract's
        budget and the project's limit, and the move has no other go-ahead for
        this attempt number on another candidate. What failed, and whether the
        earlier worker has stopped, are the service's and the ledger's checks:
        the signer can't read either, so the allowance is the bound it keeps."""
        verdict, config, policy, project = self._standing_move(event_id, issue_id, revision)
        frozen = self._within(contract, project, verdict.task.value)
        bound = contracts.binding(frozen)
        digest = str(bound["digest"])
        terms = self.seen.terms_of(event_id)
        if terms is None or terms.get("digest") != digest:
            raise Refused(
                "this Todo move did not authorize this contract, so its repair allowance"
                " doesn't cover it",
                final=True,
            )
        allowance = terms.get("repair_allowance")
        if type(allowance) is not int or allowance <= 0:
            raise Refused("this Todo move came with no automatic repairs", final=True)
        policy_digest = policy_sha256(config, verdict.project_id)
        if (
            terms.get("repair_terms_sha256") != repair_terms_sha256(project)
            or terms.get("revision") != revision
        ):
            raise Refused(
                "the project's repair settings changed since this Todo move; its repair"
                " allowance lapsed with them, so a repair needs Rolando's go-ahead or a new move",
                final=True,
            )
        budget = bound["attempt_budget"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 2:
            raise Refused("a repair is attempt 2 or later", final=True)
        if not isinstance(budget, int) or attempt > min(budget, project.max_attempts):
            raise Refused(
                f"attempt {attempt} is over the task's budget of"
                f" {min(budget if isinstance(budget, int) else 0, project.max_attempts)}",
                final=True,
            )
        if attempt - 1 > allowance:
            raise Refused(
                f"this Todo move came with {allowance} automatic repair"
                f"{'' if allowance == 1 else 's'}, and this would be repair {attempt - 1}",
                final=True,
            )
        if isinstance(prior_pr, bool) or not isinstance(prior_pr, int) or prior_pr < 1:
            raise Refused("no pull request was named", final=True)
        if not isinstance(prior_head, str) or not _COMMIT_RE.fullmatch(prior_head):
            raise Refused("the failed commit must be a 40-character commit id", final=True)
        if not isinstance(failure, str) or not failure.strip():
            raise Refused("a repair names what failed", final=True)
        plain = repair_findings.plain(findings)
        if not plain or len(plain) > repair_findings.MAX_FINDINGS:
            raise Refused("a repair carries the findings it fixes", final=True)
        self.seen.bind_repair(event_id, attempt, f"{digest}:{prior_pr}:{prior_head}")
        now = self.now()
        task = TaskId(verdict.task.value)
        data = {
            "identity": APPROVER,
            "os_user": "factory-signer",
            "by": "the factory, under the repair allowance of Rolando's Todo move",
            "authenticated_by": (
                f"the repair allowance ({allowance}) of Rolando's move of"
                f" {verdict.issue_key} to Todo (entry {event_id}), checked again in Linear's"
                " history by the signer. A factory action, not a decision Rolando made."
            ),
            "decided_at": now.isoformat(),
            "expires_at": (now + TTL).isoformat(),
            "digest": digest,
            "failure": failure.strip()[:600],
            "event_id": event_id,
            "issue_key": verdict.issue_key,
            "revision": verdict.revision,
            "routine_id": project.routine_id,
            "policy_sha256": policy_digest,
            "repair_allowance": str(allowance),
            "prior_pr": str(prior_pr),
            "prior_head": prior_head,
            "findings": plain,
            "findings_sha256": repair_findings.digest(plain),
        }
        event = LedgerEvent(
            SOURCE_REPAIR_AUTHORIZED, now, task, AttemptId(task, attempt), data=data
        )
        return sign_source_repair(event, self.key)

    def _standing_move(self, event_id: str, issue_id: str, revision: str):
        """The checks every signature starts with: the onboarding file is usable
        and intake is on, the signer's Linear key isn't Rolando's, and Linear
        shows ``event_id`` as Rolando's settled, unedited latest move of the
        ticket to Todo, on exactly the text ``revision``."""
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
        except IntakeBlocked as e:
            raise Refused(str(e), final=False) from None
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
        project = config.project(verdict.project_id)
        if project is None:
            raise Refused("the ticket's project is not onboarded", final=True)
        return verdict, config, policy, project

    @staticmethod
    def _within(contract: object, project, task: str):
        if not isinstance(contract, Mapping):
            raise Refused("no contract was given", final=True)
        errors = contracts.approval_errors(contract)
        if errors:
            raise Refused("the contract is not valid: " + "; ".join(errors), final=True)
        problems = project.source_problems(contract, task)
        if problems:
            raise Refused(
                "the contract is outside the project's limits: " + "; ".join(problems), final=True
            )
        return contracts.freeze(contract)


def event_to_json(e: LedgerEvent) -> Mapping[str, object]:
    out: dict[str, object] = {
        "kind": e.kind,
        "at": e.at.isoformat(),
        "task": None if e.task is None else e.task.value,
        "data": json.loads(json.dumps(e.data, default=_plain)),
    }
    if e.attempt is not None:
        out["attempt"] = e.attempt.number
    return out


def event_from_json(raw: Mapping[str, object]) -> LedgerEvent:
    task = raw.get("task")
    tid = None if task is None else TaskId(str(task))
    number = raw.get("attempt")
    attempt = None
    if number is not None:
        if tid is None or isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("an attempt needs its task and a number")
        attempt = AttemptId(tid, number)
    return LedgerEvent(
        str(raw["kind"]),
        datetime.fromisoformat(str(raw["at"])),
        tid,
        attempt,
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
    "repair_terms_sha256",
]
