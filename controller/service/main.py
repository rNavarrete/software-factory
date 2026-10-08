"""``python3 -m controller.service``: the factory's background service.

    python3 -m controller.service run --fixtures <dir>   run until stopped
    python3 -m controller.service once --fixtures <dir>  one round, then exit
    python3 -m controller.service status                 the queue and outbox
    python3 -m controller.service install-secrets <dir>  host start-up step
    python3 -m controller.service signer --socket <path>  the key-holding process

On the host the service and the signer run as two different users. The
signer (``controller/signer``) reads the approval key as root, then becomes
``factory-signer``; the service becomes ``factory`` and holds a ``SignerKey``
that can check signatures but never make one (``FACTORY_SIGNER_SOCKET``).
Without that variable (tests, a laptop) the service reads the key file
itself, as before.

``--source linear`` reads Rolando's Todo moves from Linear (ENG-174, see
controller/intake); the default ``fixtures`` reads ``events.json``.

Everything lives under the factory home (``~/.software-factory``, the same
folder the ``python3 -m controller`` commands use): ``ledger.db``, ``backups/``, ``contracts/``,
``onboarding.json``, ``service.heartbeat`` and ``service.lock``. Secrets come
from ``$FACTORY_SECRETS_DIR`` (one 0400 file each, see secrets.py).

Only one service runs per home: ``service.lock`` is held for the life of the
process and a second one exits at once. On Fly.io the volume can only be
attached to one machine, which keeps a second copy off another machine too.

The Linear integrations (ENG-174, 175, 178) and the reviewer (ENG-156) are
not built yet, so for now the service runs with the fixtures in a folder:
``events.json`` (Todo moves, refusals, pause/resume) and one contract per
ticket in ``contracts/<ISSUE-KEY>.json``. With ``--fake-runtime`` (the
default with fixtures) no real worker is fired.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from controller.service.secrets import NAMES, SecretMissing, env_name

log = logging.getLogger("factory.service")

DEFAULT_INTERVAL = 60
SECRETS_DIR_ENV = "FACTORY_SECRETS_DIR"
SIGNER_SOCKET_ENV = "FACTORY_SIGNER_SOCKET"


class AlreadyRunning(Exception):
    pass


def home() -> Path:
    """The same folder the ``python3 -m controller`` commands use."""
    from controller.ledger.store import DEFAULT_HOME

    return DEFAULT_HOME.expanduser()


@contextmanager
def service_lock(root: Path) -> Iterator[None]:
    """Held for the whole life of the service process."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / "service.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(f"another service already runs on {root}") from None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def prune_backups(folder: Path, keep: int) -> None:
    files = sorted(folder.glob("ledger-*.db"))
    for old in files[: max(0, len(files) - keep)]:
        old.unlink(missing_ok=True)


def install_secrets(
    target: Path,
    environ: dict[str, str] | None = None,
    *,
    only: tuple[str, ...] = NAMES,
    owner: str | None = None,
) -> list[str]:
    """Move the ``FACTORY_*`` secrets from the environment into 0400 files in
    ``target`` (a 0700 folder) and out of the environment. Returns the names
    written. Run once at container start, before the service. ``only`` limits
    which secrets go there; ``owner`` gives the folder and files to that user
    (the service's own copy, which never includes the approval key)."""
    environ = os.environ if environ is None else environ
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    ids = None
    if owner is not None:
        import pwd

        entry = pwd.getpwnam(owner)
        ids = (entry.pw_uid, entry.pw_gid)
        os.chown(target, *ids)
    written = []
    for name in NAMES:
        if name not in only:
            continue
        value = environ.pop(env_name(name), None)
        if not value or not value.strip():
            continue
        path = target / name
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "w") as f:
            f.write(value.strip())
        if ids is not None:
            os.chown(path, *ids)
        written.append(name)
    return written


def build(args: argparse.Namespace):
    """The real wiring on the host. Imported late so ``status`` stays light."""
    from controller.adapter.fake import FakeRuntimeAdapter, FakeStep
    from controller.approval import Approvals, ContractStore, StaticKey
    from controller.attempts import AttemptGate
    from controller.dispatch import Dispatcher, GhBaseCheck
    from controller.dispatch.dispatch import FACTORY_ROUTINE
    from controller.ledger import SqliteLedgerStore
    from controller.recovery import PILOT_REPO, GhCliReader, Recovery
    from controller.service import fixtures, onboarding
    from controller.service.github_http import HttpGhRunner
    from controller.service.seams import Integrations
    from controller.service.secrets import FileSecrets, approval_key_bytes
    from controller.service.service import Heartbeat, Service, never

    root = home()
    secrets_dir = os.environ.get(SECRETS_DIR_ENV)
    if not secrets_dir:
        raise SecretMissing(f"set {SECRETS_DIR_ENV} to the folder holding the secret files")
    secrets = FileSecrets(secrets_dir)
    socket_path = os.environ.get(SIGNER_SOCKET_ENV)
    if socket_path:
        from controller.signer import SignerKey

        key = SignerKey(socket_path)
    else:
        key = StaticKey(approval_key_bytes(secrets))

    store = SqliteLedgerStore(root / "ledger.db")
    backups = root / "backups"
    gate = AttemptGate(store)
    approvals = Approvals(
        store,
        key,
        confirm=never,
        os_user="factory-service",
        contracts=ContractStore(root / "contracts"),
    )
    gh = HttpGhRunner(lambda: secrets.get("github-token"))
    recovery = Recovery(store, approvals, GhCliReader(run=gh), gate=gate, confirm=never)

    def backup(now: datetime) -> None:
        store.backup(backups, now)
        prune_backups(backups, 48)

    fixture_dir = Path(args.fixtures)
    if args.fake_runtime:
        fake = FakeRuntimeAdapter([FakeStep.launch()] * 100)
        adapter = lambda trig, key: fake  # noqa: E731
        start_key = lambda trig: "fake"  # noqa: E731
    else:
        from controller.adapter.routine import RoutineAdapter

        adapter = lambda trig, key: RoutineAdapter(trig, start_key=key)  # noqa: E731
        start_key = lambda trig: secrets.get("routine-token")  # noqa: E731

    dispatcher = Dispatcher(
        store,
        approvals,
        recovery,
        gate,
        GhBaseCheck(run=gh),
        adapter=adapter,
        start_key=start_key,
        backup=backup,
    )
    contracts_by_issue = {
        p.stem: json.loads(p.read_text())
        for p in sorted((fixture_dir / "contracts").glob("*.json"))
    }
    if args.source == "linear":
        from controller.intake import LinearSource, policy_from
        from controller.intake.linear import HttpTransport

        def policy():
            return policy_from(
                onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
            )

        source = LinearSource(HttpTransport(lambda: secrets.get("linear-key")), policy)
    else:
        source = fixtures.FixtureSource(fixtures.load_events(fixture_dir / "events.json"))
    config_path = Path(args.onboarding) if args.onboarding else root / "onboarding.json"
    integrations = Integrations(
        source=source,
        preparer=fixtures.FixturePreparer(contracts_by_issue),
        reporter=fixtures.LogReporter(),
        reviewer=fixtures.RecordingReviewer(),
        repair=fixtures.NoRepair(),
    )
    service = Service(
        store,
        dispatcher,
        recovery,
        gate,
        ContractStore(root / "contracts"),
        lambda: onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE),
        integrations,
        heartbeat=Heartbeat(root / "service.heartbeat"),
        backup=backup,
        instance=os.environ.get("FLY_MACHINE_ID", os.uname().nodename),
    )
    return service, store


def run_signer(args: argparse.Namespace) -> int:
    """Start as root: read the approval key, open the socket for the service's
    user, then become the signer's own user for good and answer requests."""
    import grp
    import pwd

    from controller.approval import StaticKey
    from controller.service.secrets import FileSecrets, approval_key_bytes
    from controller.signer import SignerServer, drop_privileges

    secrets_dir = os.environ.get(SECRETS_DIR_ENV)
    if not secrets_dir:
        raise SecretMissing(f"set {SECRETS_DIR_ENV} to the folder holding the secret files")
    key = StaticKey(approval_key_bytes(FileSecrets(secrets_dir)))
    signer = pwd.getpwnam(args.user)
    client = pwd.getpwnam(args.client_user)
    group = grp.getgrgid(client.pw_gid).gr_gid
    path = Path(args.socket)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, signer.pw_uid, group)
    os.chmod(path.parent, 0o750)
    server = SignerServer(key, path, allowed_uids={0, client.pw_uid})
    server.listen()
    os.chown(path, signer.pw_uid, group)
    drop_privileges(args.user)
    stopping = False

    def on_signal(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("signer ready on %s for uid %s", path, client.pw_uid)
    try:
        server.serve_forever(lambda: stopping)
    finally:
        server.close()
    return 0


def run_forever(
    tick: Callable[[], object],
    interval: float,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Rounds until ``stop()``; a stop request lets the current round finish."""
    rounds = 0
    while not stop():
        report = tick()
        rounds += 1
        for e in getattr(report, "errors", ()):
            log.warning("%s", e)
        waited = 0.0
        while waited < interval and not stop():
            sleep(min(1.0, interval - waited))
            waited += 1.0
    return rounds


def status(root: Path) -> int:
    from controller.ledger import SqliteLedgerStore
    from controller.service.queue import ServiceView

    store = SqliteLedgerStore(root / "ledger.db")
    view = ServiceView.build(store.events())
    print(f"cursor: {view.cursor}")
    for item in view.items.values():
        print(f"{item.issue_key:10} {item.task}  {item.closed or 'open'}")
    print(f"messages waiting to post: {len(view.outbox)}")
    for m in view.outbox.values():
        print(f"  {m.key} ({m.failures} failed tries)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m controller.service")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once"):
        s = sub.add_parser(name)
        s.add_argument("--fixtures", required=True, help="folder with events.json and contracts/")
        s.add_argument("--real-runtime", dest="fake_runtime", action="store_false")
        s.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
        s.add_argument("--onboarding", help="mapping file (default: onboarding.json in the home)")
        s.add_argument("--source", choices=("fixtures", "linear"), default="fixtures")
        s.add_argument("--user", help="become this user before starting (on the host)")
    sub.add_parser("status")
    s = sub.add_parser("install-secrets")
    s.add_argument("target", type=Path)
    s.add_argument("--only", help="comma-separated secret names (default: all)")
    s.add_argument("--owner", help="give the folder and files to this user")
    s = sub.add_parser("signer")
    s.add_argument("--socket", required=True)
    s.add_argument("--user", default="factory-signer")
    s.add_argument("--client-user", default="factory")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.cmd == "install-secrets":
        only = tuple(args.only.split(",")) if args.only else NAMES
        unknown = set(only) - set(NAMES)
        if unknown:
            print(f"unknown secret names: {sorted(unknown)}", file=sys.stderr)
            return 2
        names = install_secrets(args.target, only=only, owner=args.owner)
        print(f"installed: {', '.join(names) or 'nothing'}")
        return 0
    if args.cmd == "signer":
        try:
            return run_signer(args)
        except SecretMissing as e:
            print(f"Not started: {e}.", file=sys.stderr)
            return 4
    if args.cmd == "status":
        return status(home())

    stopping = False

    def on_signal(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("stop requested; finishing the current round")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if args.user:
        from controller.signer import drop_privileges

        drop_privileges(args.user)
    try:
        with service_lock(home()):
            service, store = build(args)
            try:
                if args.cmd == "once":
                    report = service.tick()
                    print(json.dumps(report.__dict__, default=str, indent=2))
                    return 1 if report.errors else 0
                run_forever(service.tick, args.interval, lambda: stopping)
                return 0
            finally:
                store.close()
    except AlreadyRunning as e:
        print(f"Not started: {e}.", file=sys.stderr)
        return 3
    except SecretMissing as e:
        print(f"Not started: {e}.", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
