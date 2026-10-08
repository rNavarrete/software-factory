"""The loop command. Rolando runs it by hand on his Mac, from the checkout:

    python3 -m controller.loop run <contract.json>

That one command takes a task from its contract to a PR ready for his review
(see loop.py). Run it again whenever: it picks up where the task is. It stops
only where he must act: the approval code, what he saw for a behavior he was
asked to check, a flag to look at, and the code that closes out a merged task.

Less common:

    python3 -m controller.loop run <contract.json> --wait 0   (look once, don't wait)
    python3 -m controller.loop time <minutes> "<what you did>" [--task T] [--why "<reason>"]
    python3 -m controller.loop times                          (your logged minutes, as CSV)

It uses the same ledger as ``python3 -m controller`` (~/.software-factory).
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from controller import contract as contracts
from controller.approval import ApprovalRefused
from controller.dispatch import Refused
from controller.interfaces import LedgerLocked, TaskId
from controller.ledger import LedgerError
from controller.loop.decisions import Timer, time_entries
from controller.loop.loop import DEFAULT_WAIT_MINUTES, Asker, Loop
from controller.recovery import GitHubUnreadable, RecoveryRefused


def _now() -> datetime:
    return datetime.now(UTC)


def _tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _real() -> Loop:
    from controller.approval import Approvals, ContractStore, KeychainKey, tty_confirm
    from controller.attempts import AttemptGate
    from controller.dispatch import Dispatcher, GhBaseCheck
    from controller.ledger import SqliteLedgerStore
    from controller.ledger.store import DEFAULT_HOME
    from controller.loop.collect import GhApi
    from controller.loop.decisions import ReviewDecisions
    from controller.recovery import GhCliReader, Recovery

    store = SqliteLedgerStore()  # ~/.software-factory, created 0700
    home = DEFAULT_HOME.expanduser()
    timer = Timer(store, _now, interactive=_tty)
    confirm = timer.confirm(tty_confirm)
    key = KeychainKey()
    gate = AttemptGate(store)
    approvals = Approvals(store, key, confirm=confirm, contracts=ContractStore())
    recovery = Recovery(store, approvals, GhCliReader(), gate=gate, confirm=confirm)
    dispatcher = Dispatcher(
        store,
        approvals,
        recovery,
        gate,
        GhBaseCheck(),
        backup=lambda now: store.backup(home / "backups", now),
    )
    return Loop(
        store=store,
        recovery=recovery,
        dispatcher=dispatcher,
        api=GhApi(),
        decisions=ReviewDecisions(store, key, os_user=getpass.getuser()),
        timer=timer,
        asker=Asker(sys.stdin, sys.stderr),
        reports=home / "reports",
        now=_now,
        sleep=time.sleep,
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m controller.loop", description=__doc__.split("\n")[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="take a task from its contract to a PR ready for review")
    s.add_argument("contract", type=Path)
    s.add_argument("--wait", type=float, default=DEFAULT_WAIT_MINUTES, help="minutes to wait")
    s = sub.add_parser("time", help="log minutes you spent on the factory outside this command")
    s.add_argument("minutes", type=float)
    s.add_argument("activity")
    s.add_argument("--task", default=None)
    s.add_argument("--why", default=None, help="why you had to step in, if you did")
    sub.add_parser("times", help="print your logged minutes as CSV")
    return p


def main(argv: list[str] | None = None, make: Callable[[], Loop] = _real) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    contract = None
    if args.cmd == "run":
        try:
            contract = contracts.loads(args.contract.read_bytes())
        except (OSError, ValueError) as e:
            print(f"Can't read the contract: {e}")
            return 2
    try:
        loop = make()
        if args.cmd == "run":
            return loop.run(contract, wait_minutes=max(args.wait, 0))
        if args.cmd == "time":
            if not 0 < args.minutes <= 24 * 60:
                print("Minutes must be above 0 and at most a day.")
                return 2
            loop.timer.task = TaskId(args.task) if args.task else None
            loop.timer.record(args.minutes, args.activity, args.why, entered_by="Rolando")
            print("Logged.")
            return 0
        for line in time_entries(loop.store):
            print(line)
        return 0
    except Refused as e:
        print("Nothing sent:")
        for b in e.blocks:
            print(f"  - {b.detail} [{b.code}]")
        return 1
    except (
        ApprovalRefused,
        RecoveryRefused,
        GitHubUnreadable,
        LedgerLocked,
        LedgerError,
        LookupError,
        OSError,
        ValueError,
    ) as e:
        print(f"Not done: {e}")
        return 1
