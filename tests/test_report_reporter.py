"""LinearReporter and HttpTransport against an in-memory Linear (ENG-178)."""

from __future__ import annotations

import http.client
import json
import unittest
import urllib.error

from controller.report import messages as m
from controller.report.linear_api import API, HttpTransport, LinearDown, LinearRefused
from controller.report.replies import _QUESTION_RE, _REF_RE
from controller.report.reporter import (
    BODY_LIMIT,
    LinearReporter,
    clean_key,
    comment_id,
    render,
)
from controller.service.seams import Option, Question, ReportFailed
from tests.linear_world import (
    FACTORY,
    OTHER,
    ROLANDO,
    FakeLinear,
    Opener,
    http_error,
    issue_uuid,
)

ISSUE = "0b7e3c52-6a0f-4a1e-9d1c-3f5a2b7c9e10"
LIN_KEY = "lin_api_" + "Zq9" * 12
SHA = "a" * 40


def reporter(linear: FakeLinear, **kw: object) -> LinearReporter:
    return LinearReporter(linear, ROLANDO, **kw)  # type: ignore[arg-type]


def text(body: str = "The factory picked this ticket up.") -> str:
    return m.progress(m.Stage.QUEUED, body)


class CommentIdTests(unittest.TestCase):
    def test_same_issue_and_key_give_the_same_uuid4(self) -> None:
        a = comment_id(ISSUE, "intake:evt-1")
        self.assertEqual(a, comment_id(ISSUE, "intake:evt-1"))
        self.assertEqual(a[14], "4")
        self.assertNotEqual(a, comment_id("issue-2", "intake:evt-1"))
        self.assertNotEqual(a, comment_id(ISSUE, "intake:evt-2"))

    def test_clean_key_has_no_spaces_and_is_capped(self) -> None:
        spaced = clean_key("  a b\tc\n ")
        self.assertTrue(spaced.startswith("a_b_c~"), spaced)
        self.assertNotRegex(spaced, r"\s")
        self.assertLessEqual(len(clean_key("k" * 500)), 200)
        self.assertEqual(clean_key("intake:evt-1"), "intake:evt-1")  # clean keys stay as they are

    def test_long_keys_that_share_a_start_stay_different(self) -> None:
        """Rolando's report: truncation made two different keys one comment."""
        a, b = "k" * 300 + "a", "k" * 300 + "b"
        self.assertNotEqual(clean_key(a), clean_key(b))
        self.assertNotEqual(comment_id(ISSUE, clean_key(a)), comment_id(ISSUE, clean_key(b)))
        linear = FakeLinear()
        r = reporter(linear)
        r.post(ISSUE, a, text())
        r.post(ISSUE, b, text())
        self.assertEqual(len(linear.on(ISSUE)), 2)


class DuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.linear = FakeLinear()

    def test_post_shows_one_comment_by_the_factory(self) -> None:
        reporter(self.linear).post(ISSUE, "intake:evt-1", text())
        [c] = self.linear.on(ISSUE)
        self.assertEqual(c.user_id, FACTORY)
        self.assertEqual(c.id, comment_id(ISSUE, "intake:evt-1"))
        self.assertTrue(c.body.endswith("factory-ref: intake:evt-1"))

    def test_same_key_twice_and_after_a_restart_shows_one_comment(self) -> None:
        r = reporter(self.linear)
        r.post(ISSUE, "intake:evt-1", text())
        r.post(ISSUE, "intake:evt-1", text())
        reporter(self.linear).post(ISSUE, "intake:evt-1", text("A different text"))
        self.assertEqual(len(self.linear.on(ISSUE)), 1)
        self.assertEqual(self.linear.creates(), 1)

    def test_key_with_spaces_is_not_the_same_message_as_another_key(self) -> None:
        r = reporter(self.linear)
        r.post(ISSUE, "intake: evt-1", text())
        r.post(ISSUE, "intake:_evt-1", text())
        r.post(ISSUE, "intake: evt-1", text())  # the same key again: still one
        self.assertEqual(len(self.linear.on(ISSUE)), 2)

    def test_lost_answer_then_retry_shows_one_comment(self) -> None:
        self.linear.lose_next_create = 1
        with self.assertRaises(ReportFailed) as cm:
            reporter(self.linear).post(ISSUE, "k1", text())
        self.assertTrue(cm.exception.hold_all)
        self.assertEqual(len(self.linear.on(ISSUE)), 1)
        reporter(self.linear).post(ISSUE, "k1", text())  # a restarted process retries
        self.assertEqual(len(self.linear.on(ISSUE)), 1)
        self.assertEqual(self.linear.creates(), 1)

    def test_conflict_on_create_where_the_comment_exists_counts_as_posted(self) -> None:
        reporter(self.linear).post(ISSUE, "k1", text())
        # The lookup misses it once (as a lagging read might); the create
        # then conflicts, and the second lookup finds the comment.
        self.linear.hide_next_lookup = 1
        reporter(self.linear).post(ISSUE, "k1", text())
        self.assertEqual(len(self.linear.on(ISSUE)), 1)
        self.assertEqual(self.linear.creates(), 2)

    def test_missing_comment_answered_as_null_is_created(self) -> None:
        self.linear.missing_as_null = True
        reporter(self.linear).post(ISSUE, "k1", text())
        self.assertEqual(len(self.linear.on(ISSUE)), 1)

    def test_unconfirmed_create_that_did_land_counts_as_posted(self) -> None:
        self.linear.unconfirmed_create = 1
        reporter(self.linear).post(ISSUE, "k1", text())
        self.assertEqual(len(self.linear.on(ISSUE)), 1)

    def test_refused_create_is_a_plain_failure(self) -> None:
        self.linear.refuse_create[ISSUE] = LinearRefused("Argument Validation Error", "INVALID")
        with self.assertRaises(ReportFailed) as cm:
            reporter(self.linear).post(ISSUE, "k1", text())
        self.assertFalse(cm.exception.hold_all)
        self.assertEqual(self.linear.on(ISSUE), [])


class IssueNameTests(unittest.TestCase):
    def test_key_and_uuid_name_the_same_comment(self) -> None:
        linear = FakeLinear()
        uid = issue_uuid("ENG-1")
        reporter(linear).post("ENG-1", "k1", text())
        reporter(linear).post(uid, "k1", text())
        [c] = linear.on("ENG-1")
        self.assertEqual(c.id, comment_id(uid, "k1"))
        self.assertEqual(linear.creates(), 1)

    def test_issue_key_is_resolved_once(self) -> None:
        linear = FakeLinear()
        r = reporter(linear)
        r.post("ENG-1", "k1", text())
        r.post("ENG-1", "k2", text())
        lookups = [q for q, _ in linear.calls if "issue(id" in q and "comments" not in q]
        self.assertEqual(len(lookups), 1)

    def test_unknown_issue_is_a_plain_failure_and_posts_nothing(self) -> None:
        linear = FakeLinear(missing_issues={"ENG-404"})
        with self.assertRaises(ReportFailed) as cm:
            reporter(linear).post("ENG-404", "k1", text())
        self.assertFalse(cm.exception.hold_all)
        self.assertEqual(linear.creates(), 0)

    def test_lookup_refused_for_another_reason_does_not_create(self) -> None:
        linear = FakeLinear(refuse_lookup=LinearRefused("Forbidden", "FORBIDDEN"))
        with self.assertRaises(ReportFailed) as cm:
            reporter(linear).post(ISSUE, "k1", text())
        self.assertFalse(cm.exception.hold_all)
        self.assertEqual(linear.creates(), 0)


class ForeignCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.linear = FakeLinear()

    def test_existing_id_on_another_issue_is_refused(self) -> None:
        self.linear.add("ENG-999", "x", FACTORY, cid=comment_id(ISSUE, "k1"))
        with self.assertRaises(ReportFailed) as cm:
            reporter(self.linear).post(ISSUE, "k1", text())
        self.assertFalse(cm.exception.hold_all)
        self.assertIn("not the factory's own", str(cm.exception))
        self.assertEqual(self.linear.on(ISSUE), [])
        self.assertEqual(self.linear.creates(), 0)

    def test_existing_id_by_another_user_is_refused(self) -> None:
        for user in (ROLANDO, OTHER, None):
            with self.subTest(user=user):
                linear = FakeLinear()
                linear.add(ISSUE, "x", user, cid=comment_id(ISSUE, "k1"))
                with self.assertRaises(ReportFailed):
                    reporter(linear).post(ISSUE, "k1", text())
                self.assertEqual(len(linear.on(ISSUE)), 1)
                self.assertEqual(linear.creates(), 0)


class OutageTests(unittest.TestCase):
    def post_via_http(self, answer: object) -> ReportFailed:
        viewer = {"data": {"viewer": {"id": FACTORY}}}
        transport = HttpTransport(lambda: LIN_KEY, Opener(answer, first=[viewer]))
        with self.assertRaises(ReportFailed) as cm:
            LinearReporter(transport, ROLANDO).post(ISSUE, "k1", text())
        return cm.exception

    def test_rate_limits_and_outages_hold_all(self) -> None:
        cases = {
            "429": http_error(429),
            "400 RATELIMITED": http_error(
                400, {"errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]}
            ),
            "500": http_error(500),
            "502 with body": http_error(502, {"errors": [{"message": "bad gateway"}]}),
            "503": http_error(503),
            "401": http_error(401),
            "403": http_error(403),
            "timeout": TimeoutError("timed out"),
            "TimeoutError": TimeoutError(),
            "URLError": urllib.error.URLError("no route"),
            "connection reset": ConnectionResetError(),
            "incomplete read": http.client.IncompleteRead(b"x"),
            "not json": b"<html>oops</html>",
            "no data": {"something": 1},
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.assertTrue(self.post_via_http(answer).hold_all)

    def test_graphql_ratelimited_in_a_200_holds_all(self) -> None:
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        self.assertTrue(self.post_via_http(body).hold_all)

    def test_bad_request_refusal_does_not_hold_all(self) -> None:
        for answer in (
            http_error(400, {"errors": [{"message": "bad input"}]}),
            http_error(404),
            http_error(409, {"errors": [{"message": "conflict"}]}),
        ):
            with self.subTest(answer=answer.code):
                self.assertFalse(self.post_via_http(answer).hold_all)

    def test_fake_linear_down_holds_all_and_creates_nothing(self) -> None:
        linear = FakeLinear(down=LinearDown("x", "RATELIMITED"))
        with self.assertRaises(ReportFailed) as cm:
            reporter(linear).post(ISSUE, "k1", text())
        self.assertTrue(cm.exception.hold_all)
        self.assertEqual(linear.comments, {})

    def test_down_during_the_existence_check_holds_all(self) -> None:
        linear = FakeLinear()
        r = reporter(linear)
        r.viewer()
        linear.fail_next = [LinearDown("gone")]
        with self.assertRaises(ReportFailed) as cm:
            r.post(ISSUE, "k1", text())
        self.assertTrue(cm.exception.hold_all)
        self.assertEqual(linear.creates(), 0)


class IdentityTests(unittest.TestCase):
    def test_key_acting_as_rolando_never_posts(self) -> None:
        linear = FakeLinear(viewer_id=ROLANDO)
        r = reporter(linear)
        for key in ("k1", "k2", "question:x:q"):
            with self.assertRaises(ReportFailed) as cm:
                r.post(ISSUE, key, text())
            self.assertTrue(cm.exception.hold_all)
            self.assertIn("acts as Rolando", str(cm.exception))
        self.assertEqual(linear.comments, {})
        self.assertEqual(linear.creates(), 0)

    def test_factory_user_mismatch_never_posts(self) -> None:
        linear = FakeLinear(viewer_id=OTHER)
        with self.assertRaises(ReportFailed) as cm:
            reporter(linear, factory_user_id=FACTORY).post(ISSUE, "k1", text())
        self.assertTrue(cm.exception.hold_all)
        self.assertEqual(linear.creates(), 0)

    def test_factory_user_match_posts(self) -> None:
        linear = FakeLinear()
        reporter(linear, factory_user_id=FACTORY).post(ISSUE, "k1", text())
        self.assertEqual(len(linear.comments), 1)

    def test_no_viewer_id_holds_all(self) -> None:
        linear = FakeLinear(viewer_id="")
        with self.assertRaises(ReportFailed) as cm:
            reporter(linear).post(ISSUE, "k1", text())
        self.assertTrue(cm.exception.hold_all)

    def test_viewer_is_asked_once(self) -> None:
        linear = FakeLinear()
        r = reporter(linear)
        r.post(ISSUE, "k1", text())
        r.post(ISSUE, "k2", text())
        self.assertEqual(sum(1 for q, _ in linear.calls if "viewer" in q), 1)


class SecretTests(unittest.TestCase):
    SECRETS = (
        "ghp_" + "A1b2C3d4E5" * 3,
        LIN_KEY,
        "sk-ant-api03-" + "x" * 30,
        "github_pat_" + "B2" * 15,
    )

    def test_secrets_in_text_are_redacted_in_the_posted_body(self) -> None:
        linear = FakeLinear()
        raw = "\n".join(
            [
                "**Factory: Notice**",
                f"tokens {' '.join(self.SECRETS)}",
                "password=hunter2",
                "Authorization: Bearer abcdef0123456789",
                "curl -H 'x' Bearer zzzzzzzzzzzzzzzz",
            ]
        )
        reporter(linear).post(ISSUE, "k1", raw)
        [c] = linear.on(ISSUE)
        for s in (*self.SECRETS, "hunter2", "abcdef0123456789", "zzzzzzzzzzzzzzzz"):
            self.assertNotIn(s, c.body)
        self.assertIn("[REDACTED]", c.body)
        self.assertIn("password=", c.body)

    def test_key_never_in_transport_errors(self) -> None:
        key = "oauth-" + "Q" * 30
        echoes = [
            http_error(400, {"errors": [{"message": f"bad key {LIN_KEY}"}]}),
            http_error(401, {"errors": [{"message": f"Authorization: {LIN_KEY}"}]}),
            http_error(500, {"errors": [{"message": f"boom {key}"}]}),
            {"errors": [{"message": f"refused {key} {LIN_KEY}"}]},
            urllib.error.URLError(f"cannot reach with {LIN_KEY}"),
            OSError(f"socket for {key}"),
        ]
        for answer in echoes:
            for k in (LIN_KEY, key):
                with self.subTest(answer=repr(answer)[:40], key=k[:8]):
                    t = HttpTransport(lambda k=k: k, Opener(answer))
                    with self.assertRaises((LinearDown, LinearRefused)) as cm:
                        t("query { viewer { id } }", {})
                    self.assertNotIn(k, str(cm.exception))
                    self.assertNotIn(LIN_KEY, str(cm.exception))
                    self.assertLessEqual(len(str(cm.exception)), 260)


class HttpTransportTests(unittest.TestCase):
    def test_posts_to_linear_with_the_key(self) -> None:
        opener = Opener({"data": {"viewer": {"id": "u"}}})
        t = HttpTransport(lambda: LIN_KEY, opener)
        self.assertEqual(t("query { viewer { id } }", {"a": 1}), {"viewer": {"id": "u"}})
        [req] = opener.requests
        self.assertEqual(req.full_url, API)  # type: ignore[attr-defined]
        self.assertEqual(req.get_method(), "POST")  # type: ignore[attr-defined]
        self.assertEqual(req.get_header("Authorization"), LIN_KEY)  # type: ignore[attr-defined]
        self.assertEqual(opener.timeouts, [60])

    def test_oauth_token_gets_bearer(self) -> None:
        opener = Opener({"data": {}})
        HttpTransport(lambda: "tok123", opener)("q", {})
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer tok123")  # type: ignore[attr-defined]

    def test_refusal_carries_the_code(self) -> None:
        t = HttpTransport(
            lambda: LIN_KEY,
            Opener({"errors": [{"message": "nope", "extensions": {"code": "invalid_input"}}]}),
        )
        with self.assertRaises(LinearRefused) as cm:
            t("q", {})
        self.assertEqual(cm.exception.code, "INVALID_INPUT")

    def test_redirect_is_not_followed(self) -> None:
        from controller.report.linear_api import _NoRedirect

        self.assertIsNone(_NoRedirect().redirect_request())
        t = HttpTransport(lambda: LIN_KEY, Opener(http_error(302)))
        with self.assertRaises(LinearRefused):
            t("q", {})


class RenderTests(unittest.TestCase):
    def question(self, **kw: object) -> Question:
        base: dict[str, object] = dict(
            text="Sort by title or by date?",
            options=(Option("A", "Title", "A to Z."), Option("B", "Date", "Newest first.")),
            recommended="B",
            key="q-sort",
        )
        base.update(kw)
        return Question(**base)  # type: ignore[arg-type]

    def last_two(self, body: str) -> list[str]:
        return [x.strip() for x in body.split("\n") if x.strip()][-2:]

    def test_question_keeps_its_marker_and_ref(self) -> None:
        body = render(m.question(self.question()), "question:issue-1:q-sort")
        mark, ref = self.last_two(body)
        self.assertRegex(mark, _QUESTION_RE)
        self.assertEqual(mark, "factory-question: q-sort options=A,B")
        self.assertEqual(ref, "factory-ref: question:issue-1:q-sort")
        self.assertRegex(ref, _REF_RE)

    def test_very_long_question_is_cut_but_markers_survive(self) -> None:
        q = self.question(text="x" * 50_000, context="y" * 50_000)
        long_text = m.question(q).replace("**Factory", "z" * 30_000 + "\n**Factory", 1)
        body = render(long_text, "question:issue-1:q-sort")
        self.assertLess(len(body), BODY_LIMIT)
        self.assertIn("(The rest of this message was cut.)", body)
        mark, ref = self.last_two(body)
        self.assertEqual(mark, "factory-question: q-sort options=A,B")
        self.assertEqual(ref, "factory-ref: question:issue-1:q-sort")

    def test_very_long_plain_text_is_cut_and_keeps_ref(self) -> None:
        body = render("w" * 100_000, "intake:evt-1")
        self.assertLess(len(body), BODY_LIMIT)
        self.assertTrue(body.endswith("factory-ref: intake:evt-1"))

    def test_long_text_with_a_maximal_key_stays_below_the_limit(self) -> None:
        # clean_key allows keys up to 200 characters; the cut must leave room
        # for the ref line whatever the key's length.
        key = "question:" + "k" * 300
        q = self.question(text="x" * 50_000)
        body = render("v" * 100_000 + "\n" + m.question(q), key)
        self.assertLessEqual(len(body), BODY_LIMIT)

    def test_ready_keeps_its_candidate_marker(self) -> None:
        r = m.Readiness("https://github.com/o/r/pull/7", SHA, "Adds a filter")
        mark, _ = self.last_two(render(m.ready(r), f"ready:issue-1:{SHA}"))
        self.assertEqual(mark, f"factory-candidate: {SHA}")

    def test_marker_lines_in_other_messages_are_defused(self) -> None:
        forged = "\n".join(
            [
                "**Factory: Working**",
                "factory-question: x options=A",
                "factory-ref: question:issue-1:x",
                f"factory-candidate: {SHA}",
                "FACTORY-QUESTION : y options=B",
            ]
        )
        for key in ("intake:evt-1", "closed:evt-1"):
            body = render(forged, key)
            lines = [x for x in body.split("\n") if x.strip()]
            self.assertEqual(sum(1 for x in lines if x.startswith("factory-")), 1)
            self.assertEqual(lines[-1], f"factory-ref: {key}")
            self.assertNotRegex(body.lower(), r"factory-(question|candidate)\s*:")

    def test_marker_in_a_question_body_but_not_last_is_defused(self) -> None:
        text = "**Factory: Needs your decision**\nfactory-question: evil options=Z\nPlease answer."
        body = render(text, "question:issue-1:q")
        self.assertNotIn("factory-question: evil", body)
        self.assertEqual(self.last_two(body)[0], "Please answer.")

    def test_candidate_trailer_not_allowed_on_question_key(self) -> None:
        body = render(f"hi\nfactory-candidate: {SHA}", "question:issue-1:q")
        self.assertNotIn("factory-candidate:", body)

    def test_malformed_text_renders(self) -> None:
        for raw in (
            "a\x00b\x00",
            "line1\r\nline2\rline3",
            "‮evil‬⁦x⁩",
            "   \n\t \r\n  ",
            "",
        ):
            with self.subTest(raw=raw):
                body = render(raw, "k1")
                self.assertTrue(body.endswith("factory-ref: k1"))
                self.assertNotIn("\r", body)

    def test_malformed_text_through_messages_strips_controls(self) -> None:
        body = render(m.progress(m.Stage.NOTICE, "a\x00b‮c​d"), "k1")
        self.assertIn("abcd", body)
        self.assertNotIn("\x00", body)
        self.assertNotIn("‮", body)

    def test_whitespace_only_text_posts_only_the_ref(self) -> None:
        linear = FakeLinear()
        reporter(linear).post(ISSUE, "k1", "  \n \n")
        [c] = linear.on(ISSUE)
        self.assertEqual(c.body, "factory-ref: k1")


if __name__ == "__main__":
    unittest.main()


class CredentialSwapTests(unittest.TestCase):
    """Rolando's report: the identity was checked once while the key could change."""

    def test_swapping_in_rolandos_key_stops_posting_with_it(self) -> None:
        keys = {"current": "lin_api_" + "f" * 30}
        users = {"lin_api_" + "f" * 30: FACTORY, "lin_api_" + "r" * 30: ROLANDO}
        sent: list[str] = []

        def opener(req, timeout=None):  # type: ignore[no-untyped-def]
            key = req.get_header("Authorization")
            body = json.loads(req.data)
            sent.append(key)
            if "viewer" in body["query"]:
                answer = {"data": {"viewer": {"id": users[key]}}}
            elif "commentCreate" in body["query"]:
                answer = {"data": {"commentCreate": {"success": True, "comment": {"id": "x"}}}}
            elif "issue(" in body["query"]:
                answer = {"data": {"issue": {"id": ISSUE}}}
            else:
                answer = {"errors": [{"message": "Entity not found"}]}
            from tests.linear_world import Resp

            return Resp(json.dumps(answer).encode())

        transport = HttpTransport(lambda: keys["current"], opener, forbidden_user=ROLANDO)
        r = LinearReporter(transport, ROLANDO)
        r.post(ISSUE, "k1", text())
        keys["current"] = "lin_api_" + "r" * 30
        sent.clear()
        with self.assertRaises(ReportFailed) as cm:
            r.post(ISSUE, "k2", text())
        self.assertTrue(cm.exception.hold_all)
        self.assertIn("acts as Rolando", str(cm.exception))
        # Only the identity question went out with his key; nothing was posted.
        self.assertEqual(len(sent), 1)
        with self.assertRaises(LinearDown):
            transport("mutation { commentCreate }", {})


class ForbiddenChangesTests(unittest.TestCase):
    """Rolando's report on #32: the approver could change while a key's check stayed cached."""

    def test_a_newly_forbidden_user_stops_a_key_already_checked(self) -> None:
        forbidden = {ROLANDO}
        sent: list[str] = []

        def opener(req, timeout=None):  # type: ignore[no-untyped-def]
            from tests.linear_world import Resp

            query = json.loads(req.data)["query"]
            sent.append(query)
            if "viewer" in query:
                return Resp(json.dumps({"data": {"viewer": {"id": FACTORY}}}).encode())
            return Resp(json.dumps({"data": {"ok": True}}).encode())

        transport = HttpTransport(
            lambda: "lin_api_" + "f" * 30, opener, forbidden_user=lambda: forbidden
        )
        transport("query { ok }", {})
        forbidden.add(FACTORY)
        sent.clear()
        with self.assertRaises(LinearDown):
            transport("mutation { commentCreate }", {})
        self.assertEqual(sent, [])  # the cached identity is reused, but nothing is sent


class ProbeTests(unittest.TestCase):
    """Rolando's report: the probe passed even when the marker was gone."""

    def test_probe_needs_the_question_marker_back(self) -> None:
        from controller.report.__main__ import marker_survived
        from controller.report.messages import QUESTION_MARK
        from controller.report.replies import parse_comment
        from controller.report.reporter import QUESTION_PREFIX, render

        key = QUESTION_PREFIX + "probe-1"
        issue = issue_uuid(ISSUE)
        cid = comment_id(issue, key)

        def stored(body: str):  # type: ignore[no-untyped-def]
            return parse_comment(
                {
                    "id": cid,
                    "body": body,
                    "createdAt": "2026-10-08T12:00:00Z",
                    "user": {"id": FACTORY},
                }
            )

        good = render(f"x\n\n{QUESTION_MARK} probe options=-", key)
        self.assertTrue(marker_survived(stored(good), issue, FACTORY))
        lost = render("x", key)  # Linear dropped the marker line
        self.assertFalse(marker_survived(stored(lost), issue, FACTORY))
        other = render(f"x\n\n{QUESTION_MARK} not-probe options=-", key)
        self.assertFalse(marker_survived(stored(other), issue, FACTORY))
