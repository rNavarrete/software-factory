"""The signer: the one process on the service host that holds the approval key.

The approval key is HMAC: whoever can check a signature with it can also make
one. So on the host the key lives only in this small process, which runs as
its own Unix user (``factory-signer``) and reads the key file before anything
else. The service runs as another user (``factory``), cannot read the key
file, and cannot read this process's memory (different users, no ptrace).
It holds a ``SignerKey``, which can check a signature by asking the signer
and refuses to make one. Code in the service that reads Linear text, model
output or GitHub data therefore cannot forge Rolando's approval, a repair
go-ahead or a clearing, whatever it does.

The signer answers on a Unix socket in a folder only the two users can open,
one JSON request per connection:

- ``{"op": "key-id"}``: the key's public fingerprint.
- ``{"op": "verify", "payload": <base64>, "mac": <hex>}``: whether ``mac``
  is the key's signature of ``payload``. It says yes or no and nothing else,
  so asking it can't produce a signature.

The kernel reports the caller's user id (``SO_PEERCRED``); only the allowed
user ids are answered.

Rolando's own decisions over ``fly ssh console`` run as root, read the key
file directly, and are signed exactly as on the Mac. They never go through
this socket.

Standard library only. Linux (``SO_PEERCRED``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import pwd
import socket
import struct
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from controller.approval import StaticKey

log = logging.getLogger("factory.signer")

MAX_REQUEST = 1 << 20
_CACHE_LIMIT = 4096


class SignerUnavailable(OSError):
    """The signer did not answer. Nothing can be checked, so nothing fires."""


def drop_privileges(user: str) -> None:
    """Become ``user`` for good (no way back to root)."""
    entry = pwd.getpwnam(user)
    os.setgroups([])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    if os.getuid() != entry.pw_uid or os.geteuid() != entry.pw_uid:
        raise PermissionError(f"could not become {user}")
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise PermissionError(f"dropped to {user} but could still become root")


Handler = Callable[[Mapping[str, object], int], Mapping[str, object]]


class SignerServer:
    """Answers requests on ``path``. ``allowed_uids`` may call it."""

    def __init__(
        self,
        key: StaticKey,
        path: Path,
        *,
        allowed_uids: Collection[int],
        extra: Mapping[str, Handler] | None = None,
    ) -> None:
        self._key = key
        self.path = Path(path)
        self._allowed = frozenset(allowed_uids)
        self._handlers: dict[str, Handler] = {
            "key-id": lambda req, uid: {"key_id": self._key.key_id},
            "verify": self._verify,
        }
        self._handlers.update(extra or {})
        self._sock: socket.socket | None = None

    def _verify(self, req: Mapping[str, object], uid: int) -> Mapping[str, object]:
        payload, mac = req.get("payload"), req.get("mac")
        if not isinstance(payload, str) or not isinstance(mac, str):
            return {"error": "payload and mac must be text"}
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return {"error": "payload must be base64"}
        return {"ok": self._key.verify(raw, mac)}

    def listen(self) -> None:
        self.path.unlink(missing_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        os.chmod(self.path, 0o660)
        s.listen(16)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.path.unlink(missing_ok=True)

    def serve_one(self) -> None:
        assert self._sock is not None, "listen() first"
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(30)
            uid = _peer_uid(conn)
            if uid not in self._allowed:
                log.warning("refused a caller with uid %s", uid)
                try:
                    _send(conn, {"error": "not allowed"})
                except OSError:
                    pass
                return
            try:
                req = _receive(conn)
                handler = self._handlers.get(str(req.get("op")))
                answer = (
                    {"error": f"unknown op {req.get('op')!r}"}
                    if handler is None
                    else handler(req, uid)
                )
            except Exception as e:  # one bad request never stops the signer
                log.warning("request failed: %s", type(e).__name__)
                answer = {"error": f"request failed ({type(e).__name__})"}
            try:
                _send(conn, answer)
            except OSError:  # the caller hung up; nothing to tell it
                log.warning("a caller left before its answer")

    def serve_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        assert self._sock is not None, "listen() first"
        self._sock.settimeout(1.0)
        while not stop():
            try:
                self.serve_one()
            except TimeoutError:
                continue
            except OSError as e:  # one broken connection never stops the signer
                log.warning("connection failed: %s", type(e).__name__)


def _peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", creds)
    return uid


def _receive(conn: socket.socket) -> Mapping[str, object]:
    chunks, size = [], 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_REQUEST:
            raise ValueError("request too large")
        if chunk.endswith(b"\n"):
            break
    req = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(req, dict):
        raise ValueError("request must be an object")
    return req


def _send(conn: socket.socket, answer: Mapping[str, object]) -> None:
    conn.sendall(json.dumps(answer, separators=(",", ":")).encode() + b"\n")


def call(path: Path, request: Mapping[str, object], timeout: float = 30) -> Mapping[str, object]:
    """One request to the signer."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            answer = _receive(s)
    except (OSError, ValueError) as e:
        raise SignerUnavailable(f"the signer did not answer ({type(e).__name__})") from None
    if "error" in answer:
        raise SignerUnavailable(f"the signer refused: {answer['error']}")
    return answer


class SignerKey:
    """The service's view of the approval key: it can check, never sign."""

    def __init__(self, path: Path | str, transport: Callable[..., Mapping[str, object]] = call):
        self._path = Path(path)
        self._call = transport
        self._key_id: str | None = None
        self._cache: dict[str, bool] = {}

    @property
    def key_id(self) -> str:
        if self._key_id is None:
            value = self._call(self._path, {"op": "key-id"}).get("key_id")
            if not isinstance(value, str) or not value:
                raise SignerUnavailable("the signer gave no key id")
            self._key_id = value
        return self._key_id

    def sign(self, payload: bytes) -> str:
        raise PermissionError("the service can't sign: only Rolando and the signer can")

    def verify(self, payload: bytes, mac: str) -> bool:
        # Records never change once written, so an answer can be kept.
        cache_key = hashlib.sha256(payload + b"\0" + mac.encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key]
        req = {"op": "verify", "payload": base64.b64encode(payload).decode(), "mac": mac}
        ok = self._call(self._path, req).get("ok") is True
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[cache_key] = ok
        return ok

    def __repr__(self) -> str:
        return f"SignerKey(path={str(self._path)!r})"


__all__ = [
    "SignerKey",
    "SignerServer",
    "SignerUnavailable",
    "call",
    "drop_privileges",
]
