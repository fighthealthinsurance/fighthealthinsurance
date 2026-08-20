"""Tests for the shared response-similarity helpers (chat loop prevention)."""

from django.test import TestCase

from fighthealthinsurance.ml.response_similarity import (
    EXACT_REPEAT_MIN_CHARS,
    NEAR_REPEAT_MIN_CHARS,
    bag_of_words,
    is_mostly_repeated,
    jaccard_similarity,
    normalize_text,
    response_similarity,
    sequence_similarity,
)

# The reply from the observed production loop (assistant re-sent it verbatim
# after the user answered "CA").
LOOPED_REPLY = (
    "The new Medicaid requirements can be tricky! To help you understand them "
    "better, could you tell me a bit more about your situation? For example:\n\n"
    "* What state are you in?\n"
    "* What is your current income and household size?\n"
    "* Do you currently have Medicaid coverage?\n"
    "* What specific requirements are you trying to understand?\n\n"
    "Once I have this information, I can help you find the relevant resources "
    "and explain how to navigate the new requirements."
)


class TestNormalizeAndBags(TestCase):
    def test_normalize_text(self):
        self.assertEqual(normalize_text("  Hello   WORLD\n"), "hello world")

    def test_bag_of_words(self):
        self.assertEqual(bag_of_words("Hello, hello world!"), {"hello", "world"})

    def test_jaccard_identical(self):
        self.assertEqual(jaccard_similarity("a b c", "c b a"), 1.0)

    def test_jaccard_disjoint(self):
        self.assertEqual(jaccard_similarity("a b c", "d e f"), 0.0)

    def test_jaccard_empty(self):
        self.assertEqual(jaccard_similarity("", "a"), 0.0)


class TestSequenceSimilarity(TestCase):
    def test_identical_is_one(self):
        self.assertEqual(sequence_similarity(LOOPED_REPLY, LOOPED_REPLY), 1.0)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(
            sequence_similarity("Hello  World", "hello world"),
            1.0,
        )

    def test_different_texts_low(self):
        self.assertLess(
            sequence_similarity(
                "Great, you're in California! Medi-Cal is the state program.",
                "Please upload your denial letter so I can review it.",
            ),
            0.6,
        )

    def test_empty_returns_zero(self):
        self.assertEqual(sequence_similarity("", "anything"), 0.0)


class TestIsMostlyRepeated(TestCase):
    def test_verbatim_repeat_detected(self):
        self.assertTrue(is_mostly_repeated(LOOPED_REPLY, LOOPED_REPLY))

    def test_whitespace_variation_still_detected(self):
        self.assertTrue(
            is_mostly_repeated(LOOPED_REPLY.replace("\n", " "), LOOPED_REPLY)
        )

    def test_light_rewording_detected(self):
        # A few words changed in an otherwise identical long reply.
        reworded = LOOPED_REPLY.replace("can be tricky", "are quite tricky").replace(
            "a bit more", "some more"
        )
        self.assertTrue(is_mostly_repeated(reworded, LOOPED_REPLY))

    def test_genuinely_new_reply_not_detected(self):
        fresh = (
            "Great — since you're in California, the program is called "
            "Medi-Cal. The new federal rules add an 80-hour monthly work or "
            "volunteering requirement for many adults starting at the end of "
            "2026. Would you like me to check what applies to your situation?"
        )
        self.assertFalse(is_mostly_repeated(fresh, LOOPED_REPLY))

    def test_short_exact_confirmation_not_detected(self):
        # Below EXACT_REPEAT_MIN_CHARS: legitimate short repeats stay allowed.
        short = "Yes."
        self.assertLess(len(short), EXACT_REPEAT_MIN_CHARS)
        self.assertFalse(is_mostly_repeated(short, short))

    def test_short_exact_question_detected(self):
        # At/above EXACT_REPEAT_MIN_CHARS an exact repeat counts even when
        # short — re-sending the same question verbatim is still a loop.
        question = "Which state are you in?"
        self.assertGreaterEqual(len(question), EXACT_REPEAT_MIN_CHARS)
        self.assertTrue(is_mostly_repeated(question, question))

    def test_short_similar_but_not_exact_not_detected(self):
        # Below NEAR_REPEAT_MIN_CHARS the similarity path must not fire, so a
        # reworded short question is not treated as a repeat.
        a = "So which state are you living in"
        b = "Which state do you live in?"
        self.assertLess(len(a), NEAR_REPEAT_MIN_CHARS)
        self.assertFalse(is_mostly_repeated(a, b))

    def test_none_and_empty_inputs(self):
        self.assertFalse(is_mostly_repeated(None, LOOPED_REPLY))
        self.assertFalse(is_mostly_repeated(LOOPED_REPLY, None))
        self.assertFalse(is_mostly_repeated("", ""))

    def test_reordered_long_text_detected_via_jaccard(self):
        # Same content with paragraphs shuffled: word set is ~identical.
        paragraphs = LOOPED_REPLY.split("\n\n")
        reordered = "\n\n".join(reversed(paragraphs))
        self.assertTrue(is_mostly_repeated(reordered, LOOPED_REPLY))


class TestResponseSimilarity(TestCase):
    def test_identical(self):
        self.assertEqual(response_similarity(LOOPED_REPLY, LOOPED_REPLY), 1.0)

    def test_none_inputs(self):
        self.assertEqual(response_similarity(None, "x"), 0.0)
        self.assertEqual(response_similarity("x", None), 0.0)

    def test_distinct_answers_below_alternate_threshold(self):
        a = "You can appeal within 180 days; start by requesting your denial letter."
        b = (
            "Medi-Cal has an eligibility phone line; I can also look up "
            "the county office for you if you'd like."
        )
        self.assertLess(response_similarity(a, b), 0.7)
