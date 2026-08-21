"""Tests for the shared response-similarity helpers (chat loop prevention)."""

from django.test import TestCase

from fighthealthinsurance.ml.response_similarity import (
    EXACT_REPEAT_MIN_CHARS,
    NEAR_REPEAT_MIN_CHARS,
    bag_of_words,
    is_canned_reply,
    is_mostly_repeated,
    jaccard_similarity,
    normalize_text,
    response_similarity,
    sequence_similarity,
    user_requested_repeat,
)
from tests.chat_fixtures import CANNED_MEDICAID_REPLY, LOOPED_REPLY


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


class TestLongNearVerbatimRepeats(TestCase):
    """Regression: difflib's default autojunk heuristic treats every common
    character in a 200+ element sequence as junk, collapsing ratio() for
    exactly the long replies this module targets (0.33 measured where the
    true ratio was 0.96, against a 0.9 threshold)."""

    def test_reworded_long_reply_is_a_repeat(self):
        reworded = LOOPED_REPLY.replace("can be tricky!", "are confusing.").replace(
            "Once I have this information,", "Once I have that,"
        )
        self.assertTrue(is_mostly_repeated(reworded, LOOPED_REPLY))

    def test_long_similarity_scores_high(self):
        reworded = LOOPED_REPLY.replace("can be tricky!", "are confusing.")
        self.assertGreater(sequence_similarity(reworded, LOOPED_REPLY), 0.9)

    def test_unrelated_long_replies_are_not_repeats(self):
        unrelated = (
            "Your plan denied this as investigational, so the strongest "
            "argument is medical necessity backed by the trial registry. "
            "Start by requesting the full denial letter and the plan's "
            "clinical policy bulletin for this code. "
        ) * 3
        self.assertFalse(is_mostly_repeated(unrelated, LOOPED_REPLY * 3))


class TestUserRequestedRepeatPrecision(TestCase):
    """`repeat` is everyday clinical vocabulary here, and this flag disables
    every rung of the loop-prevention ladder — so it must match an explicit
    request to say something again, not the topic of the conversation."""

    CLINICAL_MENTIONS = (
        "They denied my repeat colonoscopy.",
        "This is a repeat denial for the same MRI.",
        "I need a repeat prescription for my insulin.",
        "my doctor ordered a repeat echocardiogram",
        "The insurer keeps denying repeat imaging",
        "Can you resend the fax to my doctor?",
        "I filed an appeal one more time last month",
    )

    EXPLICIT_REQUESTS = (
        "can you repeat that?",
        "repeat that please",
        "say that again",
        "please repeat the last answer",
        "send it again",
        "repeat",
        "one more time please",
        "resend it",
        "show that again",
    )

    def test_clinical_mentions_do_not_disable_the_ladder(self):
        for message in self.CLINICAL_MENTIONS:
            with self.subTest(message=message):
                self.assertFalse(user_requested_repeat(message))

    def test_explicit_requests_are_honored(self):
        for message in self.EXPLICIT_REQUESTS:
            with self.subTest(message=message):
                self.assertTrue(user_requested_repeat(message))


class TestCannedReplyRequiresTheWholeBlock(TestCase):
    """The system prompt tells the model to link the Medicaid FAQ on every
    work-requirements answer, so matching the link alone exempted ordinary
    (including looping) Medicaid replies from the whole ladder."""

    def test_full_canned_block_is_exempt(self):
        self.assertTrue(is_canned_reply(CANNED_MEDICAID_REPLY))

    def test_reply_merely_linking_the_faq_is_not_exempt(self):
        linking = (
            LOOPED_REPLY
            + "\n\nSee the [Medicaid Work Requirements FAQ](/faq/medicaid/)."
        )
        self.assertFalse(is_canned_reply(linking))
