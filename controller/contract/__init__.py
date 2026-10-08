"""Single-task contract format, validation and canonical digest (ENG-144).

The routine adapter calls ``validate(contract, digest)`` before every launch;
an empty list means the contract is well formed, approvable and matches the
digest. ``digest(contract)`` is the canonical digest the PR title marker and
``Contract-Digest:`` line carry.
"""

from controller.contract.contract import (
    ACTIONS,
    BOUND_FIELDS,
    EVIDENCE_TYPES,
    FORMAT,
    RISK_MARKERS,
    approval_errors,
    binding,
    canonical_bytes,
    digest,
    freeze,
    loads,
    structure_errors,
    validate,
)

__all__ = [
    "ACTIONS",
    "BOUND_FIELDS",
    "EVIDENCE_TYPES",
    "FORMAT",
    "RISK_MARKERS",
    "approval_errors",
    "binding",
    "canonical_bytes",
    "digest",
    "freeze",
    "loads",
    "structure_errors",
    "validate",
]
