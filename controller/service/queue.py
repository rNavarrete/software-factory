"""The service's queue, cursor and outbox, kept as events in the one ledger.

Nothing is stored anywhere else, so the ledger's append-only file, its writer
lock and its backups cover the queue too, and a restarted service rebuilds
exactly what the old one knew from ``ServiceView.build(events)``. Each poll's
results and its new cursor go in one append, so a crash either keeps both or
neither (and the poll is simply read again).

A queue item is one accepted Todo move. Its task id is the ticket key in
lowercase (``eng-186``), which is also how the ledger ties the item to its
attempts: the item owns exactly the attempts of that task made while it is
open. An item closes when its task's attempt is merged or finished, when the
move stops standing, when the project is removed from onboarding, or when the
contract needs a product answer first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from controller.interfaces import LedgerEvent, StoredEvent, TaskId
from controller.service.seams import Authorization, Control, Refusal

SERVICE_STARTED = "service-started"
CONFIG_SEEN = "service-config"
INTAKE_ACCEPTED = "intake-accepted"
INTAKE_REFUSED = "intake-refused"
INTAKE_CURSOR = "intake-cursor"
CONTROL_APPLIED = "control-applied"
ITEM_CONTRACT = "item-contract"
ITEM_CLOSED = "item-closed"
OUTBOX_QUEUED = "outbox-queued"
OUTBOX_SENT = "outbox-sent"
OUTBOX_FAILED = "outbox-failed"
REVIEW_STARTED = "review-started"

KINDS = frozenset(
    {
        SERVICE_STARTED,
        CONFIG_SEEN,
        INTAKE_ACCEPTED,
        INTAKE_REFUSED,
        INTAKE_CURSOR,
        CONTROL_APPLIED,
        ITEM_CONTRACT,
        ITEM_CLOSED,
        OUTBOX_QUEUED,
        OUTBOX_SENT,
        OUTBOX_FAILED,
        REVIEW_STARTED,
    }
)


@dataclass
class Item:
    event_id: str
    task: TaskId
    issue_id: str
    issue_key: str
    project_id: str
    actor: str
    moved_at: str
    revision: str
    seq: int
    digest: str | None = None
    closed: str | None = None

    def authorization(self) -> Authorization:
        return Authorization(
            self.event_id,
            self.issue_id,
            self.issue_key,
            self.project_id,
            self.actor,
            datetime.fromisoformat(self.moved_at),
            self.revision,
            "recorded at intake",
        )


@dataclass
class Message:
    key: str
    issue_id: str
    text: str
    seq: int
    failures: int = 0
    last_failed_at: datetime | None = None


@dataclass
class ServiceView:
    items: dict[str, Item] = field(default_factory=dict)
    """By intake event id, in the order accepted."""
    seen: set[str] = field(default_factory=set)
    """Every intake and control event id recorded, accepted or refused."""
    cursor: str | None = None
    config_sha256: str | None = None
    outbox: dict[str, Message] = field(default_factory=dict)
    """Queued and not yet sent, by key."""
    queued_keys: set[str] = field(default_factory=set)
    reviews: set[str] = field(default_factory=set)
    """Attempts whose review was started."""

    @classmethod
    def build(cls, stored: Iterable[StoredEvent]) -> ServiceView:
        v = cls()
        for s in stored:
            e = s.event
            d = e.data
            if e.kind == INTAKE_ACCEPTED and e.task is not None:
                eid = str(d["event_id"])
                v.seen.add(eid)
                v.items[eid] = Item(
                    eid,
                    e.task,
                    str(d["issue_id"]),
                    str(d["issue_key"]),
                    str(d["project_id"]),
                    str(d["actor"]),
                    str(d["moved_at"]),
                    str(d["revision"]),
                    s.seq,
                )
            elif e.kind in (INTAKE_REFUSED, CONTROL_APPLIED):
                v.seen.add(str(d["event_id"]))
            elif e.kind == INTAKE_CURSOR:
                v.cursor = str(d["cursor"])
            elif e.kind == CONFIG_SEEN:
                v.config_sha256 = str(d["sha256"])
            elif e.kind == ITEM_CONTRACT:
                item = v.items.get(str(d["event_id"]))
                if item is not None:
                    item.digest = str(d["digest"])
            elif e.kind == ITEM_CLOSED:
                item = v.items.get(str(d["event_id"]))
                if item is not None and item.closed is None:
                    item.closed = str(d["reason"])
            elif e.kind == OUTBOX_QUEUED:
                key = str(d["key"])
                if key not in v.queued_keys:
                    v.queued_keys.add(key)
                    v.outbox[key] = Message(key, str(d["issue_id"]), str(d["text"]), s.seq)
            elif e.kind == OUTBOX_SENT:
                v.outbox.pop(str(d["key"]), None)
            elif e.kind == OUTBOX_FAILED:
                m = v.outbox.get(str(d["key"]))
                if m is not None:
                    m.failures += 1
                    m.last_failed_at = e.at
            elif e.kind == REVIEW_STARTED and e.attempt is not None:
                v.reviews.add(str(e.attempt))
        return v

    def open_items(self) -> list[Item]:
        return [i for i in self.items.values() if i.closed is None]

    def open_item_for_issue(self, issue_id: str) -> Item | None:
        return next((i for i in self.open_items() if i.issue_id == issue_id), None)


# --- Event constructors ------------------------------------------------------------


def accepted(a: Authorization, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        INTAKE_ACCEPTED,
        now,
        a.task,
        data={
            "event_id": a.event_id,
            "issue_id": a.issue_id,
            "issue_key": a.issue_key,
            "project_id": a.project_id,
            "actor": a.actor,
            "moved_at": a.moved_at.isoformat(),
            "revision": a.revision,
            "evidence": a.evidence,
        },
    )


def refused(r: Refusal | Authorization, reason: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        INTAKE_REFUSED,
        now,
        data={
            "event_id": r.event_id,
            "issue_id": r.issue_id,
            "issue_key": r.issue_key,
            "reason": reason,
        },
    )


def cursor(value: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(INTAKE_CURSOR, now, data={"cursor": value})


def control(c: Control, applied: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        CONTROL_APPLIED,
        now,
        data={
            "event_id": c.event_id,
            "action": c.action,
            "actor": c.actor,
            "at": c.at.isoformat(),
            "note": c.note,
            "applied": applied,
        },
    )


def item_contract(item: Item, digest: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        ITEM_CONTRACT, now, item.task, data={"event_id": item.event_id, "digest": digest}
    )


def item_closed(item: Item, reason: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        ITEM_CLOSED, now, item.task, data={"event_id": item.event_id, "reason": reason}
    )


def message(key: str, issue_id: str, text: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(OUTBOX_QUEUED, now, data={"key": key, "issue_id": issue_id, "text": text})


def sent(key: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(OUTBOX_SENT, now, data={"key": key})


def send_failed(key: str, error: str, now: datetime) -> LedgerEvent:
    return LedgerEvent(OUTBOX_FAILED, now, data={"key": key, "error": error[:500]})


def started(data: Mapping[str, object], now: datetime) -> LedgerEvent:
    return LedgerEvent(SERVICE_STARTED, now, data=dict(data))


def config_seen(sha256: str, projects: list[str], intake: bool, now: datetime) -> LedgerEvent:
    return LedgerEvent(
        CONFIG_SEEN,
        now,
        data={"sha256": sha256, "projects": projects, "intake_enabled": intake},
    )


__all__ = [
    "KINDS",
    "Item",
    "Message",
    "ServiceView",
]
