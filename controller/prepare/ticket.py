"""The ticket as Rolando wrote it, pinned and read without judgement.

``Snapshot`` holds exactly the fields ENG-174 pins as a ticket's ``revision``
(the same sha256 over the same JSON), so the preparer can prove the text it
drafts from is the text Rolando moved to Todo. ``read`` then pulls out the
parts a contract needs: the goal, the acceptance criteria as written, and
everything else as context. It never invents a requirement: every criterion
in a contract is one of Rolando's lines, word for word after cleanup.

Ticket text is untrusted input. Nothing here follows it; it is only split,
cleaned and measured.

Standard library only. Nothing here does I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Snapshot:
    id: str
    key: str
    title: str
    description: str
    project_id: str | None
    team_id: str | None
    parent_id: str | None
    labels: tuple[str, ...]


def revision(s: Snapshot) -> str:
    """sha256 of the work's exact text. Same body and encoding as
    ``controller.intake.linear.revision`` (ENG-174); a test pins the two."""
    body = {
        "id": s.id,
        "key": s.key,
        "title": s.title,
        "description": s.description,
        "project_id": s.project_id,
        "team_id": s.team_id,
        "parent_id": s.parent_id,
        "labels": sorted(s.labels),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Reading:
    """What the ticket says, cleaned but not interpreted."""

    title: str
    goal: str
    criteria: tuple[str, ...]
    """Each acceptance criterion as Rolando wrote it, in order."""
    context: str
    """The rest of the description (other sections), for the worker's inputs."""
    criteria_heading: bool
    """True if the criteria came from an "Acceptance criteria" section, False
    if they were checklist lines found elsewhere (or there are none)."""


# Linear writes mentions as <issue id=".." href="..">ENG-174</issue>; keep the label.
_MENTION_RE = re.compile(r"<(issue|user|project|document|team)\b[^<>]*>([^<]*)</\1>", re.IGNORECASE)
_ATX_RE = re.compile(r" {0,3}#{1,6}[ \t]")
_BOLD_RE = re.compile(r" {0,3}\*\*([^*]+)\*\*:?[ \t]*$")
_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?(.*)$")
_CHECKBOX_RE = re.compile(r"^\s{0,3}[-*+]\s+\[[ xX]\]\s+(.*)$")
_CONTINUATION_RE = re.compile(r"^\s{2,}\S")
_CRITERIA_HEADINGS = re.compile(
    r"^(acceptance criteria|acceptance|criteria|done when|definition of done)\b", re.IGNORECASE
)
_GOAL_HEADINGS = re.compile(r"^(outcome|goal|summary|what|why|problem)\b", re.IGNORECASE)
_KEEP_FORMAT = frozenset("\u200c\u200d")
"""Joiners that emoji and some scripts need; the screen ignores them."""
_SPACE_RE = re.compile(r"[^\S\n]+")
"""Every kind of space (tabs, no-break and other Unicode spaces), so later
patterns only ever see a plain single space between words."""


def clean(text: str) -> str:
    """NFC, mentions reduced to their label, control and invisible format
    characters removed (they can hide text from a reader), runs of spaces
    collapsed. Newlines are kept."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _MENTION_RE.sub(lambda m: m.group(2), text)
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\n\t":
            kept.append(ch)
        elif ch in _KEEP_FORMAT:
            kept.append(ch)
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        elif cat in ("Zl", "Zp"):
            kept.append("\n")
        else:
            kept.append(ch)
    lines = []
    for line in "".join(kept).split("\n"):
        body = line.lstrip()
        indent = " " * (len(line) - len(body))
        lines.append(indent + _SPACE_RE.sub(" ", body).rstrip())
    return "\n".join(lines)


def _heading(line: str) -> str | None:
    """The heading's text, or None. Plain string work, no backtracking."""
    if _ATX_RE.match(line):
        return line.strip().lstrip("#").strip().rstrip("#").strip()
    m = _BOLD_RE.match(line)
    return m.group(1).strip() if m else None


def _items(lines: Sequence[str], pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        m = pattern.match(line)
        if m:
            out.append(m.group(1).strip())
        elif out and _CONTINUATION_RE.match(line) and not _ITEM_RE.match(line):
            out[-1] = f"{out[-1]} {line.strip()}"
    return [_strip_markup(i) for i in out if _strip_markup(i)]


def _strip_markup(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    return text.strip()


def read(s: Snapshot) -> Reading:
    title = clean(s.title).replace("\n", " ").strip()
    lines = clean(s.description).split("\n")
    sections: list[tuple[str, list[str]]] = [("", [])]
    fenced = False
    unfenced = []
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            unfenced.append(line)
    lines = unfenced
    for line in lines:
        h = _heading(line)
        if h is not None:
            sections.append((h, []))
        else:
            sections[-1][1].append(line)

    criteria_lines = [ls for h, ls in sections if h and _CRITERIA_HEADINGS.match(h)]
    if criteria_lines:
        criteria = [c for ls in criteria_lines for c in _items(ls, _ITEM_RE)]
        from_heading = True
    else:
        criteria = _items(lines, _CHECKBOX_RE)
        from_heading = False

    goal_parts = [ls for h, ls in sections if h and _GOAL_HEADINGS.match(h)]
    if goal_parts:
        goal_text = "\n".join(line for ls in goal_parts for line in ls).strip()
    else:
        goal_text = "\n".join(
            line for line in sections[0][1] if not _CHECKBOX_RE.match(line)
        ).strip()
    goal = f"{title}. {goal_text}".strip() if goal_text else title

    context_parts = []
    for h, ls in sections[1:]:
        if _CRITERIA_HEADINGS.match(h) or _GOAL_HEADINGS.match(h):
            continue
        body = "\n".join(ls).strip()
        if body:
            context_parts.append(f"{h}: {body}")
    return Reading(
        title=title,
        goal=_collapse(goal),
        criteria=tuple(_collapse(c) for c in criteria),
        context="\n\n".join(context_parts),
        criteria_heading=from_heading,
    )


def _collapse(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", text).strip()


__all__ = ["Reading", "Snapshot", "clean", "read", "revision"]
