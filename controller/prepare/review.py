"""The check a drafted contract must pass before it is frozen.

It does not trust the drafter. From the ticket reading, the policy, the
onboarding entry, the chosen base and the authorization alone, it works out
what the contract is allowed to contain and refuses anything else:

- the contract validates (schema) and has no criterion still unclear;
- repository, task, base, budget, checks and actions are exactly what the
  policy and onboarding give, and no risk marker or dependency is added;
- every path is one of the policy's writable paths and none is protected;
- the criteria are Rolando's lines, all of them, in order, unchanged, and
  each has evidence a reviewer can check (a policy command, or steps to
  observe);
- the goal is the ticket's own, and ``notes`` names the exact ticket
  revision, the authorization and the policy, so the digest binds them.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Mapping

from controller import contract as contracts
from controller.prepare.drafter import STEPS
from controller.prepare.policy import DRAFTABLE_ACTIONS, ProjectPolicy, matches, overlaps
from controller.prepare.ticket import Reading


def review(
    contract: Mapping[str, object],
    *,
    reading: Reading,
    policy: ProjectPolicy,
    policy_sha256: str,
    project: Mapping[str, object],
    task_id: str,
    base_commit: str,
    revision: str,
    event_id: str,
) -> list[str]:
    """Every reason this contract can't go forward; empty if none."""
    problems = list(contracts.approval_errors(contract))
    if problems:
        return problems

    def want(key: str, value: object) -> None:
        got = contract.get(key)
        if isinstance(got, tuple):
            got = list(got)
        if got != value:
            problems.append(f"{key} is {got!r}, expected {value!r}")

    want("task_id", task_id)
    want("repository", project.get("repository"))
    want("base_commit", base_commit)
    want("attempt_budget", project.get("max_attempts"))
    want("escalate_to", policy.escalate_to)
    want("risk_markers", [])
    want("depends_on", [])
    want("goal", reading.goal)

    want("verification_commands", list(policy.verification_commands))
    allowed_checks = set(project.get("checks") or ())  # type: ignore[arg-type]
    if not set(policy.verification_commands) <= allowed_checks:
        problems.append("the policy's checks are not all onboarded for this project")

    allowed = set(project.get("allowed_actions") or ()) & set(DRAFTABLE_ACTIONS)  # type: ignore[arg-type]
    actions = set(contract["permitted_actions"])  # type: ignore[arg-type]
    if not actions <= allowed:
        problems.append(f"actions outside policy: {sorted(actions - allowed)}")

    for p in contract["permitted_paths"]:  # type: ignore[union-attr]
        if p not in policy.writable_paths:
            problems.append(f"path {p!r} is not one of the policy's writable paths")
        for q in policy.protected_paths:
            if overlaps(p, q) or matches(p, q):
                problems.append(f"path {p!r} overlaps protected {q!r}")

    criteria = contract["acceptance_criteria"]
    statements = [c["statement"] for c in criteria]  # type: ignore[index, union-attr]
    if statements != list(reading.criteria):
        problems.append("the criteria are not exactly the ticket's acceptance criteria, in order")
    for i, c in enumerate(criteria):  # type: ignore[arg-type]
        if c["id"] != f"ac{i + 1}":
            problems.append(f"criterion {i + 1} has id {c['id']!r}")
        ev = c["evidence"]
        if ev["type"] == "human-review":
            problems.append(f"{c['id']} asks Rolando to review; drafting never adds that")
        if ev["type"] == "observable-behavior":
            if ev["expected"] != c["statement"]:
                problems.append(f"{c['id']}'s expected result is not Rolando's statement")
            if ev["steps"] not in STEPS.values():
                problems.append(f"{c['id']}'s steps are not one of the factory's own")

    notes = contract.get("notes")
    for needle, what in (
        (revision, "the ticket revision"),
        (event_id, "the authorization"),
        (policy_sha256, "the drafting policy"),
    ):
        if not isinstance(notes, str) or needle not in notes:
            problems.append(f"notes do not name {what}")
    return problems


__all__ = ["review"]
