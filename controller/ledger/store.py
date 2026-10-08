"""The durable LedgerStore: one SQLite file outside every git checkout.

ADR 0002 section 3: ``~/.software-factory/ledger.db``, mode 0600, WAL,
``synchronous=FULL``. Events are append-only. SQLite triggers refuse any
UPDATE or DELETE on them, and the store refuses to open a ledger whose
triggers are gone, so rewriting history takes a deliberate act outside the
controller. The worker never has a path to this file: it runs on another
machine (G-D2).

The single-writer lock is an ``flock`` on ``<ledger>.lock``. The OS releases it
when the process dies, so a crashed controller never leaves it stuck.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from controller.interfaces import (
    AttemptId,
    LedgerEvent,
    LedgerLocked,
    RunId,
    StoredEvent,
    TaskId,
)
from controller.ledger import kinds
from controller.ledger.redact import redact_json

DEFAULT_HOME = Path("~/.software-factory")
LEDGER_FILE = "ledger.db"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    task TEXT,
    attempt INTEGER,
    fire INTEGER,
    data TEXT NOT NULL,
    CHECK (attempt IS NULL OR task IS NOT NULL),
    CHECK (fire IS NULL OR attempt IS NOT NULL)
);
CREATE INDEX events_task ON events (task, seq);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END;
"""


class LedgerError(Exception):
    """The ledger file is unusable: wrong place, wrong schema, or tampered with."""


def default_path() -> Path:
    return DEFAULT_HOME.expanduser() / LEDGER_FILE


def inside_git_checkout(path: Path) -> Path | None:
    """The checkout root if ``path`` is inside a git working tree, else None."""
    for parent in path.resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


class SqliteLedgerStore:
    """``LedgerStore`` on SQLite. One instance per controller process."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_path()
        checkout = inside_git_checkout(self.path)
        if checkout is not None:
            raise LedgerError(f"the ledger must live outside every git checkout, not in {checkout}")
        _private_dir(self.path.parent)
        _private_file(self.path)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        try:
            self._init_schema()
        except BaseException:
            self._db.close()
            raise
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_fd: int | None = None

    # --- LedgerStore -------------------------------------------------------------

    def append(self, *events: LedgerEvent) -> Sequence[StoredEvent]:
        if self._lock_fd is None:
            raise LedgerLocked("append needs this process to hold the writer lock")
        if not events:
            return []
        clean = [_redacted(e) for e in events]
        for e in clean:
            kinds.check(e)
        rows = [_row(e) for e in clean]
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            seqs = [
                db.execute(
                    "INSERT INTO events (kind, at, task, attempt, fire, data)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                ).lastrowid
                for row in rows
            ]
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return [StoredEvent(s, e) for s, e in zip(seqs, clean, strict=True)]

    def events(self, task: TaskId | None = None) -> Sequence[StoredEvent]:
        sql = "SELECT seq, kind, at, task, attempt, fire, data FROM events"
        args: tuple[object, ...] = ()
        if task is not None:
            sql += " WHERE task = ?"
            args = (task.value,)
        return [_stored(r) for r in self._db.execute(sql + " ORDER BY seq", args)]

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        if self._lock_fd is not None:
            raise LedgerLocked("this process already holds the writer lock")
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LedgerLocked(f"another controller process holds {self._lock_path}") from None
        self._lock_fd = fd
        try:
            yield
        finally:
            self._lock_fd = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # --- Extras --------------------------------------------------------------------

    def backup(self, backups_dir: Path | str, now: datetime) -> Path:
        """Copy the whole ledger with SQLite's online backup (ADR 0002 section 3).

        Dispatch and reconcile call this after they write. Returns the new file.
        """
        target_dir = Path(backups_dir).expanduser()
        _private_dir(target_dir)
        target = target_dir / f"ledger-{now.strftime('%Y%m%dT%H%M%S%fZ')}.db"
        if target.exists():
            raise LedgerError(f"backup {target} already exists")
        _private_file(target)
        dest = sqlite3.connect(target)
        try:
            self._db.backup(dest)
        finally:
            dest.close()
        return target

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SqliteLedgerStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Internals -------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create a new ledger, or check an existing one is intact. An existing
        ledger is never repaired silently: missing triggers mean someone
        changed it outside the controller."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
            if "events" not in names:
                for statement in _SCHEMA.split(";\n"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if row is None or row[0] != str(SCHEMA_VERSION):
            found = row[0] if row else "unknown"
            raise LedgerError(f"ledger schema {found}, this controller needs {SCHEMA_VERSION}")
        if not {"events_no_update", "events_no_delete"} <= names:
            raise LedgerError("the ledger's append-only triggers are missing; it was changed")


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _private_file(path: Path) -> None:
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)


def _redacted(event: LedgerEvent) -> LedgerEvent:
    data = redact_json(_thaw(event.data))
    assert isinstance(data, dict)
    return LedgerEvent(event.kind, event.at, event.task, event.attempt, event.run, data)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(v) for v in value]
    return value


def _row(e: LedgerEvent) -> tuple[object, ...]:
    data = json.dumps(_thaw(e.data), ensure_ascii=False, allow_nan=False)
    return (
        e.kind,
        e.at.isoformat(),
        e.task.value if e.task else None,
        e.attempt.number if e.attempt else None,
        e.run.fire if e.run else None,
        data,
    )


def _stored(row: tuple) -> StoredEvent:
    seq, kind, at, task, attempt, fire, data = row
    task_id = TaskId(task) if task is not None else None
    attempt_id = AttemptId(task_id, attempt) if attempt is not None and task_id else None
    run_id = RunId(attempt_id, fire) if fire is not None and attempt_id else None
    event = LedgerEvent(
        kind, datetime.fromisoformat(at), task_id, attempt_id, run_id, json.loads(data)
    )
    return StoredEvent(seq, event)
