"""Drafting the technical side of a contract from a screened ticket.

``Drafter`` is the seam a model-based drafter would plug into later. Its
output is a proposal only: ``review.review`` checks it against the ticket
and the policy before anything is frozen, and a drafter's claim that its
draft is in scope counts for nothing.

``RuleDrafter`` is the v1 drafter. It needs no model and no extra key:

- paths: the project's writable paths from the policy, never anything the
  ticket names;
- actions: what the project allows, minus anything that needs a risk marker;
- checks and budget: the policy's commands and the project's attempt budget;
- evidence per criterion: a criterion that names a function, or reads as
  logic, is proved by the test command; one about the page is checked by
  using the built app; one about documentation by reading it. The statement
  is always Rolando's line unchanged.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from controller.prepare.policy import DRAFTABLE_ACTIONS, ProjectPolicy
from controller.prepare.ticket import Reading

_FUNCTION_RE = re.compile(r"\b[a-z][A-Za-z0-9_]*\(|\b[a-z]+[A-Z][A-Za-z0-9]*\b")
"""A call, or a camelCase name: the criterion is about a function."""
_DOCS_RE = re.compile(r"\b(readme|docs?/|documentation|documented|document)\b", re.I)
_UI_RE = re.compile(
    r"\b(page|screen|button|click|clicking|tap|shows?|shown|displays?|displayed|visible|"
    r"hidden|form|input|field|checkbox|dropdown|select(?:ing)?|menu|list(?:ed)?|label|"
    r"heading|style|colou?r|layout|ui|user can|reader can|the user|the reader)\b",
    re.I,
)

KIND_LOGIC, KIND_UI, KIND_DOCS = "logic", "ui", "docs"


@dataclass(frozen=True)
class Draft:
    permitted_paths: tuple[str, ...]
    permitted_actions: tuple[str, ...]
    verification_commands: tuple[str, ...]
    evidence: tuple[Mapping[str, str], ...]
    """One per criterion, in order."""
    kinds: tuple[str, ...]
    drafter: str
    """Name and version, recorded in the contract's notes."""


class Drafter(Protocol):
    def draft(
        self, reading: Reading, policy: ProjectPolicy, project: Mapping[str, object]
    ) -> Draft: ...


def kind_of(statement: str) -> str:
    if _FUNCTION_RE.search(statement):
        return KIND_LOGIC
    if _DOCS_RE.search(statement):
        return KIND_DOCS
    if _UI_RE.search(statement):
        return KIND_UI
    return KIND_LOGIC


STEPS = {
    KIND_DOCS: "Read the changed documentation at the PR's head commit.",
    KIND_UI: "Build the app at the PR's head commit, open the page and do what the"
    " criterion describes.",
}
"""The only observation steps a contract may carry; the review refuses others."""


def evidence_for(kind: str, statement: str, policy: ProjectPolicy) -> dict[str, str]:
    if kind == KIND_LOGIC:
        return {"type": "automated-check", "command": policy.test_command}
    return {"type": "observable-behavior", "steps": STEPS[kind], "expected": statement}


class RuleDrafter:
    name = "rules/v1"

    def draft(
        self, reading: Reading, policy: ProjectPolicy, project: Mapping[str, object]
    ) -> Draft:
        allowed = set(project.get("allowed_actions") or ())  # type: ignore[arg-type]
        actions = tuple(a for a in DRAFTABLE_ACTIONS if a in allowed)
        kinds = tuple(kind_of(c) for c in reading.criteria)
        return Draft(
            permitted_paths=policy.writable_paths,
            permitted_actions=actions,
            verification_commands=policy.verification_commands,
            evidence=tuple(
                evidence_for(k, c, policy) for k, c in zip(kinds, reading.criteria, strict=True)
            ),
            kinds=kinds,
            drafter=self.name,
        )


__all__ = ["STEPS", "Draft", "Drafter", "RuleDrafter", "evidence_for", "kind_of"]
