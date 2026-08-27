"""Tests for the shared response-similarity helpers (chat loop prevention)."""

import inspect

from django.test import TestCase

from fighthealthinsurance.ml.response_similarity import (
    CANNED_REPLY_SIGNATURES,
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
    user_requested_transformation,
)
from fighthealthinsurance.ml.ml_models import MEDICAID_WORK_REQUIREMENTS_BLOCK
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
        # Verb + "again" with arbitrary words between is ordinary denial
        # vocabulary, not a repeat request -- these used to match and switch
        # the whole ladder off on exactly the turns most likely to be
        # mid-loop.
        "My state denied my Medicaid again, what can I do?",
        "I need to repeat the MRI but insurance denied it",
        "Please tell them again that this is urgent",
        # The polite prefix ("can you repeat ...") used to accept ANY object,
        # so the one shape this predicate exists to exclude slipped through
        # whenever the user was merely polite about it.
        "Can you repeat the MRI?",
        "Do I need to repeat the colonoscopy?",
        "could you repeat the bloodwork if the first one was inconclusive",
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
        "state that again",
        "tell me that again",
        "show me that one more time",
        # A bare polite ask, with the request ending there.
        "could you repeat?",
        # Named prior content, not just a pronoun: these are ordinary ways to
        # ask for our last answer back, and returning False on them let the
        # anti-repeat scorer steer the model away from the user's request.
        "send your last reply again",
        "show me the previous answer again",
        "state the answer once more",
        "repeat your answer",
        "repeat the previous response",
    )

    def test_clinical_mentions_do_not_disable_the_ladder(self):
        for message in self.CLINICAL_MENTIONS:
            with self.subTest(message=message):
                self.assertFalse(user_requested_repeat(message))

    def test_explicit_requests_are_honored(self):
        for message in self.EXPLICIT_REQUESTS:
            with self.subTest(message=message):
                self.assertTrue(user_requested_repeat(message))


class TestTransformRequestExemption(TestCase):
    """A faithful transformation (proofread / fix the grammar / reformat) of
    the user's paste or of our previous reply is SUPPOSED to read nearly the
    same as the source text, so the anti-repeat ladder stands down on
    transform requests -- but only on explicit ones, per the
    err-toward-not-matching rule."""

    TRANSFORM_REQUESTS = (
        "Can you fix the grammar of this: We was denied coverage for the MRI",
        "proofread this and correct any typos",
        "Please rewrite this paragraph so it sounds more professional",
        "reformat my appeal so I can send it",
        "check the spelling in the letter below",
        "copy-edit the following",
        # Iterating on our own previous reply.
        "Now reformat it into three paragraphs",
        "now rephrase it a bit more formally",
    )

    NON_TRANSFORM_MESSAGES = (
        "They denied the claim and won't fix the billing error",
        "My doctor said she would revise the prior auth request",
        "How do I improve my chances of approval?",
        "I want to check whether I might be eligible for Medicaid",
        "Can you help me appeal this denial?",
        # Questions ABOUT rewriting are not requests to rewrite. The bare
        # verbs used to match these and stand the whole ladder down on an
        # ordinary advice turn.
        "Should I reformat my appeal?",
        "Will my insurer rephrase the denial?",
        "Do they reformat denials before sending them?",
    )

    def test_transform_requests_are_recognized(self):
        for message in self.TRANSFORM_REQUESTS:
            with self.subTest(message=message):
                self.assertTrue(user_requested_transformation(message))

    def test_ordinary_messages_keep_the_ladder_armed(self):
        for message in self.NON_TRANSFORM_MESSAGES:
            with self.subTest(message=message):
                self.assertFalse(user_requested_transformation(message))


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


class TestCannedSignatureTracksTheActualPrompt(TestCase):
    """The signature must recognize the block the system prompt mandates.

    Regression: the third marker used to be the literal date "December 31,
    2026". When the prompt's block was corrected to the statutory January 1,
    2027 deadline the signature silently stopped matching, so a second
    work-requirements reply was hard-rejected as a repeat and the self-heal
    retry steered the model away from text the prompt requires verbatim.
    Asserting against the live prompt keeps the two from drifting again.
    """

    def test_prompt_block_is_recognized_as_canned(self):
        self.assertTrue(is_canned_reply(MEDICAID_WORK_REQUIREMENTS_BLOCK))

    def test_prompt_is_built_from_the_named_block(self):
        # The constant is only a useful anchor while the prompt is actually
        # built from it; re-inlining the text would leave the test above
        # passing against a string nothing sends.
        #
        # Match the identifier, not the concatenation syntax: a Black reflow
        # or a switch to an f-string placeholder keeps the prompt correct,
        # and this test should not fail for either. Scoped to the
        # prompt-building method so an unrelated reference elsewhere in the
        # module can't satisfy it.
        from fighthealthinsurance.ml import ml_models

        source = inspect.getsource(ml_models.RemoteModelLike.generate_chat_response)
        self.assertIn("MEDICAID_WORK_REQUIREMENTS_BLOCK", source)

    def test_signature_carries_no_date_marker(self):
        # Dates rot; the phrases describing the requirement do not.
        for signature in CANNED_REPLY_SIGNATURES:
            for marker in signature:
                self.assertNotRegex(
                    marker,
                    r"\b(19|20)\d{2}\b",
                    f"{marker!r} pins the signature to a date that will change",
                )
