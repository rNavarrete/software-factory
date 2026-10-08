"""Read the independent mapper's review from a comment on the worker's PR (ENG-145).

``verify.assertions`` needs someone other than the worker to say which
assertion checks each criterion, and to show the test failing without the
change. In the pilot that someone is a read-only verifier (a Claude session
Rolando starts, or Rolando himself). It posts its work as one comment on the
PR, holding a fenced block::

    ```factory-review/v1
    {"contract_digest": "<64 hex>", "commit": "<head>", "base_commit": "<base>",
     "merge_base": "<merge base>",
     "links": [{"criterion": "ac1", "path": "tests/books.test.ts",
                "test": "filterByStatus > keeps order", "assertion": "expect(...)...",
                "why": "..."}],
     "proofs": [{"criterion": "ac1", "path": "...", "test": "...",
                 "outcome": "failed-assertion", "output_excerpt": "..."}],
     "limits": [{"criterion": "ac2", "reason": "..."}]}
    ```

Who made the mapping and ran the proofs is the comment's author as GitHub
records it, never a name in the JSON, and only logins on ``mappers`` count:
the pilot repo is public, so anyone can comment. The proofs' link is the
comment itself. A review names the exact contract digest and revision it was
made for; the newest valid one for this revision wins, so a corrected review
(always a new comment) replaces an earlier one, and a review of an older push
is never reused. An edited comment is never read: GitHub lets anyone with write
access, the worker included, edit a comment without changing its author.

A review comment carries mapping work only. Clearing a flag and observing a
behavior are Rolando's own decisions; they are typed at his terminal by the
loop (``controller.loop``) and never read from GitHub.

Standard library only; no I/O here (the collector reads the comments).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from verify.assertions import AssertionLink, FailureProof, FailureProofLimit, ProofOutcome
from verify.criteria import Candidate

MARKER = "factory-review/v1"
DEFAULT_MAPPERS = frozenset({"rNavarrete"})
_BLOCK_RE = re.compile(r"^```" + re.escape(MARKER) + r"[ \t]*\n(.*?)\n```[ \t]*$", re.M | re.S)


@dataclass(frozen=True)
class Comment:
    """One PR conversation comment as GitHub records it."""

    id: int
    author: str
    url: str
    body: str
    created_at: str
    updated_at: str
    """Anyone with write access to the repo (the worker too) can edit any
    comment, and the author GitHub shows stays the same. So a review whose
    comment was edited is never read: a correction goes in a new comment."""


@dataclass(frozen=True)
class Review:
    links: tuple[AssertionLink, ...] = ()
    proofs: tuple[FailureProof, ...] = ()
    limits: tuple[FailureProofLimit, ...] = ()
    url: str | None = None
    """The comment the review came from; None if there is none."""
    ignored: tuple[str, ...] = ()
    """Review comments not used, and why."""


def read_review(
    comments: Iterable[Comment],
    contract_digest: str,
    candidate: Candidate,
    *,
    mappers: frozenset[str] = DEFAULT_MAPPERS,
) -> Review:
    """The newest valid review in ``comments`` for this contract and revision."""
    allowed = {m.lower() for m in mappers}
    ignored: list[str] = []
    valid: list[tuple[tuple[str, int], Review]] = []
    for c in comments:
        blocks = _BLOCK_RE.findall(c.body)
        if not blocks:
            continue
        label = f"review comment {c.url} by {c.author!r}"
        if c.updated_at != c.created_at:
            ignored.append(f"edited: {label} was changed after it was posted; post a new one")
            continue
        if c.author.lower() not in allowed:
            ignored.append(f"untrusted: {label}: only {', '.join(sorted(mappers))} may map")
            continue
        if len(blocks) != 1:
            ignored.append(f"ambiguous: {label} holds {len(blocks)} review blocks")
            continue
        try:
            review = _parse(blocks[0], c, contract_digest, candidate)
        except (ValueError, TypeError, KeyError) as e:
            ignored.append(f"unreadable: {label}: {e}")
            continue
        if isinstance(review, str):
            ignored.append(f"stale: {label}: {review}")
            continue
        valid.append(((c.created_at, c.id), review))
    if not valid:
        return Review(ignored=tuple(ignored))
    valid.sort(key=lambda v: v[0])
    for _, older in valid[:-1]:
        ignored.append(f"replaced: review comment {older.url} has a newer review")
    r = valid[-1][1]
    return Review(r.links, r.proofs, r.limits, r.url, tuple(ignored))


def _parse(text: str, c: Comment, want: str, candidate: Candidate) -> Review | str:
    data = json.loads(text)
    if not isinstance(data, Mapping):
        raise ValueError("the review block is not a JSON object")
    head, base = candidate.head_commit, candidate.base_commit
    if data.get("contract_digest") != want:
        return f"it is for contract {str(data.get('contract_digest'))[:12]}, not {want[:12]}"
    expected = (("commit", head), ("base_commit", base), ("merge_base", candidate.merge_base))
    for key, value in expected:
        if data.get(key) != value:
            return f"its {key} is {str(data.get(key))[:12]}, not {value[:12]}"
    links = tuple(
        AssertionLink(
            criterion=_s(x, "criterion"),
            path=_s(x, "path"),
            test=_s(x, "test"),
            assertion=_s(x, "assertion"),
            contract_digest=want,
            commit=head,
            base_commit=base,
            mapper=c.author,
            why=_s(x, "why"),
        )
        for x in _items(data, "links")
    )
    proofs = tuple(
        FailureProof(
            criterion=_s(x, "criterion"),
            path=_s(x, "path"),
            test=_s(x, "test"),
            contract_digest=want,
            tests_commit=head,
            code_commit=candidate.merge_base,
            outcome=ProofOutcome(_s(x, "outcome")),
            by=c.author,
            url=c.url,
            output_excerpt=str(x.get("output_excerpt") or ""),
        )
        for x in _items(data, "proofs")
    )
    limits = tuple(
        FailureProofLimit(
            criterion=_s(x, "criterion"),
            contract_digest=want,
            commit=head,
            base_commit=base,
            by=c.author,
            reason=_s(x, "reason"),
        )
        for x in _items(data, "limits")
    )
    return Review(links, proofs, limits, c.url)


def _items(data: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(x, Mapping) for x in value):
        raise ValueError(f"{key} must be a list of objects")
    return value


def _s(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value


def review_block(
    contract_digest: str,
    candidate: Candidate,
    links: Iterable[Mapping[str, str]] = (),
    proofs: Iterable[Mapping[str, str]] = (),
    limits: Iterable[Mapping[str, str]] = (),
) -> str:
    """The fenced block a mapper posts, for the verifier's procedure."""
    body = {
        "contract_digest": contract_digest,
        "commit": candidate.head_commit,
        "base_commit": candidate.base_commit,
        "merge_base": candidate.merge_base,
        "links": list(links),
        "proofs": list(proofs),
        "limits": list(limits),
    }
    return f"```{MARKER}\n{json.dumps(body, indent=2, ensure_ascii=False)}\n```"
