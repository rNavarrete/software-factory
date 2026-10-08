"""The signer (``controller/signer``): the one process that holds the approval key.

A real ``SignerServer`` answers on a Unix socket in a temporary folder, from a
background thread, for the current user. Linux only (``SO_PEERCRED``).
"""

import base64
import functools
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

from controller.approval import Approvals, StaticKey
from controller.service.service import never
from controller.signer import SignerKey, SignerServer, SignerUnavailable, drop_privileges
from controller.signer import signer as signer_module
from tests.test_approval import example, yes
from tests.test_attempts import MemoryLedger

logging.getLogger("factory").addHandler(logging.NullHandler())

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
KEY = StaticKey(b"k" * 32)
OTHER = StaticKey(b"o" * 32)


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED is Linux only")
class SignerCase(unittest.TestCase):
    allowed = None
    """Uids the server answers; None means the current user."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "signer.sock"
        allowed = {os.getuid()} if self.allowed is None else self.allowed
        self.server = SignerServer(KEY, self.path, allowed_uids=allowed)
        self.server.listen()
        self.stop = threading.Event()
        self.crashed = []

        def serve():
            try:
                self.server.serve_forever(self.stop.is_set)
            except BaseException as e:  # recorded, so a test can say the signer died
                self.crashed.append(e)

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.stop.set()
        if self.thread.is_alive():
            try:  # wake the accept loop so it sees the stop at once
                signer_module.call(self.path, {"op": "key-id"}, timeout=1)
            except SignerUnavailable:
                pass
        self.thread.join(5)
        self.server.close()

    def key(self, timeout=5):
        return SignerKey(self.path, functools.partial(signer_module.call, timeout=timeout))

    def raw(self, data):
        """Send raw bytes as one request; return the signer's answer."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(self.path))
            s.sendall(data)
            return dict(signer_module._receive(s))


class SignerTests(SignerCase):
    # --- what the service's key can and can't do ---

    def test_key_id_is_the_real_keys(self):
        self.assertEqual(self.key().key_id, KEY.key_id)

    def test_verify_says_yes_only_for_the_keys_own_signature(self):
        k = self.key()
        payload = b"approve contract abc"
        self.assertTrue(k.verify(payload, KEY.sign(payload)))
        self.assertFalse(k.verify(payload, OTHER.sign(payload)))
        self.assertFalse(k.verify(payload + b"x", KEY.sign(payload)))
        self.assertFalse(k.verify(payload, "not hex"))
        self.assertFalse(k.verify(b"", ""))

    def test_the_service_key_can_never_sign(self):
        with self.assertRaises(PermissionError):
            self.key().sign(b"anything")
        self.assertNotIn("k" * 32, repr(self.key()))

    def test_verify_answers_are_kept(self):
        calls = []

        def counting(path, req):
            calls.append(req["op"])
            return signer_module.call(path, req, timeout=5)

        k = SignerKey(self.path, counting)
        mac = KEY.sign(b"p")
        self.assertTrue(k.verify(b"p", mac))
        self.assertTrue(k.verify(b"p", mac))
        self.assertFalse(k.verify(b"p", OTHER.sign(b"p")))
        self.assertEqual(calls, ["verify", "verify"])

    # --- the approvals the service checks ---

    def test_service_checks_rolandos_approval_through_the_signer(self):
        store = MemoryLedger()
        Approvals(store, KEY, confirm=yes, os_user="rolando").approve(example(), NOW)
        service = Approvals(store, self.key(), confirm=never, os_user="factory")
        self.assertTrue(service.check(example(), NOW).approved)
        # An approval signed with another key does not count.
        other = MemoryLedger()
        Approvals(other, OTHER, confirm=yes, os_user="mallory").approve(example(), NOW)
        self.assertFalse(Approvals(other, self.key(), confirm=never).check(example(), NOW).approved)

    def test_service_cannot_approve_and_writes_nothing(self):
        store = MemoryLedger()
        desk = Approvals(store, self.key(), confirm=yes, os_user="factory")
        with self.assertRaises(PermissionError):
            desk.approve(example(), NOW)
        self.assertEqual(store.events(), [])
        self.assertFalse(desk.check(example(), NOW).approved)

    # --- bad requests ---

    def test_malformed_requests_get_an_error_and_the_signer_keeps_serving(self):
        cases = {
            "bad base64": json.dumps({"op": "verify", "payload": "!!!", "mac": "00"}),
            "payload not text": json.dumps({"op": "verify", "payload": 1, "mac": "00"}),
            "no mac": json.dumps({"op": "verify", "payload": "eA=="}),
            "not an object": "[1, 2, 3]",
            "not json": "{nope",
            "unknown op": json.dumps({"op": "sign", "payload": "eA=="}),
            "no op": "{}",
        }
        for name, body in cases.items():
            with self.subTest(name):
                answer = self.raw(body.encode() + b"\n")
                self.assertIn("error", answer)
                self.assertNotIn("ok", answer)
                self.assertNotIn("mac", answer)
                self.assertEqual(self.key().key_id, KEY.key_id)
        self.assertEqual(self.crashed, [])

    def test_no_request_signs_anything(self):
        payload = base64.b64encode(b"p").decode()
        for op in ("sign", "key", "mac", "hmac"):
            with self.subTest(op):
                answer = self.raw(json.dumps({"op": op, "payload": payload}).encode() + b"\n")
                self.assertEqual(set(answer), {"error"})

    def test_oversized_request_is_refused(self):
        big = b'{"op": "key-id", "pad": "' + b"x" * (signer_module.MAX_REQUEST + 10) + b'"}\n'
        answer = self.raw(big)
        self.assertIn("error", answer)
        self.assertEqual(self.key().key_id, KEY.key_id)

    def test_caller_hanging_up_does_not_stop_the_signer(self):
        # Was a bug (fixed): SignerServer.serve_one (controller/signer/signer.py:131-141) sends
        # the answer outside its try/except, and serve_forever only catches
        # TimeoutError. A caller that hangs up before reading the answer (here: it
        # connects and closes; the service does the same when its own call times
        # out) makes _send raise BrokenPipeError, which ends serve_forever and so
        # the signer process. Expected: the next request is still answered
        # ("one bad request never stops the signer"). Actual: nothing answers.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(self.path))
        self.thread.join(1)
        self.assertEqual(self.crashed, [])
        self.assertEqual(self.key(timeout=2).key_id, KEY.key_id)

    # --- reaching the signer ---

    def test_unreachable_socket_is_unavailable(self):
        k = SignerKey(self.path.with_name("nobody.sock"))
        with self.assertRaises(SignerUnavailable):
            _ = k.key_id
        with self.assertRaises(SignerUnavailable):
            k.verify(b"p", KEY.sign(b"p"))

    def test_unreachable_signer_means_nothing_is_approved(self):
        store = MemoryLedger()
        Approvals(store, KEY, confirm=yes, os_user="rolando").approve(example(), NOW)
        service = Approvals(store, SignerKey(self.path.with_name("gone.sock")), confirm=never)
        try:
            approved = service.check(example(), NOW).approved
        except SignerUnavailable:
            approved = False
        self.assertFalse(approved)

    def test_socket_is_not_world_accessible(self):
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode & 0o007, 0)


class RefusedCallerTests(SignerCase):
    allowed = {os.getuid() + 4242}

    def test_caller_whose_uid_is_not_allowed_is_refused(self):
        with self.assertRaises(SignerUnavailable) as cm:
            _ = self.key().key_id
        self.assertIn("not allowed", str(cm.exception))
        with self.assertRaises(SignerUnavailable):
            self.key().verify(b"p", KEY.sign(b"p"))


class DropPrivilegesTests(unittest.TestCase):
    def test_unknown_user_changes_nothing(self):
        uid, gid = os.getuid(), os.getgid()
        with self.assertRaises(KeyError):
            drop_privileges("factory-signer-no-such-user-174")
        self.assertEqual((os.getuid(), os.getgid()), (uid, gid))

    @unittest.skipIf(os.getuid() == 0, "as root the call would really change users")
    def test_non_root_cannot_become_another_user(self):
        import pwd

        other = next((p.pw_name for p in pwd.getpwall() if p.pw_uid not in (os.getuid(), 0)), None)
        if other is None:
            self.skipTest("no other user on this machine")
        with self.assertRaises(PermissionError):
            drop_privileges(other)


if __name__ == "__main__":
    unittest.main()
