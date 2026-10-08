"""What the factory says on a Linear ticket, in plain English (ENG-178).

Every function here is pure and returns the comment text. Each progress
entry opens with a bold stage name (``**Factory: Working**``), so the
ticket's comments read as its board: the newest entry is where the work
stands. The team's own Linear states and labels are left alone, for two
reasons: the factory must not overwrite a state Rolando set, and intake
(ENG-174) pins a ticket's labels in the text he approved, so a label added
by the factory would cancel his own Todo move.

Text that comes from outside the factory (ticket text, model output,
GitHub, error messages) goes through ``_quote`` or ``_line``: secrets are
redacted, anything that looks like a factory marker is defused, and length
is capped. The factory's markers (``factory-question:`` and the
``factory-ref:`` line the reporter adds) are what ``replies.py`` trusts, so
nothing outside this module may produce one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from controller.ledger.redact import redact
from controller.service.seams import Question

QUESTION_MARK = "factory-question:"
CANDIDATE_MARK = "factory-candidate:"
REF_MARK = "factory-ref:"
_MARK_RE = re.compile(r"(?i)factory\\*-\\*(question|candidate|ref)\\*\s*:")
_LINE_LIMIT = 300
_BLOCK_LIMIT = 4000
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


class Stage(Enum):
    """Where a ticket's work stands, as Rolando sees it."""

    QUEUED = "Queued"
    WAITING = "Waiting"
    WORKING = "Working"
    REVIEWING = "Reviewing"
    REPAIRING = "Repairing"
    NEEDS_DECISION = "Needs your decision"
    READY = "Ready for your review"
    FAILED = "Failed"
    STOPPED = "Stopped"
    UNCLEAR = "Unclear"
    MERGED = "Merged"
    MERGED_EXCEPTION = "Merged as an exception"
    RELEASED = "Released"
    NOTICE = "Notice"


# --- Cleaning outside text ------------------------------------------------------


def defuse(text: str) -> str:
    """Outside text with any factory marker made inert."""
    return _MARK_RE.sub(lambda m: f"factory {m.group(1)} (quoted):", text)


_MARKDOWN_RE = re.compile(r"([\\`*_\[\]!<>|~])")
"""What can make a link, an image, an autolink, emphasis (a fake stage
heading), code or a table. ``(``, ``)`` and a ``#`` inside a line do nothing
without these, so "PR #7 (eng-186-a1)" reads as written."""
_LEADING_RE = re.compile(r"(?m)^(\s*)([#>=+-])")
"""A heading, quote or list started at the beginning of a line."""


def _quote(text: object, limit: int = _BLOCK_LIMIT) -> str:
    """Outside text for a comment: redacted, markers defused, control
    characters (and text-direction tricks) removed, markdown escaped (so it
    can't add links, images or a fake stage heading), length capped."""
    s = _CONTROL_RE.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    s = defuse(redact(s)).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return _LEADING_RE.sub(r"\1\\\2", _MARKDOWN_RE.sub(r"\\\1", s))


def _line(text: object, limit: int = _LINE_LIMIT) -> str:
    """Outside text that must stay on one line."""
    return _quote(" ".join(str(text or "").split()), limit)


def _url(url: object) -> str:
    """A link only if it is a plain https URL; anything else is dropped."""
    s = str(url or "").strip()
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~%/?#=&+:@-]*)?", s):
        return ""
    return redact(s)


def short_sha(commit: str) -> str:
    return commit[:12] if _SHA_RE.fullmatch(commit or "") else _line(commit, 40)


# --- Progress -------------------------------------------------------------------


def progress(
    stage: Stage,
    text: str,
    *,
    worker: str = "",
    wait_reason: str = "",
    pr_url: str = "",
    preview_url: str = "",
) -> str:
    """One progress entry. ``text`` is the factory's own sentence; the
    keyword values may come from outside and are cleaned."""
    lines = [f"**Factory: {stage.value}**", "", _quote(text)]
    facts = []
    if worker:
        facts.append(f"Worker: {_line(worker, 120)}")
    if wait_reason:
        facts.append(f"Waiting for: {_line(wait_reason)}")
    pr = _url(pr_url)
    if pr:
        facts.append(f"Pull request: {pr}")
    preview = _url(preview_url)
    if preview:
        facts.append(f"Preview: {preview}")
    if facts:
        lines += [""] + [f"- {f}" for f in facts]
    return "\n".join(lines)


# --- Questions --------------------------------------------------------------------


NOTICE_KINDS = frozenset({"changed", "factory"})
"""Question kinds (ENG-175) that report a problem instead of asking anything."""


def question(q: Question) -> str:
    """A product question that can be answered in one go.

    In v1 the answer goes into the ticket itself: Rolando updates its text
    and moves it to Todo again, and the contract is drafted from that text
    (ENG-175). The recommendation is labelled as one, and silence is never
    an answer. A ``changed`` or ``factory`` kind is a plain notice.

    Ends with the ``factory-question:`` line ``replies.py`` reads: the
    question's key and its option ids."""
    kind = str(getattr(q, "kind", "product") or "product")
    notice = kind in NOTICE_KINDS
    stage = Stage.STOPPED if notice else Stage.NEEDS_DECISION
    lines = [f"**Factory: {stage.value}**", ""]
    if not notice:
        lines.append("Before the factory can start, it needs an answer:")
    lines.append(_quote(q.text, 1500))
    if q.context:
        lines += ["", f"Why it matters: {_quote(q.context, 1500)}"]
    if q.options and not notice:
        lines += ["", "Options:"]
        for o in q.options:
            lines.append(f"- **{o.id}**: {_line(o.label, 200)}. {_line(o.consequence)}")
    if q.recommended and not notice:
        lines += ["", f"The factory recommends **{q.recommended}**. That is only a suggestion."]
    if q.if_no_answer:
        lines += ["", f"If nobody acts: {_quote(q.if_no_answer, 500)}"]
    if notice:
        lines += ["", "Nothing will start until the ticket is moved to Todo again."]
    else:
        lines += [
            "",
            "To answer, put your decision into the ticket's text, then move the ticket to"
            " Todo again. Nothing starts until then.",
        ]
    ids = ",".join(o.id for o in q.options) if not notice else ""
    lines += ["", f"{QUESTION_MARK} {q.key or 'q'} options={ids or '-'}"]
    return "\n".join(lines)


def question_repeat(q: Question) -> str:
    """The same question came back because the ticket text didn't change.
    Keeps the original meaning: a factory failure or a changed ticket stays
    a stop notice, and only a real question asks for an answer."""
    kind = str(getattr(q, "kind", "product") or "product")
    if kind in NOTICE_KINDS:
        return progress(
            Stage.STOPPED,
            "The factory stopped again, for the same reason as in its notice above:"
            f" {_line(q.text, 500)} Nothing will start until that is resolved.",
        )
    return progress(
        Stage.NEEDS_DECISION,
        "The ticket hasn't changed since the factory's question above, so the answer isn't"
        " in it yet. Update the ticket's text with your answer, then move it to Todo again.",
    )


def observation_request(
    commit: str, prompt: str, *, preview_url: str = "", diff_url: str = ""
) -> str:
    """Asks for a product observation on one exact candidate."""
    lines = [
        "**Factory: Needs your decision**",
        "",
        f"Please look at the change at commit `{short_sha(commit)}`.",
    ]
    preview, diff = _url(preview_url), _url(diff_url)
    if preview:
        lines.append(f"- Preview: {preview}")
    if diff:
        lines.append(f"- Changes: {diff}")
    lines += [
        "",
        _quote(prompt, 1000),
        "",
        "Reply in this thread starting with `Observation:`. It is recorded against that"
        " commit only; a later change needs a new look.",
    ]
    return "\n".join(lines + _candidate(commit))


def _candidate(commit: str) -> list[str]:
    """The marker that binds replies in this thread to one exact commit."""
    return ["", f"{CANDIDATE_MARK} {commit}"] if _SHA_RE.fullmatch(commit or "") else []


# --- Readiness, merge, release ------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    result: str
    """``passed``, ``failed`` or a short note."""


@dataclass(frozen=True)
class Readiness:
    pr_url: str
    commit: str
    """The exact commit that was checked and reviewed."""
    changed: str
    checks: Sequence[Check] = ()
    limitations: Sequence[str] = ()
    preview_url: str = ""


def ready(r: Readiness) -> str:
    lines = [
        "**Factory: Ready for your review**",
        "",
        f"What changed: {_quote(r.changed, 1500)}",
        "",
        f"Reviewed commit: `{short_sha(r.commit)}`",
    ]
    pr = _url(r.pr_url)
    if pr:
        lines.append(f"Pull request: {pr}")
    preview = _url(r.preview_url)
    if preview:
        lines.append(f"Preview: {preview}")
    lines += ["", "What was checked:"]
    if r.checks:
        lines += [f"- {_line(c.name, 120)}: {_line(c.result, 120)}" for c in r.checks]
    else:
        lines.append("- Nothing was recorded as checked.")
    lines += ["", "Limitations:"]
    lines += [f"- {_line(x)}" for x in r.limitations] or ["- None recorded."]
    lines += [
        "",
        "Review and merge on GitHub. Merging does not release anything; a release is"
        " approved separately. To record a product observation on this exact commit, reply"
        " in this thread starting with `Observation:`.",
    ]
    return "\n".join(lines + _candidate(r.commit))


@dataclass(frozen=True)
class ReviewEvidence:
    """What the factory holds about the independent review of a PR."""

    started: bool = False
    reviewed_commit: str = ""
    """The commit the review's verdict is about, if there is one."""
    passed: bool = False
    unresolved: Sequence[str] = field(default_factory=tuple)
    """Findings the review raised that are still open."""


def missing_evidence(evidence: ReviewEvidence, merged_head: str) -> list[str]:
    """Why a merge can't be called fully verified. Empty when it can."""
    missing: list[str] = []
    if not evidence.started:
        missing.append("the independent review never started")
    elif not evidence.reviewed_commit:
        missing.append("the independent review has no verdict yet")
    elif not merged_head:
        missing.append("the merged commit is not recorded, so the review can't be matched to it")
    elif evidence.reviewed_commit != merged_head:
        missing.append(
            f"the review covered `{short_sha(evidence.reviewed_commit)}`, but the merged PR"
            f" ended at `{short_sha(merged_head)}`"
        )
    elif not evidence.passed:
        missing.append("the independent review did not pass")
    missing += [f"open finding: {_line(f)}" for f in evidence.unresolved]
    return missing


def merged(
    pr_number: int | None, merged_head: str, evidence: ReviewEvidence, *, pr_url: str = ""
) -> tuple[Stage, str]:
    """The merge record. A merge without complete review evidence is
    recorded as an exception and lists what is missing."""
    head = f"`{short_sha(merged_head)}`" if merged_head else "an unrecorded commit"
    missing = missing_evidence(evidence, merged_head)
    link = _url(pr_url)
    tail = [f"Pull request: {link}"] if link else []
    if not missing:
        return Stage.MERGED, "\n".join(
            [
                "**Factory: Merged**",
                "",
                f"{_pr(pr_number)} was merged at {head}. The independent review passed on that"
                " exact commit. Nothing has been released; that needs its own approval.",
            ]
            + ([""] + tail if tail else [])
        )
    lines = [
        "**Factory: Merged as an exception**",
        "",
        f"{_pr(pr_number)} was merged at {head} without complete review evidence:",
        *[f"- {m}" for m in missing],
        "",
        "This merge is recorded as an exception, not as verified work. Nothing has been"
        " released; that needs its own approval.",
    ] + ([""] + tail if tail else [])
    return Stage.MERGED_EXCEPTION, "\n".join(lines)


def _pr(number: int | None) -> str:
    return f"PR #{number}" if number else "The pull request"


def released(release: str, commit: str, record: str) -> str:
    """The release record: what was released, from which commit, and the
    signed approval record that allowed it."""
    return (
        f"**Factory: Released**\n\nRelease {_line(release, 120)} went out from commit"
        f" `{short_sha(commit)}`, approved by record {_line(record, 120)}."
    )


# --- Repairs, providers, health -------------------------------------------------


def repair(reason: str, used: int, allowance: int) -> str:
    left = max(allowance - used, 0)
    return progress(
        Stage.REPAIRING,
        f"The factory is repairing a failed check: {_line(reason)}. This is repair {used} of"
        f" {allowance}; {left} left.",
    )


def provider_switch(old: str, new: str, reason: str) -> str:
    return progress(
        Stage.NOTICE,
        f"The factory switched from {_line(old, 80)} to {_line(new, 80)}: {_line(reason)}.",
    )


def health(down_from: str, down_to: str, pending: Sequence[str]) -> str:
    """After a restart: what was down and what is still pending, so nobody
    has to open a terminal to find out."""
    lines = [
        "**Factory: Notice**",
        "",
        f"The factory was not running from {_line(down_from, 40)} to {_line(down_to, 40)}."
        " It is back and is catching up on anything it missed in Linear and on GitHub.",
        "Nothing was started twice. A worker that was running then shows as unclear until"
        " it is checked; the factory never treats a missing heartbeat as safe to restart.",
    ]
    if pending:
        lines += ["", "Pending work:"] + [f"- {_line(p, 120)}" for p in pending]
    else:
        lines += ["", "No work is pending."]
    return "\n".join(lines)


# --- Time -------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeSummary:
    entered_minutes: int | None = None
    """What Rolando entered on the ticket (``time: 20m`` comments)."""
    measured_minutes: int | None = None
    """Time the factory measured itself, if any. It can't see his screen, so
    this is usually unavailable."""


def time_summary(t: TimeSummary) -> str:
    def fmt(m: int | None, what: str) -> str:
        return f"- {what}: unavailable" if m is None else f"- {what}: {m} min"

    return "\n".join(
        [
            "**Factory: Notice**",
            "",
            "Your time on this ticket:",
            fmt(t.entered_minutes, "Entered by you"),
            fmt(t.measured_minutes, "Measured by the factory"),
            "",
            "To add time, comment `time: 15m` (or `time: 1h`). Only your own comments count.",
        ]
    )


__all__ = [
    "CANDIDATE_MARK",
    "NOTICE_KINDS",
    "QUESTION_MARK",
    "REF_MARK",
    "Check",
    "Readiness",
    "ReviewEvidence",
    "Stage",
    "TimeSummary",
    "defuse",
    "health",
    "merged",
    "missing_evidence",
    "observation_request",
    "progress",
    "provider_switch",
    "question",
    "question_repeat",
    "ready",
    "released",
    "repair",
    "short_sha",
    "time_summary",
]
