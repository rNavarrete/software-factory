"""Whether a ticket can become a contract as written, or needs Rolando first.

Only questions he can answer as product owner come out of here: behavior
that is missing or unclear, a ticket too big for one task (with a proposed
split), or a request the project's policy doesn't allow (a protected file, a
new dependency, something the factory never does). Everything technical
(files, commits, checks, JSON) is the factory's to decide and never asked.

The checks are plain rules over the cleaned ticket text, not a model, so the
same ticket always gets the same answer and nothing in the ticket can talk
its way past them. They only ever stop work; none of them widens a contract.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from controller.prepare.policy import ProjectPolicy, matches
from controller.prepare.ticket import Reading, clean

_UNCLEAR_RE = re.compile(
    r"\?\s*$|\bTBD\b|\bTBC\b|\bTODO\b|\?\?|\(\?\)|\b(?:[Nn]ot sure|[Uu]nsure)\b"
)
_DEPENDENCY_RE = re.compile(
    r"\b(?:npm (?:i|install|add)\b|yarn add\b|pnpm add\b|pip install\b"
    r"|(?:add|install|bring in)\s+(?:[\w@/.-]+\s+){0,3}?(?:as\s+)?(?:a\s+|an\s+|the\s+)?"
    r"(?:new\s+)?(?:runtime\s+|npm\s+|third[- ]party\s+)?(?:dependency|dependencies|package)\b"
    r"|new (?:runtime )?dependenc(?:y|ies)\b)",
    re.I,
)
_NEVER_RE = re.compile(
    r"\b(?:push(?:ing)? (?:it |this |directly )?(?:straight )?to main|force[- ]push"
    r"|merge (?:the pr|the pull request|this pr|your pr|it into main)|auto[- ]?merge"
    r"|approve (?:it|this|the pr|the pull request|your own)\b"
    r"|(?:trigger|run|start) the release|release workflow|workflow_dispatch"
    r"|deploy(?:ing)? (?:it |this )?to production"
    r"|(?:skip|disable|weaken|bypass) (?:the |all |any )?(?:tests?|checks?|ci)\b"
    r"|(?:remove|delete) (?:the |all |any )?(?:tests|test assertions|assertions|ci checks)\b"
    r"|ignore (?:all |any |the |previous |prior |your |above )*(?:instructions|rules|claude\.md)"
    r"|(?:api|access|secret) (?:key|token)s?\b|branch protection|ruleset)",
    re.I,
)
_HIDDEN = dict.fromkeys(map(ord, "‌‍­"))
"""Kept in the contract (emoji, Persian, Hindi need them) but removed before
screening, so they can't split a phrase the screen looks for."""
_MAX_GOAL, _MAX_TITLE, _MAX_SPLIT_PARTS = 3000, 250, 5


@dataclass(frozen=True)
class Stop:
    kind: str
    """``product``, ``split`` or ``scope`` (``controller.service.seams.QUESTION_KINDS``)."""
    text: str


def _quote(text: str, limit: int = 160) -> str:
    text = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return f"“{text}”"


def _tokens(pattern: str) -> tuple[str, str]:
    """(literal, regex) a ticket would use to name this protected glob."""
    literal = re.split(r"\*", pattern, maxsplit=1)[0].rstrip("/")
    if "*" not in pattern:
        return literal, re.escape(literal)
    if literal.startswith("."):
        return literal, re.escape(literal) + r"(?![\w-])"
    return literal + "/", re.escape(literal) + "/"


def _mentioned_protected(text: str, policy: ProjectPolicy) -> list[str]:
    """Protected paths the ticket names (a file name, or a folder followed by /)."""
    found = []
    for p in policy.protected_paths:
        literal, rx = _tokens(p)
        if literal and re.search(r"(?<![\w.-])" + rx, text, re.I):
            found.append(literal)
    return sorted(set(found))


def screen(reading: Reading, full_text: str, policy: ProjectPolicy) -> Stop | None:
    """The first reason this ticket can't be drafted as written, or None.

    ``full_text`` is the raw title and description. The rules run on the same
    cleaned text the contract is built from, so invisible characters or
    mention tags can't hide a phrase from them."""
    text = clean(full_text).translate(_HIDDEN)
    if not reading.title:
        return Stop("product", "The ticket has no title. What should this task be called?")
    if len(full_text) > policy.max_ticket_chars:
        if len(reading.criteria) > policy.max_criteria:
            return _split(reading, policy, f"it has {len(reading.criteria)} acceptance criteria")
        return too_long(len(full_text), policy)
    if not reading.criteria:
        return Stop(
            "product",
            "What should be true when this is done? Please add the acceptance criteria as a"
            " checklist under an “Acceptance criteria” heading, one line per thing to check.",
        )
    if len(reading.title) > _MAX_TITLE or len(reading.goal) > _MAX_GOAL:
        return Stop(
            "product",
            "The title or outcome is too long to work from as one goal. Could you say the"
            " outcome in a few sentences and put the detail in the acceptance criteria?",
        )
    scope = _scope(text, policy)
    if scope is not None:
        return scope
    unclear = [c for c in reading.criteria if _UNCLEAR_RE.search(c)]
    if unclear:
        lines = "\n".join(f"- {_quote(c)}" for c in unclear)
        return Stop(
            "product",
            "Some acceptance criteria are still open questions, so the factory can't tell what"
            f" done looks like:\n{lines}\nWhat should each of these be?",
        )
    long = [c for c in reading.criteria if len(c) > 1000]
    if long:
        return Stop(
            "product",
            f"The criterion {_quote(long[0])} is very long. Could you break it into shorter,"
            " separately checkable lines?",
        )
    repeated = sorted({c for c in reading.criteria if reading.criteria.count(c) > 1})
    if repeated:
        return Stop(
            "product",
            f"The criterion {_quote(repeated[0])} appears more than once. Was a different"
            " line meant there?",
        )
    if len(reading.criteria) > policy.max_criteria:
        return _split(reading, policy, f"it has {len(reading.criteria)} acceptance criteria")
    return None


def too_long(chars: int, policy: ProjectPolicy) -> Stop:
    return Stop(
        "split",
        f"This ticket is {chars:,} characters, more than one task should be (the limit is"
        f" {policy.max_ticket_chars:,}). Could you cut it down to one outcome, or split it into"
        " smaller tickets?",
    )


_SEGMENT_RE = re.compile(r"[\w.-]+")
_EXTENSIONS = frozenset(
    "ts tsx js jsx mjs cjs json css scss html htm md svg png jpg jpeg gif ico yml yaml toml"
    " txt sh".split()
)
_SPLIT_RE = re.compile(r"[\s,;()\[\]{}<>\"'`|*]+")


def _pathish(word: str) -> bool:
    if "://" in word or word.startswith(("@", "/")):
        return False
    parts = word.split("/")
    if len(parts) > 1:
        head, last = parts[:-1], parts[-1]
        if not all(_SEGMENT_RE.fullmatch(p) for p in head):
            return False
        return last == "" or (
            bool(_SEGMENT_RE.fullmatch(last)) and last.rsplit(".", 1)[-1] in _EXTENSIONS
        )
    # A bare file name ("books.ts") may live in any writable folder; only
    # protected names (screened separately) are asked about.
    return False


def _outside(text: str, policy: ProjectPolicy) -> list[str]:
    """Files or folders the ticket names that no writable path covers. Split
    on spaces and punctuation, then plain string checks: linear in the text."""
    found = set()
    for word in _SPLIT_RE.split(text):
        word = word.rstrip(".:!?")
        if not word or len(word) > 300 or not _pathish(word):
            continue
        probe = word + "x" if word.endswith("/") else word
        if not any(matches(probe, w) or matches(probe + "/x", w) for w in policy.writable_paths):
            found.add(word)
    return sorted(found)


def _scope(text: str, policy: ProjectPolicy) -> Stop | None:
    asks: list[str] = []
    protected = _mentioned_protected(text, policy)
    if protected:
        asks.append(
            "changes to files the factory isn't allowed to touch in this project ("
            + ", ".join(protected)
            + ")"
        )
    else:
        outside = _outside(text, policy)
        if outside:
            asks.append(
                "work on files outside what factory tasks may change here ("
                + ", ".join(outside[:5])
                + ")"
            )
    dep = _DEPENDENCY_RE.search(text)
    if dep:
        asks.append(f"a new dependency ({_quote(dep.group(0), 60)})")
    never = _NEVER_RE.search(text)
    if never:
        asks.append(f"something the factory never does on its own ({_quote(never.group(0), 60)})")
    if not asks:
        return None
    return Stop(
        "scope",
        "This ticket asks for " + "; ".join(asks) + ". A factory task can't include that."
        " Should that part be dropped from the ticket (you can do it by hand), or should the"
        " ticket wait?",
    )


def _split(reading: Reading, policy: ProjectPolicy, why: str) -> Stop:
    n = len(reading.criteria)
    parts = -(-n // policy.max_criteria)
    size = -(-n // parts)
    groups = [reading.criteria[i : i + size] for i in range(0, n, size)]
    if parts > _MAX_SPLIT_PARTS:
        return Stop(
            "split",
            f"This is far too big for one task ({why}; the limit is {policy.max_criteria} per"
            " task). Could you break it into a few smaller tickets, each with its own outcome?"
            " Nothing starts until you do.",
        )
    lines = []
    for i, g in enumerate(groups, 1):
        lines.append(f"Part {i}:")
        lines.extend(f"- {_quote(c, 120)}" for c in g)
    return Stop(
        "split",
        f"This is too big for one task ({why}; the limit is {policy.max_criteria}). One way to"
        f" split it, keeping your criteria in order:\n" + "\n".join(lines) + "\n"
        "If that works, make one ticket per part and move them to Todo. Or tell me a better"
        " split. Nothing starts until you decide.",
    )


__all__ = ["Stop", "screen"]
