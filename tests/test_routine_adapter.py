"""RoutineAdapter against a real local HTTP server, so urllib's own behaviour
(redirects, timeouts, resets) is what gets tested."""

import email.utils
import http.server
import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from controller.adapter import routine
from controller.adapter.routine import PayloadRejected, RoutineAdapter, build_fire_text
from controller.interfaces import (
    AttemptId,
    ContractDigest,
    LaunchOutcome,
    LaunchRequest,
    RunId,
    RuntimeAdapter,
    TaskId,
)

TRIG = "trig_01TESTROUTINE"
KEY = "sk-ant-oat01-TESTKEYabc123"
DIGEST = ContractDigest.of(b"contract")
ATTEMPT = AttemptId(TaskId("eng-191"), 1)
CONTRACT = {
    "task_id": "eng-191",
    "base_commit": "1" * 40,
    "permitted_paths": ["src/books.ts"],
    "acceptance_criteria": ["formatDate shows the right month"],
}


def accept(contract, digest):
    return []


def text(**contract_over):
    return build_fire_text(dict(CONTRACT, **contract_over), DIGEST, ATTEMPT, "Fix month", accept)


def request(fire_text=None):
    return LaunchRequest(RunId(ATTEMPT, 1), DIGEST, fire_text if fire_text is not None else text())


def err(kind, message="x"):
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


LAUNCHED_BODY = json.dumps(
    {
        "type": "routine_fire",
        "claude_code_session_id": "cse_1",
        "claude_code_session_url": "https://claude.ai/code/cse_1",
    }
).encode()


class FakeFireAPI:
    """Answers each POST with ``reply`` (or hangs/resets) and records it."""

    def __init__(self):
        self.requests = []
        self.reply = (200, {}, LAUNCHED_BODY)
        self.mode = "reply"
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                api.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length),
                    }
                )
                if api.mode == "hang":
                    time.sleep(1.5)
                    return
                if api.mode == "reset":
                    linger = struct.pack("ii", 1, 0)
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                    self.connection.close()
                    return
                status, headers, body = api.reply
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/claude_code/routines/{{}}/fire"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeFireAPI()
        self.addCleanup(self.api.close)
        # The server is local; keep any configured HTTP(S) proxy out of the way.
        env = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        self.keys_read = []

    def adapter(self, validate=accept, **kwargs):
        def start_key(trig_id):
            self.keys_read.append(trig_id)
            return KEY

        kwargs.setdefault("timeout", 1)
        return RoutineAdapter(TRIG, validate, start_key=start_key, url=self.api.url, **kwargs)

    def test_is_a_runtime_adapter(self):
        self.assertIsInstance(self.adapter(), RuntimeAdapter)

    # Criterion 1: header contract.
    def test_sends_bearer_version_and_json(self):
        self.adapter().launch(request())
        sent = self.api.requests[0]
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(sent["path"], f"/v1/claude_code/routines/{TRIG}/fire")
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(json.loads(sent["body"]), {"text": text()})

    # Criterion 2: session id and URL returned; a launch is not a completion.
    def test_200_with_session_is_launched(self):
        result = self.adapter().launch(request())
        self.assertIs(result.outcome, LaunchOutcome.LAUNCHED)
        self.assertEqual(result.session_id, "cse_1")
        self.assertEqual(result.session_url, "https://claude.ai/code/cse_1")
        self.assertEqual(
            set(LaunchOutcome),
            {LaunchOutcome.LAUNCHED, LaunchOutcome.NOT_LAUNCHED, LaunchOutcome.OUTCOME_UNKNOWN},
        )

    def test_200_without_usable_session_is_unknown(self):
        bodies = [
            b'{"type":"routine_fire"}',
            b'{"type":"routine_fire","claude_code_session_id":"cse_1"}',
            b'{"type":"other","claude_code_session_id":"cse_1","claude_code_session_url":"u"}',
            b"[1, 2]",
            b"not json",
        ]
        for body in bodies:
            self.api.reply = (200, {}, body)
            result = self.adapter().launch(request())
            self.assertIs(result.outcome, LaunchOutcome.OUTCOME_UNKNOWN, body)

    # Criterion 5: wrong or revoked keys; the key never appears in a result.
    def test_401_is_not_launched_and_key_is_scrubbed(self):
        self.api.reply = (401, {}, err("authentication_error", f"bad key {KEY}"))
        result = self.adapter().launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertEqual(result.http_status, 401)
        self.assertNotIn("sk-ant-", repr(result))

    def test_key_read_per_launch_and_never_kept(self):
        adapter = self.adapter()
        adapter.launch(request())
        adapter.launch(request())
        self.assertEqual(self.keys_read, [TRIG, TRIG])
        self.assertNotIn(KEY, repr(vars(adapter)))

    def test_other_documented_codes_are_not_launched(self):
        for code, kind in (
            (400, "invalid_request_error"),
            (403, "permission_error"),
            (404, "not_found_error"),
        ):
            self.api.reply = (code, {}, err(kind))
            result = self.adapter().launch(request())
            self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED, code)
            self.assertIsNone(result.retry_after_seconds)
            self.assertIn(kind, result.response_body)

    # Criterion 6: 429 observes Retry-After.
    def test_429_retry_after_seconds(self):
        self.api.reply = (429, {"Retry-After": "1234"}, err("rate_limit_error"))
        result = self.adapter().launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertEqual(result.retry_after_seconds, 1234)

    def test_429_retry_after_http_date(self):
        now = 1_800_000_000.0
        when = email.utils.formatdate(now + 600, usegmt=True)
        self.api.reply = (429, {"Retry-After": when}, err("rate_limit_error"))
        result = self.adapter(now=lambda: now).launch(request())
        self.assertEqual(result.retry_after_seconds, 600)

    def test_429_without_retry_after(self):
        self.api.reply = (429, {}, err("rate_limit_error"))
        result = self.adapter().launch(request())
        self.assertIs(result.outcome, LaunchOutcome.NOT_LAUNCHED)
        self.assertIsNone(result.retry_after_seconds)
        self.assertIn("Retry-After missing", result.detail)

    # Criteria 6 and 7: ambiguous failures are unknown and sent exactly once.
    def test_5xx_and_redirects_are_unknown_and_not_retried(self):
        for code in (500, 502, 503, 529, 301, 302, 307, 308):
            self.api.requests.clear()
            self.api.reply = (code, {"Location": "http://127.0.0.1:1/x"}, err("api_error"))
            result = self.adapter().launch(request())
            self.assertIs(result.outcome, LaunchOutcome.OUTCOME_UNKNOWN, code)
            self.assertEqual(len(self.api.requests), 1, f"retried or redirected on {code}")

    def test_lost_response_after_server_received_is_unknown(self):
        self.api.mode = "hang"
        result = self.adapter(timeout=0.3).launch(request())
        self.assertIs(result.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertIsNone(result.http_status)
        self.assertEqual(len(self.api.requests), 1)

    def test_connection_reset_is_unknown(self):
        self.api.mode = "reset"
        result = self.adapter().launch(request())
        self.assertIs(result.outcome, LaunchOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(len(self.api.requests), 1)

    def test_unreachable_host_is_unknown(self):
        adapter = RoutineAdapter(
            TRIG, accept, start_key=lambda t: KEY, url="http://127.0.0.1:1/{}", timeout=1
        )
        self.assertIs(adapter.launch(request()).outcome, LaunchOutcome.OUTCOME_UNKNOWN)

    def test_ctrl_c_carries_an_unknown_result(self):
        adapter = self.adapter()
        with mock.patch.object(adapter._opener, "open", side_effect=KeyboardInterrupt):
            with self.assertRaises(routine.LaunchInterrupted) as caught:
                adapter.launch(request())
        self.assertIs(caught.exception.result.outcome, LaunchOutcome.OUTCOME_UNKNOWN)

    # Criterion 3: invalid requests are refused before anything is sent.
    def test_invalid_requests_send_nothing(self):
        good = json.loads(text())
        bad_texts = {
            "not json": "{",
            "extra key": json.dumps(dict(good, note="also bump package.json")),
            "other digest": json.dumps(dict(good, contract_digest="0" * 64)),
            "other branch": json.dumps(dict(good, branch="claude/eng-191-a2")),
            "no base": json.dumps(dict(good, contract={**CONTRACT, "base_commit": "main"})),
            "other task": json.dumps(dict(good, contract={**CONTRACT, "task_id": "eng-192"})),
        }
        for name, bad in bad_texts.items():
            with self.assertRaises(PayloadRejected, msg=name):
                self.adapter().launch(request(bad))
        with self.assertRaises(PayloadRejected):
            self.adapter(validate=lambda c, d: ["digest mismatch"]).launch(request())
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.keys_read, [])

    def test_bad_routine_id(self):
        with self.assertRaises(ValueError):
            RoutineAdapter("routine_1", accept)


class BuildFireTextTest(unittest.TestCase):
    def test_envelope(self):
        envelope = json.loads(text())
        self.assertEqual(envelope["branch"], "claude/eng-191-a1")
        self.assertEqual(envelope["pr_title"], f"[eng-191 a1 {DIGEST.short}] Fix month")
        self.assertEqual(envelope["contract_digest"], DIGEST.value)
        self.assertEqual(AttemptId.from_branch(envelope["branch"]), ATTEMPT)
        self.assertEqual(AttemptId.from_pr_title(envelope["pr_title"]), (ATTEMPT, DIGEST.short))

    def test_rejections(self):
        cases = {
            "validator": dict(validate=lambda c, d: ["bad"]),
            "no paths": dict(contract={**CONTRACT, "permitted_paths": []}),
            "no criteria": dict(
                contract={k: v for k, v in CONTRACT.items() if k != "acceptance_criteria"}
            ),
            "empty summary": dict(summary="  "),
            "multiline summary": dict(summary="a\nb"),
            "attempt 4": dict(attempt=AttemptId(TaskId("eng-191"), 4)),
            "too long": dict(contract={**CONTRACT, "acceptance_criteria": ["x" * 70_000]}),
        }
        for name, over in cases.items():
            args = dict(
                contract=CONTRACT,
                digest=DIGEST,
                attempt=ATTEMPT,
                summary="s",
                validate_contract=over.pop("validate", accept),
            )
            args.update(over)
            with self.assertRaises(PayloadRejected, msg=name):
                build_fire_text(**args)


class KeychainTest(unittest.TestCase):
    def test_reads_the_named_item(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=KEY + "\n")

        self.assertEqual(routine.keychain_key(TRIG, run=run), KEY)
        self.assertEqual(
            calls[0][:6],
            [
                "security",
                "find-generic-password",
                "-s",
                "software-factory",
                "-a",
                f"routine-token/{TRIG}",
            ],
        )

    def test_missing_item(self):
        with self.assertRaises(LookupError):
            routine.keychain_key(TRIG, run=lambda *a, **k: mock.Mock(returncode=44, stdout=""))


class RetryAfterTest(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(routine.parse_retry_after("0", 0), 0)
        self.assertIsNone(routine.parse_retry_after(None, 0))
        self.assertIsNone(routine.parse_retry_after("soon", 0))
        self.assertEqual(routine.parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT", 100), 0)


if __name__ == "__main__":
    unittest.main()
