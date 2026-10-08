"""Rolando's approval, bound to one exact contract (ENG-151).

Write decisions with ``Approvals`` (approve, reject, revoke, authorize_repair,
authorize_refire, record_clearing); each is signed with the operator key from
the Keychain and needs Rolando to type a code at a real terminal. At dispatch,
``Approvals.check(contract, now)`` says whether those decisions allow the next
fire of exactly that contract; dispatch then calls ``AttemptGate.reserve``.
"""

from controller.approval.approval import (
    APPROVED,
    APPROVER,
    DEFAULT_TTL,
    HUMAN_DECISION,
    KEYCHAIN_SERVICE,
    MAX_TTL,
    REJECTED,
    REVOKED,
    SCOPE,
    ApprovalKey,
    ApprovalRefused,
    Approvals,
    Confirm,
    ContractStore,
    KeychainKey,
    StaticKey,
    Verdict,
    authentic,
    tty_confirm,
)

__all__ = [
    "APPROVED",
    "APPROVER",
    "DEFAULT_TTL",
    "HUMAN_DECISION",
    "KEYCHAIN_SERVICE",
    "MAX_TTL",
    "REJECTED",
    "REVOKED",
    "SCOPE",
    "ApprovalKey",
    "ApprovalRefused",
    "Approvals",
    "Confirm",
    "ContractStore",
    "KeychainKey",
    "StaticKey",
    "Verdict",
    "authentic",
    "tty_confirm",
]
