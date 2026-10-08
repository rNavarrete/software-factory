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

Each of the three Linear pieces can come from a fixture folder or from
Linear:

- ``--source``: Todo moves (ENG-174). ``fixtures`` reads ``events.json``.
- ``--preparer``: the task contract (ENG-175). ``fixtures`` reads
  ``contracts/<ISSUE-KEY>.json``; ``linear`` drafts it from the ticket with
  the drafting policy (``--drafting``, default ``deploy/pilot/drafting.json``)
  and picks the base commit from GitHub.
- ``--reporter``: the service's messages (ENG-178). ``log`` writes them to
  the service log; ``linear`` posts them on the tickets as the factory's own
  Linear user.

``--live`` sets all three to ``linear`` and needs no fixture folder. Whether
anything is taken on is still the onboarding file's ``intake_enabled``, which
is off unless set. The reviewer (ENG-156) and repairs (ENG-160) are still
fixtures. A real worker is fired only with ``--real-runtime``.

Before its first round, a service that uses Linear checks what it needs and
does not start without it: the ``linear-key`` and ``github-token`` secrets,
a signer that answers (the service reads ticket text, so it must never hold
the approval key), an onboarding file that names the same approver and an
``intake_since``, and a drafting policy for every onboarded project. Nothing
is sent to Linear or GitHub by these checks.
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


DRAFTING_POLICY = Path(__file__).resolve().parents[2] / "deploy" / "pilot" / "drafting.json"
"""The drafting policy in the repo, and in the image at /app/deploy/pilot."""


class AlreadyRunning(Exception):
    pass


class NotReady(Exception):
    """The service is set up wrong and doesn't start: say what to fix."""


def linear_opener():
    """What sends requests to Linear: None is the real API. Tests replace it."""
    return None


def github_opener():
    """What sends requests to GitHub: None is the real API. Tests replace it."""
    return None


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
    config_path = Path(args.onboarding) if args.onboarding else root / "onboarding.json"
    ready_check(args, secrets, socket_path, config_path)
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
        source_routine=FACTORY_ROUTINE,
    )
    gh = HttpGhRunner(lambda: secrets.get("github-token"), github_opener())
    recovery = Recovery(store, approvals, GhCliReader(run=gh), gate=gate, confirm=never)

    def backup(now: datetime) -> None:
        store.backup(backups, now)
        prune_backups(backups, 48)

    fixture_dir = Path(args.fixtures) if args.fixtures else None
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
    forbidden = approvers(args, config_path)
    linear = None
    if "linear" in (args.source, args.preparer):
        from controller.intake.linear import HttpTransport

        # Reads only: Todo moves and ticket text. The reporter has its own.
        linear = HttpTransport(
            lambda: secrets.get("linear-key"), linear_opener(), forbidden_user=forbidden
        )
    if args.source == "linear":
        from controller.intake import LinearSource, policy_from

        def policy():
            return policy_from(
                onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
            )

        source = LinearSource(linear, policy)
    else:
        assert fixture_dir is not None
        source = fixtures.FixtureSource(fixtures.load_events(fixture_dir / "events.json"))
    if args.preparer == "linear":
        from controller.loop.collect import GhApi
        from controller.prepare import LinearTicketReader, Preparer
        from controller.prepare import policy as drafting
        from controller.service.github_http import as_bytes

        drafting_path = Path(args.drafting)
        preparer = Preparer(
            LinearTicketReader(linear),
            GhApi(run=as_bytes(gh)),
            lambda: drafting.load(drafting_path),
        )
    else:
        assert fixture_dir is not None
        preparer = fixtures.FixturePreparer(
            {
                p.stem: json.loads(p.read_text())
                for p in sorted((fixture_dir / "contracts").glob("*.json"))
            }
        )
    integrations = Integrations(
        source=source,
        preparer=preparer,
        reporter=_reporter(args, secrets, forbidden),
        reviewer=_reviewer(args, secrets, store, root) or fixtures.RecordingReviewer(),
        repair=fixtures.NoRepair(),
        authorizer=_authorizer(socket_path) if args.source == "linear" else None,
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


def approvers(args: argparse.Namespace, config_path: Path) -> Callable[[], frozenset[str]]:
    """The Linear users the factory's key must never act as, asked again
    before every request: ``--approver-linear-id`` (Rolando), and whoever the
    onboarding file names as approver now or named earlier in this run. So
    a changed approver applies to keys already checked, and a file that
    can't be read never lifts a restriction."""
    from controller.dispatch.dispatch import FACTORY_ROUTINE
    from controller.recovery import PILOT_REPO
    from controller.service import onboarding

    seen = {args.approver_linear_id}

    def current() -> frozenset[str]:
        try:
            config = onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
        except (OSError, onboarding.OnboardingError):
            pass
        else:
            if config.approver_linear_user_id:
                seen.add(config.approver_linear_user_id)
        return frozenset(seen)

    return current


def ready_check(args: argparse.Namespace, secrets, socket_path: str | None, config_path: Path):
    """Refuse to start a service that would fail every round. Reads only
    local files: nothing is sent to Linear or GitHub."""
    from controller.dispatch.dispatch import FACTORY_ROUTINE
    from controller.recovery import PILOT_REPO
    from controller.service import onboarding

    uses = {args.source, args.preparer, args.reporter}
    if "fixtures" in (args.source, args.preparer) and not args.fixtures:
        raise NotReady("--fixtures is needed unless --source and --preparer are both linear")
    if "linear" not in uses:
        return
    secrets.get("linear-key")  # raises SecretMissing, which says which file
    if args.preparer == "linear":
        secrets.get("github-token")
    if "linear" in (args.source, args.preparer):
        if not socket_path:
            raise NotReady(
                f"reading Linear tickets needs the signer ({SIGNER_SOCKET_ENV}): the process"
                " that reads ticket text must never hold the approval key"
            )
        from controller.signer import SignerKey, SignerUnavailable

        try:
            _ = SignerKey(socket_path).key_id  # answers only if the signer is up
        except SignerUnavailable as e:
            raise NotReady(f"the signer at {socket_path} is not working: {e}") from None
    try:
        config = onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)
    except (OSError, onboarding.OnboardingError) as e:
        raise NotReady(f"the onboarding file {config_path} is unusable: {e}") from None
    named = config.approver_linear_user_id
    if not named:
        raise NotReady(
            f"the onboarding file {config_path} names no approver_linear_user_id, so the"
            " factory can't tell whose moves count or whose identity it must never use"
        )
    if named != args.approver_linear_id:
        raise NotReady(
            "the onboarding file names a different approver than --approver-linear-id, so"
            " the reporter could post as the approver"
        )
    if args.source == "linear":
        from controller.intake import IntakeBlocked, policy_from

        try:
            policy_from(config)
        except IntakeBlocked as e:
            raise NotReady(
                f"intake can't run with the onboarding file {config_path}: {e}"
            ) from None
    if args.preparer == "linear":
        from controller.prepare import policy as drafting

        try:
            policies = drafting.load(Path(args.drafting))
        except drafting.PolicyError as e:
            raise NotReady(f"the drafting policy is unusable: {e}") from None
        for pid, project in config.projects.items():
            policy = policies.project(pid)
            if policy is None:
                raise NotReady(f"the drafting policy has no entry for onboarded project {pid}")
            if policy.base.branch != project.base_branch:
                raise NotReady(
                    f"the drafting policy's base branch for {pid} is {policy.base.branch!r},"
                    f" not the onboarded {project.base_branch!r}"
                )


def _authorizer(socket_path: str | None):
    if not socket_path:
        raise SecretMissing(
            f"--source linear needs the signer ({SIGNER_SOCKET_ENV}): the service never signs"
        )
    from controller.signer import SignerAuthorizer

    return SignerAuthorizer(socket_path)


def run_signer(args: argparse.Namespace) -> int:
    """Start as root: read the approval key, open the socket for the service's
    user, then become the signer's own user for good and answer requests."""
    import grp
    import pwd

    from controller.approval import StaticKey
    from controller.service.secrets import FileSecrets, approval_key_bytes
    from controller.signer import SignerServer, drop_privileges
    from controller.signer.signer import REPAIR_OP

    secrets_dir = os.environ.get(SECRETS_DIR_ENV)
    if not secrets_dir:
        raise SecretMissing(f"set {SECRETS_DIR_ENV} to the folder holding the secret files")
    secrets = FileSecrets(secrets_dir)
    key = StaticKey(approval_key_bytes(secrets))
    extra = {}
    if args.onboarding:
        try:
            linear_key = secrets.get("linear-key")
        except SecretMissing:
            # No Linear key yet: the signer only checks signatures, and no
            # Todo move can be approved. Typed approvals work as before.
            log.warning("no linear-key: Todo-move approval is off")
        else:
            handler = _todo_move_handler(args, key, linear_key)
            extra["authorize"] = handler
            extra[REPAIR_OP] = handler
    signer = pwd.getpwnam(args.user)
    client = pwd.getpwnam(args.client_user)
    group = grp.getgrgid(client.pw_gid).gr_gid
    path = Path(args.socket)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, signer.pw_uid, group)
    os.chmod(path.parent, 0o750)
    server = SignerServer(key, path, allowed_uids={0, client.pw_uid}, extra=extra)
    server.listen()
    os.chown(path, signer.pw_uid, group)
    if args.state:
        state = Path(args.state)
        state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(state.parent, signer.pw_uid, signer.pw_gid)
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


def _todo_move_handler(args: argparse.Namespace, key, linear_key: str):
    """The signer's ``authorize`` request. The Linear key and onboarding path
    are read here, as root, before the signer drops to its own user."""
    from controller.dispatch.dispatch import FACTORY_ROUTINE
    from controller.intake.linear import HttpTransport, LinearSource
    from controller.recovery import PILOT_REPO
    from controller.service import onboarding
    from controller.signer import authorize_handler
    from controller.signer.authorize import OneContractPerMove, TodoMoveAuthorizer

    if not args.state:
        raise SecretMissing("--onboarding needs --state for the signer's record of moves")
    config_path = Path(args.onboarding)

    def load():
        return onboarding.load(config_path, repository=PILOT_REPO, routine_id=FACTORY_ROUTINE)

    transport = HttpTransport(
        lambda: linear_key, forbidden_user=lambda: load().approver_linear_user_id
    )
    reader = LinearSource(transport, lambda: None)  # type: ignore[arg-type,return-value]
    authorizer = TodoMoveAuthorizer(
        key, load, reader.fetch, reader.viewer_id, OneContractPerMove(Path(args.state))
    )
    return authorize_handler(authorizer)


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


ROLANDO_LINEAR_ID = "cd9ec650-f957-4f25-b5f0-9c14bcae49c8"
"""Rolando's Linear user. The factory never posts as this user."""


def _reviewer(args: argparse.Namespace, secrets, store, root: Path):
    """The independent review (ENG-156), when the Codex review workflow is set
    up: ``--review-dispatcher`` (the GitHub login that owns the review token)
    and ``--review-model`` (docs/review.md). Without them the stand-in
    reviewer is used and no review is ever started."""
    if not (args.review_dispatcher or args.review_model):
        return None
    if not (args.review_dispatcher and args.review_model):
        raise SystemExit("--review-dispatcher and --review-model go together")
    from controller.approval import ContractStore
    from controller.recovery import PILOT_REPO
    from controller.review.reviewer import AutoReviewer, ReviewPolicy, ledger_contracts
    from controller.review.workflow import (
        WorkflowConfig,
        WorkflowDispatchRuntime,
        WorkflowResults,
    )
    from controller.service.github_http import HttpGitHubApi

    try:
        config = WorkflowConfig(
            dispatchers=frozenset({args.review_dispatcher}),
            model=args.review_model,
            effort=args.review_effort or "",
        )
    except ValueError as e:
        raise SystemExit(f"review settings: {e}") from None
    token = lambda: secrets.get("review-token")  # noqa: E731
    return AutoReviewer(
        store,
        HttpGitHubApi(lambda: secrets.get("github-token")),
        WorkflowDispatchRuntime(config, token),
        ledger_contracts(store, ContractStore(root / "contracts").load),
        policy=ReviewPolicy(workflow=config.identity),
        repo=PILOT_REPO,
        results=WorkflowResults(HttpGitHubApi(token), config),
    )


def _reporter(args: argparse.Namespace, secrets, forbidden: Callable[[], frozenset[str]]):
    from controller.service import fixtures

    if getattr(args, "reporter", "log") != "linear":
        return fixtures.LogReporter()
    from controller.report.linear_api import HttpTransport
    from controller.report.reporter import LinearReporter

    return LinearReporter(
        HttpTransport(
            lambda: secrets.get("linear-key"),
            linear_opener(),
            forbidden_user=forbidden,
        ),
        approver_id=args.approver_linear_id,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m controller.service")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once"):
        s = sub.add_parser(name)
        s.add_argument("--fixtures", help="folder with events.json and contracts/")
        s.add_argument(
            "--live", action="store_true", help="Todo moves, contracts and messages via Linear"
        )
        s.add_argument("--preparer", choices=("fixtures", "linear"), default="fixtures")
        s.add_argument("--drafting", default=str(DRAFTING_POLICY), help="drafting policy file")
        s.add_argument("--real-runtime", dest="fake_runtime", action="store_false")
        s.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
        s.add_argument("--onboarding", help="mapping file (default: onboarding.json in the home)")
        s.add_argument("--reporter", choices=("log", "linear"), default="log")
        s.add_argument("--approver-linear-id", default=ROLANDO_LINEAR_ID)
        s.add_argument("--source", choices=("fixtures", "linear"), default="fixtures")
        s.add_argument(
            "--review-dispatcher", help="GitHub login that owns the review token (ENG-156)"
        )
        s.add_argument("--review-model", help="OpenAI model for the Codex review (ENG-156)")
        s.add_argument("--review-effort", default="", help="Codex reasoning effort (optional)")
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
    s.add_argument("--onboarding", help="root-owned onboarding file; enables Todo-move approval")
    s.add_argument("--state", help="the signer's record of which contract each move authorized")
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
    if args.live:
        args.source = args.preparer = args.reporter = "linear"

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
    except (SecretMissing, NotReady) as e:
        print(f"Not started: {e}.", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
