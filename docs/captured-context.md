# Captured product context (ENG-199, in progress)

The preparer must not start a ticket while silently ignoring its linked requirements
or designs. `Preparer.prepare` now returns a factory limitation notice when it sees
a Notion or Figma link, an embedded design, a direct image link or a source attachment. Text-only
tickets keep their existing behavior. No product answer or user-supplied test example
is requested to work around this limitation.

This is a conservative temporary hold, not completed external-context support.
Attachments are observed separately from intake's ticket-text revision; adding fields
to that revision here would break the existing authorization formula. An incomplete
attachment listing also holds. The source guard cannot detect every possible prose
reference or custom document URL.

GitHub PR and commit attachments are delivery records and do not trigger this hold,
so automatic GitHub attachments do not prevent retries or revised tickets from being
drafted. This exception is restricted to GitHub PR/commit URL paths: blobs, issues,
uploaded assets and unknown attachments still hold. An incomplete attachment listing
still holds even when all visible attachments are PRs.

The guard recognizes `notion.so`, `notion.site`, `notion.com` (including
`app.notion.com`), Figma, and Linear's `uploads.linear.app` file links. Linear uploads
hold regardless of extension, including PDFs and opaque file IDs. Known source links
and image URLs are recognized with or without an `https://` prefix. Hostname matching
does not confuse `notion.so.example.com` with Notion.

## What the capture code does

`controller.prepare.context.capture(sources, read, at)` reads an explicitly selected
list. Each source has a key, kind, ID, URL and required/optional classification supplied
by the controller's selection policy. The reader returns exact bytes, a media type and
revision metadata when available. Supported kinds are project guidance, Notion,
repository guidance and images. This function follows no links and executes no source
instructions. Readers must enforce authorization and response limits while fetching.

The snapshot retains exact bytes, content hashes, revision metadata, retrieval time
and a safe failure reason. Required failures are `problems`; optional failures are
`limitations`. Notion responses must identify the requested page and explicitly report
complete markdown, no unknown blocks and no warnings. Incomplete responses are retained
as evidence, with their failure, rather than silently treated as complete.

Capture is bounded to 16 sources, 256,000 bytes each and 1,000,000 bytes total. Oversized
sources are rejected whole. `save(controller_owned_directory)` writes a complete
snapshot atomically with mode 0600 and a SHA-256 filename. It never replaces a different
existing file. The directory is private controller storage, not a source-provided path.
Raw source content can be private; do not put the snapshot in a public PR or comment.

`changed_sources(current)` identifies added, removed or changed sources using identity,
requiredness, revision, media type, content hash and completeness. Retrieval time alone
does not make a source materially different. This comparison is not wired into dispatch
yet, and a changed digest is not a new human approval.

All snapshots say `ready_for_dispatch: false` and `inspection: not_run`. Image MIME
recognition does not validate or inspect its pixels. Complete retrieval does not prove
that two documents agree, that an image is usable or that the worker understood it.
There is deliberately no switch here to claim those checks passed.

## Notion reader

`NotionReader(key_supplier, allowed_pages)` makes GET requests only to the fixed Notion
markdown endpoint for explicitly allowed page IDs. It ignores the source URL for network
routing, refuses redirects, caps the response while reading and uses a 30-second timeout.
The credential is supplied by trusted controller configuration and never becomes captured
content. HTTP error bodies and credentials are not included in failure messages.

The reader uses `Notion-Version: 2026-03-11`. Its endpoint does not provide a source edit
revision, so that field remains unavailable; the exact response hash is retained. A 404
is described as missing **or inaccessible**, because missing permission can look the same.
Truncated or unsupported content is reported as incomplete; subtree recovery is not built.

No Notion secret, production allowlist or service wiring is added by this change.
Project/repository/image readers and source selection are not yet wired either. Tests
inject source bytes and HTTP responses; they do not contact real accounts.

## Verification and remaining work

```sh
python3 -m unittest tests.test_context_capture tests.test_context_fixture tests.test_prepare
python3 -m tools.context_fixture --out /tmp/new-eng199-proof
```

The existing synthetic probe now also creates a retained snapshot using the capture code.
Tests exercise missing access, changed content, incomplete/wrong-page/malformed responses,
source limits, private atomic storage, credential confinement and the preparation hold.
The malicious-document fixture is retained inertly; that is not a claim of downstream
model prompt-injection resistance.

Before removing the hold: configure authorized source selection/readers, assess captured
text and actual image pixels for contradictions, bind that evidence to the contract and
authorization, recheck material versions before dispatch, and prove delivery to the hosted
worker and independent reviewer. The current rule drafter cannot perform that semantic
assessment. Use the existing ENG-163 qualification path with fresh synthetic inputs; no
real launch, new credential or deployment was performed here. ENG-199 remains In Progress.

Reference: [Notion markdown API](https://developers.notion.com/reference/retrieve-page-markdown).
URL forms: [Notion page domains](https://www.notion.com/en-gb/help/manage-your-notion-sites),
[Linear file storage](https://linear.app/developers/file-storage-authentication).
