"""The controller's durable record (ENG-147, ADR 0002 section 3).

``SqliteLedgerStore`` implements ``controller.interfaces.LedgerStore`` on one
SQLite file at ``~/.software-factory/ledger.db``. ``records`` builds the
ledger's own event kinds (listed in ``kinds``); the attempt gate
(controller/attempts) writes its own kinds into the same store.
"""

from controller.ledger import kinds, records
from controller.ledger.kinds import InvalidEvent
from controller.ledger.redact import redact
from controller.ledger.store import LedgerError, SqliteLedgerStore, default_path

__all__ = [
    "InvalidEvent",
    "LedgerError",
    "SqliteLedgerStore",
    "default_path",
    "kinds",
    "records",
    "redact",
]
