"""Reading the independent mapper's review comment (ENG-145).

Only a ``factory-review/v1`` block in a comment by an allowlisted login counts,
for the exact contract digest and revision; who mapped is the comment's author,
never a name in the JSON. Offline, no I/O.
"""

import json
import unittest
from dataclasses import replace

from redteam import fixtures as fx
from tests.github_world import PR_URL, honest_review
from verify.assertions import ProofOutcome
from verify.review import DEFAULT_MAPPERS, MARKER, Comment, read_review, review_block

T1 = "2026-10-08T15:00:00Z"
T2 = "2026-10-08T16:00:00Z"


def comment(body, author="rNavarrete", cid=900, updated=T1, created=None):
    created = updated if created is None else created
    return Comment(cid, author, f"{PR_URL}#issuecomment-{cid}", body, created, updated)


def block(data) -> str:
    return f"```{MARKER}\n{json.dumps(data, indent=2)}\n```"


def honest_data(**changes) -> dict:
    """The honest review block's JSON, as a dict to change."""
    text = honest_review()
    start = text.index("{")
    end = text.rindex("}") + 1
    data = json.loads(text[start:end])
    data.update(changes)
    return data


class ReviewTests(unittest.TestCase):
    def read(self, *comments, candidate=None, digest=None, **kw):
        return read_review(comments, digest or str(fx.DIGEST), candidate or fx.candidate(), **kw)

    def test_honest_review_is_read(self):
        c = comment(honest_review())
        r = self.read(c)
        self.assertEqual(r.url, c.url)
        self.assertEqual(r.ignored, ())
        self.assertEqual([lk.criterion for lk in r.links], ["ac1", "ac2"])
        self.assertEqual([p.criterion for p in r.proofs], ["ac1", "ac2"])
        self.assertEqual(r.limits, ())
        lk = r.links[0]
        self.assertEqual(lk.path, fx.TEST_FILE)
        self.assertEqual(lk.test, fx.AC1_TEST)
        self.assertEqual(lk.assertion, fx.AC1_ASSERT)
        self.assertEqual(lk.contract_digest, str(fx.DIGEST))
        self.assertEqual(lk.commit, fx.HEAD)
        self.assertEqual(lk.base_commit, fx.MAIN)
        self.assertEqual(lk.mapper, "rNavarrete")
        p = r.proofs[0]
        self.assertEqual(p.outcome, ProofOutcome.FAILED_ERROR)
        self.assertEqual(p.tests_commit, fx.HEAD)
        self.assertEqual(p.code_commit, fx.BASE)
        self.assertEqual(p.by, "rNavarrete")
        self.assertEqual(p.url, c.url)

    def test_limits_are_read(self):
        body = honest_review(limits=[{"criterion": "ac2", "reason": "new function"}])
        r = self.read(comment(body))
        self.assertEqual(len(r.limits), 1)
        self.assertEqual(r.limits[0].by, "rNavarrete")
        self.assertEqual(r.limits[0].commit, fx.HEAD)

    def test_no_comments_is_no_review(self):
        r = self.read()
        self.assertIsNone(r.url)
        self.assertEqual((r.links, r.proofs, r.limits, r.ignored), ((), (), (), ()))

    def test_comments_without_a_block_are_not_reviews(self):
        r = self.read(
            comment("LGTM, ac1 and ac2 mapped."),
            comment("```factory-review/v2\n{}\n```", cid=901),
            comment("```json\n" + json.dumps(honest_data()) + "\n```", cid=902),
        )
        self.assertIsNone(r.url)
        self.assertEqual(r.ignored, ())

    def test_default_mappers(self):
        self.assertEqual(DEFAULT_MAPPERS, frozenset({"rNavarrete"}))

    # --- who may map ---

    def test_author_not_allowlisted_is_ignored(self):
        for author in (
            fx.WORKER,
            "rnavarrete-factory-bot[bot]",
            "someone",
            "rNavarrete-x",
            "xrNavarrete",
            "rNavarrete[bot]",
            "github-actions[bot]",
        ):
            with self.subTest(author=author):
                r = self.read(comment(honest_review(), author=author))
                self.assertIsNone(r.url)
                self.assertEqual(r.links, ())
                self.assertEqual(len(r.ignored), 1)
                self.assertTrue(r.ignored[0].startswith("untrusted:"), r.ignored)
                self.assertIn(repr(author), r.ignored[0])

    def test_allowlisted_author_in_another_case_counts(self):
        for author in ("rnavarrete", "RNAVARRETE", "rNavarrete"):
            with self.subTest(author=author):
                r = self.read(comment(honest_review(), author=author))
                self.assertIsNotNone(r.url)
                self.assertEqual({lk.mapper for lk in r.links}, {author})

    def test_custom_mappers(self):
        r = self.read(comment(honest_review(), author=fx.MAPPER), mappers=frozenset({fx.MAPPER}))
        self.assertIsNotNone(r.url)
        r = self.read(comment(honest_review()), mappers=frozenset({fx.MAPPER}))
        self.assertIsNone(r.url)

    def test_untrusted_review_never_replaces_a_trusted_one(self):
        good = comment(honest_review(), cid=900, updated=T1)
        bad = comment(honest_review(links=[]), author="someone", cid=901, updated=T2)
        r = self.read(good, bad)
        self.assertEqual(r.url, good.url)
        self.assertEqual(len(r.links), 2)

    # --- mapper and by come from the comment, never the JSON ---

    def test_mapper_and_by_come_from_the_comment_author(self):
        data = honest_data(mapper="someone", by="someone", author="someone", url="https://x")
        for item in data["links"]:
            item["mapper"] = "someone"
        for item in data["proofs"]:
            item.update(by="someone", url="https://evil.example/proof")
        data["limits"] = [{"criterion": "ac2", "reason": "r", "by": "someone"}]
        c = comment(block(data))
        r = self.read(c)
        self.assertIsNotNone(r.url)
        self.assertEqual({lk.mapper for lk in r.links}, {"rNavarrete"})
        self.assertEqual({p.by for p in r.proofs}, {"rNavarrete"})
        self.assertEqual({p.url for p in r.proofs}, {c.url})
        self.assertEqual({lim.by for lim in r.limits}, {"rNavarrete"})
        self.assertEqual(r.url, c.url)

    def test_commits_and_digest_come_from_the_candidate(self):
        data = honest_data()
        for item in data["proofs"]:
            item.update(code_commit=fx.HEAD, tests_commit=fx.OLD_HEAD)
        r = self.read(comment(block(data)))
        self.assertEqual({p.code_commit for p in r.proofs}, {fx.BASE})
        self.assertEqual({p.tests_commit for p in r.proofs}, {fx.HEAD})

    # --- stale ---

    def test_wrong_digest_commit_base_or_merge_base_is_stale(self):
        cases = {
            "contract_digest": "f" * 64,
            "commit": fx.NEW_HEAD,
            "base_commit": fx.NEW_MAIN,
            "merge_base": fx.OLD_HEAD,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                r = self.read(comment(block(honest_data(**{key: value}))))
                self.assertIsNone(r.url)
                self.assertEqual(r.links, ())
                self.assertEqual(len(r.ignored), 1)
                self.assertTrue(r.ignored[0].startswith("stale:"), r.ignored)

    def test_missing_binding_fields_are_stale(self):
        for key in ("contract_digest", "commit", "base_commit", "merge_base"):
            with self.subTest(key=key):
                data = honest_data()
                del data[key]
                r = self.read(comment(block(data)))
                self.assertIsNone(r.url)
                self.assertTrue(r.ignored[0].startswith("stale:"), r.ignored)

    def test_review_of_an_older_push_is_not_reused(self):
        c = comment(honest_review())
        r = self.read(c, candidate=fx.candidate(head_commit=fx.NEW_HEAD))
        self.assertIsNone(r.url)
        self.assertIn("stale:", r.ignored[0])

    def test_review_against_another_base_is_not_reused(self):
        r = self.read(comment(honest_review()), candidate=fx.candidate(base_commit=fx.NEW_MAIN))
        self.assertIsNone(r.url)

    def test_review_for_another_contract_is_not_reused(self):
        r = self.read(comment(honest_review()), digest="f" * 64)
        self.assertIsNone(r.url)
        self.assertIn("stale:", r.ignored[0])

    # --- ambiguous and unreadable ---

    def test_two_blocks_in_one_comment_is_ambiguous(self):
        body = honest_review() + "\n\n" + review_block(str(fx.DIGEST), fx.candidate())
        r = self.read(comment(body))
        self.assertIsNone(r.url)
        self.assertEqual(len(r.ignored), 1)
        self.assertTrue(r.ignored[0].startswith("ambiguous:"), r.ignored)
        self.assertIn("2 review blocks", r.ignored[0])

    def test_unreadable_blocks(self):
        bad_link = honest_data()
        del bad_link["links"][0]["assertion"]
        blank_link = honest_data()
        blank_link["links"][0]["test"] = "   "
        bad_outcome = honest_data()
        bad_outcome["proofs"][0]["outcome"] = "failed"
        cases = {
            "malformed JSON": f"```{MARKER}\n{{not json\n```",
            "not an object": block([1, 2, 3]),
            "missing assertion": block(bad_link),
            "blank test": block(blank_link),
            "bad outcome": block(bad_outcome),
            "links not a list": block(honest_data(links={"criterion": "ac1"})),
            "links of strings": block(honest_data(links=["ac1"])),
            "proofs not a list": block(honest_data(proofs="all good")),
        }
        for label, body in cases.items():
            with self.subTest(label):
                r = self.read(comment(body))
                self.assertIsNone(r.url)
                self.assertEqual(r.links, ())
                self.assertEqual(len(r.ignored), 1)
                self.assertTrue(r.ignored[0].startswith("unreadable:"), r.ignored)

    def test_unreadable_newer_review_keeps_the_older_valid_one(self):
        good = comment(honest_review(), cid=900, updated=T1)
        bad = comment(f"```{MARKER}\n{{oops\n```", cid=901, updated=T2)
        r = self.read(good, bad)
        self.assertEqual(r.url, good.url)
        self.assertTrue(any(i.startswith("unreadable:") for i in r.ignored))

    # --- newest wins ---

    def test_newest_of_two_valid_reviews_wins(self):
        older = comment(honest_review(links=[], proofs=[], limits=[]), cid=900, updated=T1)
        newer = comment(honest_review(), cid=901, updated=T2)
        for order in ((older, newer), (newer, older)):
            with self.subTest(order=[c.id for c in order]):
                r = self.read(*order)
                self.assertEqual(r.url, newer.url)
                self.assertEqual(len(r.links), 2)
                self.assertEqual(
                    r.ignored, (f"replaced: review comment {older.url} has a newer review",)
                )

    def test_same_time_newest_is_higher_id(self):
        a = comment(honest_review(links=[]), cid=900, updated=T1)
        b = comment(honest_review(), cid=901, updated=T1)
        self.assertEqual(self.read(b, a).url, b.url)

    def test_newest_is_by_creation_time_not_id(self):
        posted_later = comment(honest_review(), cid=900, updated=T2)
        posted_first = comment(honest_review(links=[]), cid=901, updated=T1)
        r = self.read(posted_later, posted_first)
        self.assertEqual(r.url, posted_later.url)

    # --- edited comments ---

    def test_edited_review_comment_is_ignored(self):
        c = comment(honest_review(), created=T1, updated=T2)
        r = self.read(c)
        self.assertIsNone(r.url)
        self.assertEqual(r.links, ())
        self.assertEqual(len(r.ignored), 1)
        self.assertTrue(r.ignored[0].startswith("edited:"), r.ignored)
        self.assertIn(c.url, r.ignored[0])

    def test_worker_editing_rolandos_comment_keeps_his_name_and_still_does_not_count(self):
        # GitHub keeps the author when someone with write access edits a comment.
        original = comment(honest_review(), cid=900, created=T1, updated=T1)
        tampered = comment(
            honest_review(proofs=[]), author="rNavarrete", cid=900, created=T1, updated=T2
        )
        self.assertIsNotNone(self.read(original).url)
        r = self.read(tampered)
        self.assertIsNone(r.url)
        self.assertTrue(r.ignored[0].startswith("edited:"))

    def test_edited_newer_review_leaves_the_older_unedited_one(self):
        older = comment(honest_review(), cid=900, created=T1, updated=T1)
        edited = comment(
            honest_review(links=[]), cid=901, created=T2, updated="2026-10-08T17:00:00Z"
        )
        r = self.read(older, edited)
        self.assertEqual(r.url, older.url)
        self.assertEqual(len(r.links), 2)
        self.assertTrue(any(i.startswith("edited:") for i in r.ignored))

    def test_edited_comment_without_a_block_is_not_mentioned(self):
        r = self.read(comment("thanks!", created=T1, updated=T2))
        self.assertEqual(r.ignored, ())

    def test_edit_check_comes_before_trust(self):
        r = self.read(comment(honest_review(), author="someone", created=T1, updated=T2))
        self.assertIsNone(r.url)
        self.assertEqual(len(r.ignored), 1)

    def test_block_round_trips_through_review_block(self):
        cand = replace(fx.candidate(), head_commit=fx.NEW_HEAD)
        body = review_block(str(fx.DIGEST), cand, links=honest_data()["links"])
        r = self.read(comment(body), candidate=cand)
        self.assertIsNotNone(r.url)
        self.assertEqual({lk.commit for lk in r.links}, {fx.NEW_HEAD})


if __name__ == "__main__":
    unittest.main()
