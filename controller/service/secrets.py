"""Secrets for the background service, without macOS Keychain or a terminal.

The service needs four secrets, each scoped to one job:

- ``approval-key``: signs and checks decision records in the host's ledger.
  A new key made on the host, never a copy of the Mac's Keychain key.
- ``routine-token``: the factory routine's start key (fire only).
- ``github-token``: read-only access to the pilot repo, for reconcile and the
  base-commit check. Never the bot's write access, never Rolando's login.
- ``linear-key``: ENG-174's and ENG-178's Linear access.
- ``reviewer-token``: the reviewer routine's start key (fire only), on the
  reviewer's own claude.ai account (ENG-156).

None of them is ever passed to the worker: the worker only receives the fire
text (contract and markers), and runs in the cloud with no route to the host.

``EnvSecrets`` reads them from environment variables (how Fly.io delivers
``fly secrets``) once at start-up and then deletes them from the environment,
so no child process inherits them. ``FileSecrets`` reads one file per secret
(systemd ``LoadCredential``, or any folder) and refuses a file anyone but the
service's own user could read.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Protocol, runtime_checkable

NAMES = ("approval-key", "routine-token", "github-token", "linear-key", "reviewer-token")
ENV_PREFIX = "FACTORY_"
_NAME_RE = re.compile(r"^[a-z]+(?:-[a-z]+)*$")


class SecretMissing(LookupError):
    """The secret is not there or not usable. The message never holds a value."""


@runtime_checkable
class SecretStore(Protocol):
    def get(self, name: str) -> str:
        """The secret's value; raises SecretMissing."""
        ...


def env_name(name: str) -> str:
    """``routine-token`` -> ``FACTORY_ROUTINE_TOKEN``."""
    _check_name(name)
    return ENV_PREFIX + name.upper().replace("-", "_")


def _check_name(name: str) -> None:
    if name not in NAMES or not _NAME_RE.fullmatch(name):
        raise SecretMissing(f"unknown secret name {name!r}")


class EnvSecrets:
    """Secrets taken out of the environment at start-up."""

    def __init__(
        self, environ: MutableMapping[str, str] | None = None, names: Iterable[str] = NAMES
    ) -> None:
        environ = os.environ if environ is None else environ
        self._values: dict[str, str] = {}
        for name in names:
            value = environ.pop(env_name(name), None)
            if value is not None and value.strip():
                self._values[name] = value.strip()

    def get(self, name: str) -> str:
        _check_name(name)
        try:
            return self._values[name]
        except KeyError:
            raise SecretMissing(f"secret {name} is not set ({env_name(name)})") from None

    def __repr__(self) -> str:
        return f"EnvSecrets(names={sorted(self._values)})"


class FileSecrets:
    """One file per secret in ``root``, each readable by this user only."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def get(self, name: str) -> str:
        _check_name(name)
        path = self.root / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise SecretMissing(f"secret file {path} can't be opened ({e.strerror})") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecretMissing(f"secret file {path} is not a regular file")
            if info.st_uid != os.geteuid():
                raise SecretMissing(f"secret file {path} belongs to another user")
            if info.st_mode & 0o077:
                raise SecretMissing(
                    f"secret file {path} can be read by others (mode {info.st_mode & 0o777:o});"
                    " it must be 0400 or 0600"
                )
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                fd = -1
                value = f.read().strip()
        finally:
            if fd >= 0:
                os.close(fd)
        if not value:
            raise SecretMissing(f"secret file {path} is empty")
        return value

    def __repr__(self) -> str:
        return f"FileSecrets(root={str(self.root)!r})"


def approval_key_bytes(store: SecretStore) -> bytes:
    """The approval key as 32 bytes, from 64 lowercase hex chars."""
    value = store.get("approval-key")
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SecretMissing("approval-key must be 64 lowercase hex characters")
    return bytes.fromhex(value)


__all__ = [
    "ENV_PREFIX",
    "NAMES",
    "EnvSecrets",
    "FileSecrets",
    "SecretMissing",
    "SecretStore",
    "approval_key_bytes",
    "env_name",
]
