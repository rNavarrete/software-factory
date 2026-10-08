# Synthetic context qualification (ENG-199)

This offline probe carries a fictional Linear ticket, project brief, Notion response,
repository notes and annotated PNG through the factory's existing task envelope.
It needs no account, credential, network connection or user-provided example.
It does not launch a worker or enable context capture in production.

Run from the repository root, choosing an output directory that does not exist:

```sh
python3 -m tools.context_fixture --out /tmp/eng199-context-proof
python3 -m unittest tests.test_context_fixture -v
```

The output contains `fire-text.json`, recovered source files and `report.json`.
The report distinguishes a successful local byte round trip from hosted-worker and
reviewer image inspection, which remain `not_run`. The contract deliberately uses
an all-zero base commit; it is a transport specimen, not a dispatchable task.

## What this checks

Each file has a name, byte length and SHA-256 digest. The packet is base64 encoded
into ordered contract input chunks. The existing contract digest therefore binds
the exact text and image bytes. The probe exercises the real contract validation,
fire-envelope builder and payload limit. The decoder checks the envelope, chunk
order, packet digest and file digests before writing recovered files.

Executable tests cover exact binary round trips, modification after authorization,
missing chunks, unsafe filenames, oversized payload rejection and honest reporting
of which checks actually ran. The fixture answer key is excluded from the packet.

This proves neither source permissions nor Notion retrieval, completeness, conflict
resolution, prompt-injection resistance, image readability by a model or reviewer
context delivery. No production module uses this probe. Hashing verifies bytes;
it does not establish that anyone understood their contents.

## Scenario fixtures for the remaining implementation

`tests/fixtures/context/cases.json` describes expected future behavior. These are
test vectors, **not passing end-to-end tests**. Only the complete scenario is packed
by the offline probe; the other responses are inputs for subsequent integration tests.

| Scenario | Expected behavior when context capture is implemented |
| --- | --- |
| Complete sources | Retain source text and image bytes; prove visual consumption separately. |
| Missing required page | Hold and request access or replacement context. |
| Missing optional page | Record the limitation; continue only with sufficient required context. |
| Conflicting requirements | Ask which requirement applies. |
| Source changed after capture | Preserve the captured version; refresh authorization before a new dispatch. |
| Incomplete Notion response | Resolve omitted content or hold, even when HTTP retrieval succeeds. |
| Instructions embedded in a source | Do not grant permission to alter checks, scope or release policy. |
| Unreadable required image | Hold; downloading bytes alone is insufficient. |

The Notion files model the official markdown endpoint's `markdown`, `truncated`
and `unknown_block_ids` fields. All identities, URLs and product details are
synthetic. A successful response can still be incomplete, and a 404 can indicate
missing access rather than a nonexistent page.

## Hosted qualification still required

The routine fire API accepts text, with a 65,536-character limit. A small image can
fit as encoded bytes inside that text; this is a candidate transport to qualify,
not an image-upload API. Encoding adds overhead. This probe rejects oversized
payloads without truncation and does not solve delivery of larger screenshots.

For a live qualification, generate a fresh synthetic image and keep its answer key
outside the repository and worker context. The committed fixture is reproducible,
but its committed answer key is not a blind evaluation. Use a current eligible base,
the ordinary dispatcher, approval, attempt budget and completion checks. Do not
fire the API directly or dispatch this offline specimen.

The worker must verify and materialize the captured bytes, open the actual image
with an image-reading tool, and answer questions whose answers appear only in its
pixels. Record the session and evidence. Independently verify that a reviewer can
consume the same captured context. A session link, successful launch or correct
local hash is not a substitute for either proof. No live launch is part of this PR.

ENG-199 remains open for source acquisition, retained source/version manifests,
preparation and authorization integration, worker/reviewer delivery, executable
scenario checks and hosted qualification. This probe introduces no new credentials
or storage service and makes no production transport commitment.

## References

- [Routine fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire)
- [Claude Code image workflows](https://code.claude.com/docs/en/common-workflows#work-with-images)
- [Notion page markdown API](https://developers.notion.com/reference/retrieve-page-markdown)

General Claude Code image support does not establish image support in this factory's
particular hosted session; the live qualification above must establish that.
