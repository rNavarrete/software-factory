"""Capture real fixture bytes; never infer source understanding from a download."""

import base64
import hashlib
import importlib
import io
import json
import stat
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

from controller.prepare.preparer import LinearTicketReader
from controller.service.seams import Prepared, Question
from tests.test_prepare import authorize, prep, project, ticket

FIX = Path(__file__).parent / "fixtures/context"
PAGE = "11111111-1111-4111-8111-111111111199"
NOW = datetime(2026, 10, 8, 21, tzinfo=UTC)


class CaptureTests(unittest.TestCase):
    def module(self):
        self.assertTrue(
            (FIX.parents[2] / "controller/prepare/context.py").exists(),
            "source capture has not been implemented",
        )
        return importlib.import_module("controller.prepare.context")

    def test_snapshot_retains_exact_bytes_provenance_and_private_files(self):
        m = self.module()
        refs = [
            m.Source("page", "notion", PAGE, "https://www.notion.so/" + PAGE),
            m.Source("design", "image", "design-1", "https://assets.example.test/design.png"),
        ]
        raw = {
            "page": (FIX / "notion-ready.json").read_bytes(),
            "design": (FIX / "design.png").read_bytes(),
        }

        def read(ref):
            return m.Content(
                raw[ref.key],
                "application/json" if ref.kind == "notion" else "image/png",
                revision="revision-1",
            )

        snapshot = m.capture(refs, read, NOW)
        self.assertEqual(snapshot.problems, ())
        with tempfile.TemporaryDirectory() as tmp:
            path = snapshot.save(Path(tmp))
            saved = json.loads(path.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(len(saved["sources"]), 2)
            for source in saved["sources"]:
                self.assertEqual(base64.b64decode(source["base64"]), raw[source["key"]])
                self.assertEqual(source["sha256"], hashlib.sha256(raw[source["key"]]).hexdigest())
                self.assertEqual(source["revision"], "revision-1")
                self.assertEqual(source["retrieved_at"], NOW.isoformat())
            self.assertEqual(saved["sources"][1]["inspection"], "not_run")
            self.assertFalse(saved["ready_for_dispatch"])

    def test_required_and_optional_missing_are_distinct(self):
        m = self.module()

        def missing(ref):
            raise m.SourceUnavailable("permission-denied")

        refs = [
            m.Source("required", "notion", PAGE, "https://www.notion.so/" + PAGE),
            m.Source("optional", "notion", PAGE, "https://www.notion.so/" + PAGE, required=False),
        ]
        s = m.capture(refs, missing, NOW)
        self.assertEqual(len(s.problems), 1)
        self.assertEqual(len(s.limitations), 1)
        self.assertIn("required", s.problems[0])
        self.assertIn("optional", s.limitations[0])

    def test_incomplete_wrong_page_or_malformed_notion_never_count_as_complete(self):
        m = self.module()
        ref = m.Source("page", "notion", PAGE, "https://www.notion.so/" + PAGE)
        good = json.loads((FIX / "notion-ready.json").read_bytes())
        cases = [
            json.loads((FIX / "notion-incomplete.json").read_bytes()),
            {**good, "id": "22222222-2222-4222-8222-222222222222"},
            {**good, "unknown_block_ids": ["block"]},
            {k: v for k, v in good.items() if k != "truncated"},
            {**good, "warnings": [{"code": "unsupported_block"}]},
            {"markdown": "nothing else"},
        ]
        for body in cases:
            with self.subTest(body=body):
                s = m.capture(
                    [ref],
                    lambda _, body=body: m.Content(json.dumps(body).encode(), "application/json"),
                    NOW,
                )
                self.assertTrue(s.problems)

    def test_changed_source_detected_without_treating_capture_time_as_a_change(self):
        m = self.module()
        ref = m.Source("page", "notion", PAGE, "https://www.notion.so/" + PAGE)

        def reader(name):
            return lambda _: m.Content((FIX / name).read_bytes(), "application/json")

        a = m.capture([ref], reader("notion-ready.json"), NOW)
        b = m.capture([ref], reader("notion-ready.json"), NOW.replace(hour=22))
        c = m.capture([ref], reader("notion-changed.json"), NOW)
        self.assertEqual(a.changed_sources(b), ())
        self.assertEqual(a.changed_sources(c), ("page",))

    def test_capture_does_not_follow_document_instructions_or_links(self):
        m = self.module()
        ref = m.Source("page", "notion", PAGE, "https://www.notion.so/" + PAGE)
        calls = []

        def reader(source):
            calls.append(source.key)
            return m.Content((FIX / "notion-injection.json").read_bytes(), "application/json")

        s = m.capture([ref], reader, NOW)
        self.assertEqual(calls, ["page"])
        self.assertFalse(json.loads(s.to_bytes())["ready_for_dispatch"])
        self.assertEqual(s.records[0].content.data, (FIX / "notion-injection.json").read_bytes())

    def test_oversized_or_duplicate_sources_refused_without_truncation(self):
        m = self.module()
        ref = m.Source("notes", "project", "project-1", "https://linear.app/example/project/p")
        with self.assertRaises(ValueError):
            m.capture([ref, ref], lambda _: self.fail("must validate before reading"), NOW)
        s = m.capture(
            [ref], lambda _: m.Content(b"x" * (m.MAX_SOURCE_BYTES + 1), "text/plain"), NOW
        )
        self.assertTrue(s.problems)
        self.assertIsNone(s.records[0].content)


class NotionTests(unittest.TestCase):
    module = CaptureTests.module

    def notion(self):
        self.module()
        self.assertTrue(
            (FIX.parents[2] / "controller/prepare/notion.py").exists(),
            "Notion reader has not been implemented",
        )
        return importlib.import_module("controller.prepare.notion")

    def test_only_allowed_page_endpoint_gets_credential_and_read_is_bounded(self):
        m = self.module()
        n = self.notion()
        requests = []

        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json"}

        def opener(req, timeout):
            requests.append(req)
            return Response((FIX / "notion-ready.json").read_bytes())

        reader = n.NotionReader(lambda: "synthetic-key", frozenset({PAGE}), opener=opener)
        ref = m.Source("page", "notion", PAGE, "https://untrusted.example/ignored")
        content = reader(ref)
        self.assertEqual(content.data, (FIX / "notion-ready.json").read_bytes())
        self.assertEqual(
            requests[0].full_url, "https://api.notion.com/v1/pages/" + PAGE + "/markdown"
        )
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer synthetic-key")
        bad = m.Source(
            "other", "notion", "22222222-2222-4222-8222-222222222222", "https://notion.so/other"
        )
        with self.assertRaises(m.SourceUnavailable):
            reader(bad)
        self.assertEqual(len(requests), 1)

    def test_http_failures_are_typed_without_exposing_key_or_server_body(self):
        m = self.module()
        n = self.notion()
        ref = m.Source("page", "notion", PAGE, "https://www.notion.so/" + PAGE)
        for status, reason in [
            (403, "permission-denied"),
            (404, "missing-or-inaccessible"),
            (429, "temporarily-unavailable"),
            (302, "redirect-refused"),
        ]:

            def opener(req, timeout, status=status):
                raise urllib.error.HTTPError(
                    req.full_url, status, "synthetic-key", {}, io.BytesIO(b"synthetic-key")
                )

            with self.subTest(status=status), self.assertRaises(m.SourceUnavailable) as cm:
                n.NotionReader(lambda: "synthetic-key", frozenset({PAGE}), opener=opener)(ref)
            self.assertEqual(str(cm.exception), reason)


class PreparationHolds(unittest.TestCase):
    def test_linked_notion_and_embedded_design_do_not_silently_become_text_only_tasks(self):
        for extra in (
            "[Product notes](https://www.notion.so/" + PAGE + ")",
            "![Design](https://assets.example.test/design.png)",
            '<img src="https://assets.example.test/design.png">',
        ):
            snap = ticket(
                "## Acceptance criteria\n- [ ] The page shows reading books.\n\n## Context\n"
                + extra
            )
            out = prep(snap).prepare(authorize(snap), project().as_mapping())
            with self.subTest(extra=extra):
                self.assertIsInstance(out, Question)
                self.assertEqual(out.kind, "factory")
                self.assertIn("context", out.text.lower())

    def test_text_only_ticket_still_prepares(self):
        snap = ticket("## Acceptance criteria\n- [ ] sortBooks(books) sorts books.\n")
        self.assertIsInstance(prep(snap).prepare(authorize(snap), project().as_mapping()), Prepared)

    def test_attachment_metadata_reaches_preparation_guard_without_changing_intake_revision(self):
        raw = {
            "id": "issue-ENG-187",
            "identifier": "ENG-187",
            "title": "Reading",
            "description": "## Acceptance criteria\n- [ ] The page shows books.",
            "project": {"id": project().linear_project_id},
            "team": {"id": "team-eng"},
            "parent": None,
            "labels": {"nodes": []},
            "attachments": {
                "nodes": [{"id": "asset-1", "url": "https://assets.example.test/design.png"}],
                "pageInfo": {"hasNextPage": False},
            },
        }
        snap = LinearTicketReader(lambda q, v: {"issue": raw}).fetch(raw["id"])
        out = prep(snap).prepare(authorize(snap), project().as_mapping())
        self.assertIsInstance(out, Question)
        self.assertEqual(out.kind, "factory")


class CaptureBoundaryTests(unittest.TestCase):
    module = CaptureTests.module

    def test_invalid_reader_result_is_a_recorded_failure_not_a_broken_snapshot(self):
        m = self.module()
        ref = m.Source("notes", "project", "p", "https://linear.app/example/project/p")
        for bad in (None, m.Content("not bytes", "text/plain")):
            with self.subTest(bad=bad):
                s = m.capture([ref], lambda _, bad=bad: bad, NOW)
                self.assertEqual(s.problems, ("notes: invalid-response",))
                self.assertIsNone(json.loads(s.to_bytes())["sources"][0]["base64"])

    def test_total_limit_refuses_whole_source_and_retains_earlier_sources(self):
        m = self.module()
        refs = [
            m.Source(f"notes{i}", "project", str(i), "https://linear.app/example/project/p")
            for i in range(5)
        ]
        s = m.capture(refs, lambda _: m.Content(b"a" * 240000, "text/plain"), NOW)
        self.assertEqual(len(s.records), 5)
        self.assertEqual(s.problems, ("notes4: too-large",))
        self.assertEqual(len(s.records[0].content.data), 240000)

    def test_existing_snapshot_is_not_overwritten_and_same_capture_can_be_saved_twice(self):
        m = self.module()
        s = m.capture(
            [m.Source("p", "project", "p", "https://linear.app/project/p")],
            lambda _: m.Content(b"hello", "text/plain"),
            NOW,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = s.save(root)
            self.assertEqual(s.save(root), p)
            p.write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                s.save(root)
            self.assertEqual(p.read_bytes(), b"corrupted")

    def test_notion_response_limit_applies_while_reading(self):
        m = self.module()
        n = importlib.import_module("controller.prepare.notion")

        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json"}

            def read(self, size=-1):
                if size < 0 or size > m.MAX_SOURCE_BYTES + 1:
                    raise AssertionError("unbounded network read")
                return super().read(size)

        ref = m.Source("page", "notion", PAGE, "https://notion.so/" + PAGE)
        reader = n.NotionReader(
            lambda: "key",
            frozenset({PAGE}),
            opener=lambda req, timeout: Response(b"x" * (m.MAX_SOURCE_BYTES + 2)),
        )
        with self.assertRaisesRegex(m.SourceUnavailable, "too-large"):
            reader(ref)

    def test_notion_redirect_cannot_forward_authorization_to_other_host(self):
        n = importlib.import_module("controller.prepare.notion")
        handler = n._NoRedirect()
        import urllib.request

        request = urllib.request.Request(
            "https://api.notion.com/v1/pages/" + PAGE + "/markdown",
            headers={"Authorization": "Bearer synthetic-key"},
        )
        self.assertIsNone(
            handler.redirect_request(
                request, io.BytesIO(), 302, "Found", {}, "https://outside.example/collect"
            )
        )

    def test_attachment_guard_runs_before_base_selection_or_drafting(self):
        from dataclasses import replace

        from controller.prepare.preparer import Preparer
        from tests.test_prepare import POLICY, GitHub, Reader

        snap = replace(
            ticket("## Acceptance criteria\n- [ ] The page shows books."),
            attachments=("https://assets.example.test/design.png",),
        )
        gh = GitHub()
        out = Preparer(Reader(snap), gh, lambda: POLICY).prepare(
            authorize(snap), project().as_mapping()
        )
        self.assertIsInstance(out, Question)
        self.assertEqual(gh.paths, [])


class ContextReferenceGuardTests(unittest.TestCase):
    def test_notion_and_design_urls_hold_with_markdown_delimiters_and_ports(self):
        for url in (
            "https://example.notion.site",
            "https://www.notion.so:443/" + PAGE,
            "https://www.figma.com/design/abc/Reading-Room",
        ):
            for linked in (f"[Brief]({url})", f"<{url}>"):
                snap = ticket(
                    "## Acceptance criteria\n- [ ] The page shows books.\n\n## Context\n" + linked
                )
                with self.subTest(linked=linked):
                    out = prep(snap).prepare(authorize(snap), project().as_mapping())
                    self.assertIsInstance(out, Question)
                    self.assertEqual(out.kind, "factory")

    def test_unobserved_attachment_connection_cannot_establish_complete_context(self):
        raw = {
            "id": "issue-ENG-187",
            "identifier": "ENG-187",
            "title": "Reading",
            "description": "## Acceptance criteria\n- [ ] The page shows books.",
            "project": {"id": project().linear_project_id},
            "team": {"id": "team-eng"},
            "parent": None,
            "labels": {"nodes": []},
        }
        for attachments in (
            None,
            {},
            {"nodes": []},
            {"nodes": [], "pageInfo": {"hasNextPage": True}},
        ):
            with self.subTest(attachments=attachments):
                snap = LinearTicketReader(
                    lambda q, v, attachments=attachments: {
                        "issue": {**raw, "attachments": attachments}
                    }
                ).fetch(raw["id"])
                self.assertFalse(snap.attachments_complete)
                self.assertIsInstance(
                    prep(snap).prepare(authorize(snap), project().as_mapping()), Question
                )
        raw["attachments"] = {"nodes": [], "pageInfo": {"hasNextPage": False}}
        snap = LinearTicketReader(lambda q, v: {"issue": raw}).fetch(raw["id"])
        self.assertIsInstance(prep(snap).prepare(authorize(snap), project().as_mapping()), Prepared)


class ReferenceClassificationTests(unittest.TestCase):
    def prepare(self, *, attachments=(), context="", complete=True):
        from dataclasses import replace

        snap = replace(
            ticket("## Acceptance criteria\n- [ ] The page shows books.\n\n## Context\n" + context),
            attachments=attachments,
            attachments_complete=complete,
        )
        return prep(snap).prepare(authorize(snap), project().as_mapping())

    def test_github_pr_and_commit_attachments_do_not_block_redrafting(self):
        for url in (
            "https://github.com/rNavarrete/software-factory/pull/28",
            "https://github.com/rNavarrete/software-factory/pull/28#issuecomment-1",
            "https://github.com/rNavarrete/software-factory/commit/" + "a" * 40,
        ):
            with self.subTest(url=url):
                out = self.prepare(attachments=(url,))
                self.assertIsInstance(out, Prepared)
                revised = self.prepare(
                    attachments=(url,), context="Keep existing keyboard behavior."
                )
                self.assertIsInstance(revised, Prepared)

    def test_parenthesized_image_paths_still_hold(self):
        for url in (
            "https://assets.example.test/design(v2).png",
            "assets.example.test/design(v2).png",
        ):
            for reference in (url, f"<{url}>", f"[Design](<{url}>)"):
                with self.subTest(reference=reference):
                    self.assertIsInstance(self.prepare(context=reference), Question)

    def test_github_delivery_links_do_not_hide_other_or_unobserved_attachments(self):
        pr = "https://github.com/rNavarrete/software-factory/pull/28"
        for url in (
            "https://uploads.linear.app/workspace/asset/brief.pdf",
            "https://github.com/rNavarrete/software-factory/blob/main/design.png",
            "https://github.com.evil.example/rNavarrete/software-factory/pull/28",
            "https://github.com@evil.example/rNavarrete/software-factory/pull/28",
            "https://github.com/rNavarrete/software-factory/issues/28",
            "https://github.com/rNavarrete/software-factory/commit/not-a-sha",
            "unreadable attachment",
        ):
            with self.subTest(url=url):
                self.assertIsInstance(self.prepare(attachments=(pr, url)), Question)
        self.assertIsInstance(self.prepare(attachments=(pr,), complete=False), Question)

    def test_linear_uploads_hold_regardless_of_file_extension(self):
        for url in (
            "https://uploads.linear.app/workspace/asset/brief.pdf",
            "https://uploads.linear.app/workspace/asset/requirements.docx?signature=synthetic",
            "https://uploads.linear.app/workspace/asset/opaque-id",
            "uploads.linear.app/workspace/asset/brief.pdf",
        ):
            with self.subTest(url=url):
                self.assertIsInstance(self.prepare(context=f"[Brief]({url})"), Question)

    def test_notion_com_and_schemeless_design_links_hold(self):
        for url in (
            "https://app.notion.com/p/acme",
            "https://www.notion.com/Brief-" + PAGE,
            "notion.so/" + PAGE,
            "www.notion.so/" + PAGE,
            "acme.notion.site/brief",
            "app.notion.com/p/acme",
            "figma.com/design/abc/Reading",
            "www.figma.com/design/abc/Reading",
            "//www.figma.com/design/abc/Reading",
            "assets.example.test/design.png?version=2",
        ):
            for reference in (url, f"[Design]({url})", f"<{url}>"):
                with self.subTest(reference=reference):
                    out = self.prepare(context=reference)
                    self.assertIsInstance(out, Question)
                    self.assertEqual(out.kind, "factory")

    def test_ordinary_links_and_host_lookalikes_do_not_gain_context_semantics(self):
        for text in (
            "https://github.com/rNavarrete/software-factory/pull/28",
            "https://example.com/docs",
            "https://notion.so.example.com/brief",
            "notion.so.example.com/brief",
            "myfigma.com/design/abc",
            "person@notion.so",
        ):
            with self.subTest(text=text):
                self.assertIsInstance(self.prepare(context=text), Prepared)
