"""The findings a repair works from, in the one shape every step uses.

A finding comes from the independent review (``verify/findings.py``, ENG-156)
or from CI as the review read it. The repair keeps only the fields the worker
needs, cleaned of anything secret-looking, and binds the exact list with
``digest``: the signed go-ahead names that digest, and dispatch hands the
worker the list only if it still matches.

Standard library only. No I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from controller.ledger.redact import redact

FIELDS = ("id", "category", "summary", "evidence", "suggested_action")
MAX_FINDINGS = 20
"""More than this is not a routine repair: Rolando decides."""
MAX_FIELD_CHARS = 2000
"""The review caps its own texts at 2000; a longer one is refused, not cut."""
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


@dataclass(frozen=True)
class RepairFinding:
    """One open, blocking problem on the candidate, as the repair sees it."""

    id: str
    """Stable across revisions (the review's own id), so the next review can
    tell whether this repair fixed it."""
    category: str
    summary: str
    evidence: str
    suggested_action: str
    route: str
    """``repair`` or ``rolando``, as the review routed it."""
    blocking: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID_RE.fullmatch(self.id):
            raise ValueError(f"finding id must be plain text, got {self.id!r}")
        if not isinstance(self.category, str) or not _CATEGORY_RE.fullmatch(self.category):
            raise ValueError(f"finding category must be lowercase words, got {self.category!r}")
        for name in ("summary", "evidence", "suggested_action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"finding {name} must be non-empty text")
            if len(value) > MAX_FIELD_CHARS:
                raise ValueError(f"finding {name} is over {MAX_FIELD_CHARS} characters")
        if self.route not in ("repair", "rolando"):
            raise ValueError(f"finding route must be repair or rolando, got {self.route!r}")

    def as_data(self) -> dict[str, str]:
        """What the ledger keeps and the worker gets. Secret-looking text is
        blanked first, so the ledger's own redaction never changes it later."""
        return {name: redact(str(getattr(self, name))) for name in FIELDS}


def as_data(findings: Iterable[RepairFinding]) -> list[dict[str, str]]:
    return [f.as_data() for f in findings]


def digest(findings: object) -> str | None:
    """sha256 of the exact finding list (as ``as_data`` gives it, or as the
    ledger reads it back), or None if it isn't one."""
    plain = _plain(findings)
    if plain is None:
        return None
    text = json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _plain(findings: object) -> list[dict[str, str]] | None:
    if isinstance(findings, str | bytes) or not isinstance(findings, Sequence):
        return None
    out = []
    for f in findings:
        if not isinstance(f, Mapping) or set(f) != set(FIELDS):
            return None
        if not all(isinstance(f[k], str) for k in FIELDS):
            return None
        out.append({k: str(f[k]) for k in FIELDS})
    return out


def plain(findings: object) -> list[dict[str, str]] | None:
    """The finding list as plain JSON data, or None if it isn't one."""
    return _plain(findings)


__all__ = [
    "FIELDS",
    "MAX_FIELD_CHARS",
    "MAX_FINDINGS",
    "RepairFinding",
    "as_data",
    "digest",
    "plain",
]
