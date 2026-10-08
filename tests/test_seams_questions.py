"""The product-question types in seams.py (ENG-178 / ENG-175)."""

from __future__ import annotations

import unittest

from controller.service.seams import Option, Question, ReportFailed


class QuestionTest(unittest.TestCase):
    def test_plain_text_question_still_works(self) -> None:
        q = Question("Should the list sort by title?")
        self.assertEqual(q.options, ())
        self.assertIsNone(q.recommended)

    def test_full_question(self) -> None:
        q = Question(
            "Sort by title or by date added?",
            context="The ticket says 'sorted' but not by what.",
            options=(Option("A", "Title", "A to Z."), Option("B", "Date", "Newest first.")),
            recommended="b",
            if_no_answer="The ticket waits; nothing starts.",
            key="q-sort-1",
        )
        self.assertEqual(q.recommended, "b")

    def test_rejects_bad_questions(self) -> None:
        a = Option("A", "Title", "A to Z.")
        for kwargs in (
            {"text": "  "},
            {"text": "x", "options": (a, Option("a", "Again", "Dup."))},
            {"text": "x", "options": (a,), "recommended": "C"},
            {"text": "x", "recommended": "A"},
            {"text": "x", "key": "has space"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Question(**kwargs)  # type: ignore[arg-type]

    def test_rejects_bad_options(self) -> None:
        for args in (("", "l", "c"), ("A B", "l", "c"), ("A", "", "c"), ("A", "l", " ")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                Option(*args)


class ReportFailedTest(unittest.TestCase):
    def test_hold_all_defaults_off(self) -> None:
        self.assertFalse(ReportFailed("x").hold_all)
        self.assertTrue(ReportFailed("x", hold_all=True).hold_all)


if __name__ == "__main__":
    unittest.main()


class PreparerSeamTests(unittest.TestCase):
    """ENG-175's additions: a question's kind and a prepared task's summary."""

    def test_kind_defaults_to_product_and_is_checked(self):
        from controller.service.seams import QUESTION_KINDS, Prepared, Question

        self.assertEqual(Question("What colour?").kind, "product")
        for kind in QUESTION_KINDS:
            Question("x", kind=kind)
        with self.assertRaises(ValueError):
            Question("x", kind="approval")
        self.assertEqual(Prepared({}).summary, "")
