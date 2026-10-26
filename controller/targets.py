"""The repositories the factory may work on, and the one routine for each.

This table is the only place a repository becomes reachable. The onboarding
file on the host can map Linear projects only to a repository listed here,
with exactly the routine listed for it, so a hand edit on the server can't
point the factory at a repository nobody reviewed. Adding one is a reviewed
change to this file (and, for its weekly cap, to docs/limits.md).

Each routine has only its own repository attached, so a worker started for
one repository holds no credentials for another.

Anything recorded before a repository was written into the ledger (fires,
reservations, reviews from before 2026-10-26) was for the pilot.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

PILOT_REPO = "rNavarrete/factory-pilot-demo"
NEON_WAR_REPO = "rNavarrete/neon-war"

PILOT_ROUTINE = "trig_01CHWbQ267i1CMLGUym1kGd9"
"""The ``factory-worker`` routine on the factory account (created 2026-10-08),
bound to the pilot repo only."""

NEON_WAR_ROUTINE: str | None = None
"""The neon-war worker routine, bound to neon-war only. None until Rolando
creates it on the factory account; until then neon-war can't be onboarded."""

ROUTINES: Mapping[str, str | None] = MappingProxyType(
    {
        PILOT_REPO: PILOT_ROUTINE,
        NEON_WAR_REPO: NEON_WAR_ROUTINE,
    }
)
"""Repository -> the one routine allowed to work on it (None: not yet)."""

REPOSITORIES = frozenset(ROUTINES)

TOKEN_SECRETS: Mapping[str, str] = MappingProxyType(
    {
        PILOT_REPO: "routine-token",
        NEON_WAR_REPO: "neon-war-routine-token",
    }
)
"""Repository -> the service secret holding its routine's start key (each
routine has its own key; ``controller/service/secrets.py``)."""


def routine_for(repository: object) -> str | None:
    """The routine for ``repository``, or None if it has none (or isn't listed)."""
    return ROUTINES.get(repository) if isinstance(repository, str) else None


def token_secret(routine_id: str) -> str:
    """The secret holding ``routine_id``'s start key; KeyError if no listed
    repository uses that routine."""
    for repo, trig in ROUTINES.items():
        if trig is not None and trig == routine_id:
            return TOKEN_SECRETS[repo]
    raise KeyError(f"no repository uses routine {routine_id}")


def live_routines() -> Mapping[str, str]:
    """Repository -> routine, for the repositories that have one now."""
    return MappingProxyType({r: t for r, t in ROUTINES.items() if t is not None})


__all__ = [
    "NEON_WAR_REPO",
    "NEON_WAR_ROUTINE",
    "PILOT_REPO",
    "PILOT_ROUTINE",
    "REPOSITORIES",
    "ROUTINES",
    "TOKEN_SECRETS",
    "live_routines",
    "routine_for",
    "token_secret",
]
