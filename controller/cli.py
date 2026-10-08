"""The ``factory`` command. Rolando runs it by hand on his Mac:

    python3 -m controller dispatch <contract.json>
    python3 -m controller status [<task>]
    python3 -m controller reconcile <task>
    python3 -m controller clear <task> <session_url>
    python3 -m controller close <task> failed|canceled <reason>

Day to day, ``dispatch`` is the only command: it shows the contract and asks
for the approval code if this exact contract isn't approved yet, then starts
one worker. Running it again for the same contract never starts a second
worker; it says where the first one stands. After a definite not-launched
answer (a rate limit, say), running it again asks for the re-fire code once
the wait is over.

Less common:

    python3 -m controller approve <contract.json>
    python3 -m controller revoke <contract.json> <reason>
    python3 -m controller repair <contract.json> <what failed>
    python3 -m controller found <task> found|duplicates|not-found <how checked> [<session_url>...]
    python3 -m controller clear <task> --basis <basis> [--note <note>] [<session_url>...]
    python3 -m controller snapshot <session %> <weekly %> <credits spent>
    python3 -m controller hold <reason> <note>
    python3 -m controller resume <note> [--reason <hold>]

Every command uses the one ledger at ~/.software-factory/ledger.db and is
backed up to ~/.software-factory/backups after dispatch and reconcile. Each
``<task>`` command acts on the task's latest attempt unless ``--attempt N``
names another.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from controller import contract as contracts
from controller.approval import ApprovalRefused, Approvals
from controller.attempts import AttemptGate
from controller.attempts.events import FIRE_INTENT, FIRE_RESULT, ClearingBasis
from controller.attempts.policy import LedgerView
from controller.dispatch import Dispatcher, Refused
from controller.interfaces import AttemptId, LedgerLocked, LedgerStore, TaskId
from controller.ledger import LedgerError
from controller.recovery import Finding, GitHubUnreadable, Recovery, RecoveryRefused, State


@dataclass
class Controller:
    store: LedgerStore
    approvals: Approvals
    recovery: Recovery
    gate: AttemptGate
    dispatcher: Dispatcher
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def _real() -> Controller:
    """On the Mac: Keychain keys and Rolando's ``gh`` login. On the service host
    (``FACTORY_SECRETS_DIR`` set): the host's own secret files and its read-only
    GitHub token (see controller/service/secrets.py). Decisions are still
    confirmed by typing the code at a terminal in both places."""
    import os

    from controller.approval import ContractStore, KeychainKey, StaticKey
    from controller.dispatch import GhBaseCheck
    from controller.ledger import SqliteLedgerStore
    from controller.ledger.store import DEFAULT_HOME
    from controller.recovery import GhCliReader

    store = SqliteLedgerStore()  # ~/.software-factory, created 0700
    backups = DEFAULT_HOME.expanduser() / "backups"
    gate = AttemptGate(store)
    host = os.environ.get("FACTORY_SECRETS_DIR")
    extra: dict[str, object] = {}
    if host:
        from controller.service.github_http import HttpGhRunner
        from controller.service.secrets import FileSecrets, approval_key_bytes

        secrets = FileSecrets(host)
        key = StaticKey(approval_key_bytes(secrets))
        gh = HttpGhRunner(lambda: secrets.get("github-token"))
        reader, base = GhCliReader(run=gh), GhBaseCheck(run=gh)
        extra["start_key"] = lambda trig: secrets.get("routine-token")
    else:
        key, reader, base = KeychainKey(), GhCliReader(), GhBaseCheck()
    approvals = Approvals(store, key, contracts=ContractStore())
    recovery = Recovery(store, approvals, reader, gate=gate)
    dispatcher = Dispatcher(
        store,
        approvals,
        recovery,
        gate,
        base,
        backup=lambda now: store.backup(backups, now),
        **extra,  # type: ignore[arg-type]
    )
    return Controller(store, approvals, recovery, gate, dispatcher)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m controller", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_task(name: str, help: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help)
        s.add_argument("task")
        s.add_argument("--attempt", type=int, default=None)
        return s

    sub.add_parser("dispatch", help="approve if needed, then start one worker").add_argument(
        "contract", type=Path
    )
    sub.add_parser("approve", help="approve a contract without dispatching").add_argument(
        "contract", type=Path
    )
    s = sub.add_parser("revoke", help="withdraw approval of a contract")
    s.add_argument("contract", type=Path)
    s.add_argument("reason")
    s = sub.add_parser("repair", help="allow the next attempt after a failed one")
    s.add_argument("contract", type=Path)
    s.add_argument("failure")
    sub.add_parser("status", help="where tasks stand").add_argument("task", nargs="?")
    with_task("reconcile", "read the attempt's branch and PRs from GitHub")
    s = with_task("found", "record what you found when looking for a session")
    s.add_argument("finding", choices=[f.value for f in Finding])
    s.add_argument("how_checked")
    s.add_argument("session_urls", nargs="*")
    s = with_task("clear", "record that the attempt's session can no longer write")
    s.add_argument("session_urls", nargs="*")
    s.add_argument("--basis", choices=[b.value for b in ClearingBasis], default="completed")
    s.add_argument("--note", default="")
    s = with_task("close", "record that an attempt failed or was canceled")
    s.add_argument("outcome", choices=["failed", "canceled"])
    s.add_argument("reason")
    s = sub.add_parser("snapshot", help="record your claude.ai usage reading")
    s.add_argument("session_pct", type=float)
    s.add_argument("weekly_pct", type=float)
    s.add_argument("credits_spent", type=float)
    s = sub.add_parser("hold", help="stop all dispatch until resume")
    s.add_argument("reason")
    s.add_argument("note")
    s = sub.add_parser("resume", help="lift a hold")
    s.add_argument("note")
    s.add_argument("--reason", default=None)
    return p


def main(argv: list[str] | None = None, make: Callable[[], Controller] = _real) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        contract = _load(args.contract) if hasattr(args, "contract") else None
    except (OSError, ValueError) as e:
        print(f"Can't read the contract: {e}")
        return 2
    try:
        return _run(make(), args, contract)
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
    ) as e:
        print(f"Not done: {e}")
        return 1
    except ValueError as e:
        print(f"Not done: {e}")
        return 2


def _load(path: Path) -> Mapping[str, object]:
    return contracts.loads(path.read_bytes())


def _attempt(c: Controller, args: argparse.Namespace) -> AttemptId:
    task = TaskId(args.task)
    if args.attempt is not None:
        return AttemptId(task, args.attempt)
    attempts = LedgerView.build(c.store.events(task)).task_attempts(task)
    if not attempts:
        raise ValueError(f"{task} has no attempts on record")
    return attempts[-1].attempt


def _run(c: Controller, args: argparse.Namespace, contract) -> int:
    now = c.now()
    cmd = args.cmd
    if cmd == "dispatch":
        result = c.dispatcher.dispatch(contract)
        print(result.message)
        if result.status is not None and result.run is not None:
            print(f"Next: {result.status.next_step}")
        return 0 if result.outcome in ("launched", "already-dispatched") else 1
    if cmd == "approve":
        c.approvals.approve(contract, now)
        print("Approved. Nothing was sent; run dispatch to start the worker.")
    elif cmd == "revoke":
        task = TaskId(str(contract["task_id"]))
        c.approvals.revoke(task, contracts.digest(contract), args.reason, now)
        print("Approval withdrawn.")
    elif cmd == "repair":
        task = TaskId(str(contract["task_id"]))
        attempts = LedgerView.build(c.store.events(task)).task_attempts(task)
        if not attempts:
            raise ValueError(f"{task} has no attempt to repair")
        number = attempts[-1].attempt.number + 1
        c.approvals.authorize_repair(contract, number, args.failure, now)
        print(f"Attempt {number} allowed. Run dispatch to start it.")
    elif cmd == "status":
        _status(c, args.task, now)
    elif cmd == "reconcile":
        found = c.dispatcher.reconcile(_attempt(c, args))
        _print_attempt(found.status)
        for w in found.warnings:
            print(f"  warning: {w}")
        for h in found.session_hints:
            print(f"  session mentioned in a PR (a hint, not proof): {h}")
    elif cmd == "found":
        attempt = _attempt(c, args)
        run = c.recovery.attempt_status(attempt, now).latest_run
        if run is None:
            raise ValueError(f"{attempt} was never fired")
        c.recovery.record_launch_finding(
            run, Finding(args.finding), args.session_urls, args.how_checked, now
        )
        print("Recorded.")
    elif cmd == "clear":
        attempt = _attempt(c, args)
        c.recovery.clear(
            attempt,
            ClearingBasis(args.basis),
            now,
            session_urls=args.session_urls,
            note=args.note,
        )
        print(f"{attempt} cleared. The next task can start.")
    elif cmd == "close":
        attempt = _attempt(c, args)
        c.recovery.close_attempt(attempt, State(args.outcome), args.reason, now)
        print(f"{attempt} closed as {args.outcome}.")
    elif cmd == "snapshot":
        c.gate.record_snapshot(now, args.session_pct, args.weekly_pct, args.credits_spent, now)
        print("Usage reading recorded.")
    elif cmd == "hold":
        c.gate.hold(args.reason, args.note, now)
        print("On hold. Nothing will be dispatched until resume.")
    elif cmd == "resume":
        c.gate.resume(args.note, now, args.reason)
        print("Hold lifted.")
    return 0


def _status(c: Controller, task: str | None, now: datetime) -> None:
    c.recovery.recover(now)
    view = LedgerView.build(c.store.events())
    tasks = [TaskId(task)] if task else sorted({a.task for a in view.attempts})
    if not tasks:
        print("No tasks on record.")
    for t in tasks:
        s = c.recovery.status(t, now)
        print(f"{t}: {s.state.value} (release: {s.release})")
        for a in s.attempts:
            _print_attempt(a)
    blocks = list(c.recovery.blocks(now))
    for b in blocks:
        print(f"Blocks new work: {b.detail} [{b.code}]")
    for a in c.gate.run_alerts(now):
        print(f"Alert: {a.detail}")
    for reason in view.holds:
        print(f"Hold: {reason}")
    snap = view.snapshot
    if snap is None:
        print("Usage reading: none recorded")
    else:
        print(
            f"Usage reading: {snap.taken_at:%Y-%m-%d %H:%M} UTC, session {snap.session_pct:g}%,"
            f" weekly {snap.weekly_pct:g}%"
        )
    # Every fire on record and what its launch answered. Only "launched" with a
    # session started a worker; an intent with no answer yet is still unclear.
    answers: dict[str, Mapping[str, object]] = {}
    fires: list[str] = []
    for s in c.store.events():
        if s.event.kind == FIRE_INTENT and s.event.run is not None:
            fires.append(str(s.event.run))
        elif s.event.kind == FIRE_RESULT and s.event.run is not None:
            answers[str(s.event.run)] = s.event.data
    for run in fires:
        answer = answers.get(run)
        if answer is None:
            print(f"Fire {run}: no answer yet")
        else:
            print(f"Fire {run}: {answer.get('outcome')} {answer.get('session_url') or ''}".rstrip())
    print(f"Fires on record: {len(fires)}")


def _print_attempt(a) -> None:
    print(f"  {a.attempt}: {a.state.value}. {a.detail}")
    print(f"    session: {a.writer}")
    for url in a.session_urls:
        print(f"    {url}")
    print(f"    next: {a.next_step}")
