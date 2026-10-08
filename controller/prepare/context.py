"""Bounded capture of explicitly selected product sources (ENG-199).

Capture is evidence of retrieval, never evidence of understanding or permission
to dispatch. Readers own authorization and network boundaries. This module
follows no links and interprets no document instructions. Snapshots retain raw
bytes, including incomplete responses, privately; do not post them to Linear.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from controller import contract

MAX_SOURCES = 16
MAX_SOURCE_BYTES = 256_000
MAX_TOTAL_BYTES = 1_000_000
KINDS = frozenset({"project", "notion", "image", "repository"})
REASONS = frozenset(
    {
        "permission-denied",
        "missing-or-inaccessible",
        "temporarily-unavailable",
        "redirect-refused",
        "not-authorized",
        "too-large",
        "invalid-response",
        "incomplete",
        "unsupported-format",
    }
)


class SourceUnavailable(Exception):
    """A safe fixed reason, never an HTTP response body or credential."""

    def __init__(self, reason: str):
        if reason not in REASONS:
            raise ValueError("unknown source failure reason")
        super().__init__(reason)


@dataclass(frozen=True)
class Source:
    key: str
    kind: str
    source_id: str
    url: str
    required: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", self.key):
            raise ValueError("source key must be a short identifier")
        if self.kind not in KINDS or type(self.required) is not bool:
            raise ValueError("invalid source kind or requirement")
        if not self.source_id or len(self.source_id) > 500 or len(self.url) > 4000:
            raise ValueError("invalid source identity")


@dataclass(frozen=True)
class Content:
    data: bytes
    media_type: str
    revision: str | None = None


@dataclass(frozen=True)
class Record:
    source: Source
    content: Content | None
    retrieved_at: datetime
    problem: str | None = None

    def material(self) -> dict:
        c = self.content
        return {
            **asdict(self.source),
            "revision": c.revision if c else None,
            "media_type": c.media_type if c else None,
            "sha256": hashlib.sha256(c.data).hexdigest() if c else None,
            "problem": self.problem,
        }

    def document(self) -> dict:
        c = self.content
        return {
            **self.material(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "bytes": len(c.data) if c else 0,
            "base64": base64.b64encode(c.data).decode("ascii") if c else None,
            "inspection": "not_run",
        }


@dataclass(frozen=True)
class ContextSnapshot:
    records: tuple[Record, ...]

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(
            f"{r.source.key}: {r.problem}" for r in self.records if r.problem and r.source.required
        )

    @property
    def limitations(self) -> tuple[str, ...]:
        return tuple(
            f"{r.source.key}: {r.problem}"
            for r in self.records
            if r.problem and not r.source.required
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "format": "factory-source-snapshot/v1",
                "ready_for_dispatch": False,
                "sources": [r.document() for r in self.records],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def changed_sources(self, current: ContextSnapshot) -> tuple[str, ...]:
        before = {r.source.key: r.material() for r in self.records}
        after = {r.source.key: r.material() for r in current.records}
        return tuple(
            k for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)
        )

    def save(self, root: Path) -> Path:
        """Publish a complete private snapshot atomically; never overwrite an existing one.

        ``root`` is controller-owned storage, not a path obtained from source content.
        The caller binds the returned file/digest to its task record when integration lands.
        """
        data = self.to_bytes()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / (hashlib.sha256(data).hexdigest() + ".json")
        fd, tmp = tempfile.mkstemp(prefix=".capture-", dir=root)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                # A trusted directory can hold the same immutable snapshot already.
                if path.is_symlink() or path.read_bytes() != data:
                    raise ValueError("existing snapshot does not match its digest") from None
        finally:
            os.unlink(tmp)
        return path


def _problem(source: Source, content: Content) -> str | None:
    if not isinstance(content.data, bytes) or not content.data:
        return "invalid-response"
    if source.kind == "notion":
        try:
            body = contract.loads(content.data)
            if (
                body.get("object") != "page_markdown"
                or uuid.UUID(body.get("id", "")) != uuid.UUID(source.source_id)
                or not isinstance(body.get("markdown"), str)
                or not isinstance(body.get("unknown_block_ids"), tuple)
                or type(body.get("truncated")) is not bool
            ):
                return "invalid-response"
            if body["truncated"] or body["unknown_block_ids"] or body.get("warnings"):
                return "incomplete"
        except (ValueError, TypeError, AttributeError):
            return "invalid-response"
    elif source.kind == "image":
        if content.media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            return "unsupported-format"
        # This is deliberately not a pixel-inspection result; readiness stays false.
    else:
        if content.media_type not in {"text/plain", "text/markdown"}:
            return "unsupported-format"
        try:
            content.data.decode("utf-8")
        except UnicodeError:
            return "invalid-response"
    return None


def capture(
    sources: Sequence[Source], read: Callable[[Source], Content], at: datetime
) -> ContextSnapshot:
    """Read exactly the selected references, retaining completeness and provenance.

    Unknown exceptions propagate: a programming error must not masquerade as an
    optional missing source. I/O readers must enforce response limits while reading.
    """
    if len(sources) > MAX_SOURCES or len({s.key for s in sources}) != len(sources):
        raise ValueError("too many or duplicate sources")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("capture time must include a timezone")
    records = []
    total = 0
    for source in sources:
        content = None
        try:
            content = read(source)
            if not isinstance(content, Content) or not isinstance(content.data, bytes):
                content = None
                raise SourceUnavailable("invalid-response")
            if len(content.data) > MAX_SOURCE_BYTES or total + len(content.data) > MAX_TOTAL_BYTES:
                content = None
                raise SourceUnavailable("too-large")
            total += len(content.data)
            problem = _problem(source, content)
        except SourceUnavailable as e:
            problem = str(e)
        records.append(Record(source, content, at, problem))
    return ContextSnapshot(tuple(records))
