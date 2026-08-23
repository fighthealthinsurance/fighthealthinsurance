"""Tests for chat LLM client utilities."""

from django.test import TestCase

from fighthealthinsurance.chat.llm_client import (
    estimate_history_tokens,
    score_llm_response,
    create_response_scorer,
    normalize_text,
    bag_of_words,
    compute_repetition_penalty,
    alternate_is_presentable,
    find_repeated_reply,
    scores_closely_tied,
    user_requested_repeat,
    ALTERNATE_CLOSE_TIE_RATIO,
    EXACT_REPEAT_PENALTY,
    BAG_OF_WORDS_REPEAT_PENALTY,
    NEAR_REPEAT_PENALTY,
    OLDER_ASSISTANT_REPEAT_PENALTY,
    OLDER_USER_REPEAT_PENALTY,
    REPEAT_OFFENDER_DECAY,
    BAD_RESPONSE_PATTERNS,
    BAD_CONTEXT_PATTERNS,
    INVENTED_ELIGIBILITY_VERDICT_PENALTY,
)
from tests.chat_fixtures import CANNED_MEDICAID_REPLY, FRESH_REPLY, LOOPED_REPLY


class TestEstimateHistoryTokens(TestCase):
    """Test token estimation for message history."""

    def test_empty_history(self):
        """Empty history should return 0."""
        self.assertEqual(estimate_history_tokens([]), 0)

    def test_single_message(self):
        """Single message should estimate correctly."""
        history = [{"content": "Hello world!"}]  # 12 chars = ~3 tokens
        self.assertEqual(estimate_history_tokens(history), 3)

    def test_multiple_messages(self):
        """Multiple messages should sum token counts."""
        history = [
            {"content": "Hello world!"},  # 12 chars
            {"content": "How are you?"},  # 12 chars
        ]  # 24 chars = 6 tokens
        self.assertEqual(estimate_history_tokens(history), 6)

    def test_missing_content_key(self):
        """Messages without content should be handled."""
        history = [{"role": "user"}]
        self.assertEqual(estimate_history_tokens(history), 0)


class TestBadPatterns(TestCase):
    """Test pattern detection for bad responses."""

    def test_bad_response_patterns_at_start(self):
        """Should detect leaked system prompts at start of text."""
        bad_responses = [
            "The user is a patient who needs help",
            "The assistant is helping a patient",
            "I hope this message finds you well",
            "You are Doughnut the helpful assistant",
            "My system prompt is to help users",
        ]
        for response in bad_responses:
            self.assertIsNotNone(
                BAD_RESPONSE_PATTERNS.search(response),
                f"Should detect: {response[:40]}...",
            )

    def test_bad_response_patterns_mid_text(self):
        """Should detect leaked system prompts anywhere in text (not just start)."""
        bad_responses = [
            "Let me help you. The user is a patient who needs help with their appeal.",
            "Sure! The assistant is helping a patient with their case.",
            "Here's my response. I hope this message finds you well after that.",
            "Context: You are Doughnut the helpful assistant. Now...",
        ]
        for response in bad_responses:
            self.assertIsNotNone(
                BAD_RESPONSE_PATTERNS.search(response),
                f"Should detect mid-text: {response[:50]}...",
            )

    def test_good_responses_not_flagged(self):
        """Normal responses should not be flagged."""
        good_responses = [
            "I can help you with your appeal.",
            "Your insurance denial seems to be about coverage.",
            "Let me research this for you.",
        ]
        for response in good_responses:
            self.assertIsNone(
                BAD_RESPONSE_PATTERNS.search(response),
                f"Should not flag: {response[:40]}...",
            )

    def test_bad_context_patterns_at_start(self):
        """Should detect bad context patterns at start of text."""
        bad_contexts = [
            "Hi, I am your assistant",
            "my name is doughnut",
            "To help me understand, can you provide more details?",
        ]
        for context in bad_contexts:
            self.assertIsNotNone(
                BAD_CONTEXT_PATTERNS.search(context),
                f"Should detect: {context[:40]}...",
            )

    def test_bad_context_patterns_mid_text(self):
        """Should detect bad context patterns anywhere in text (non-anchored patterns)."""
        # Note: ^Hi, pattern is intentionally anchored to start, so only test non-anchored patterns
        bad_contexts = [
            "User context: my name is doughnut and I need help",
            "Previous chat: To help me understand, can you provide more details?",
        ]
        for context in bad_contexts:
            self.assertIsNotNone(
                BAD_CONTEXT_PATTERNS.search(context),
                f"Should detect mid-text: {context[:50]}...",
            )


class TestScoreLlmResponse(TestCase):
    """Test LLM response scoring."""

    def test_tool_call_keeps_the_full_model_prior(self):
        """A bare tool call is a finished reply, not a truncated one.

        The model prior is normally divided by 100 when a candidate has no
        panda context summary, on the theory that it's half-generated. A tool
        call never has one -- the tool's follow-up pass writes it -- so that
        division sank real tool calls under chatty candidates that skipped
        the tool and invented an answer instead.
        """
        tool_call = ('**medicaid_eligibility {"state": "CA"}**', None)
        prose_without_context = ("Here is a fairly long chatty answer.", None)

        tool_score = score_llm_response(tool_call, 8000)
        prose_score = score_llm_response(prose_without_context, 8000)

        self.assertGreater(tool_score, prose_score)
        self.assertGreater(tool_score, 8000)

    def test_tool_call_outranks_an_answer_that_skipped_the_tool(self):
        # The observed failure: a local model's invented eligibility verdict
        # (with a context summary) beat the tool call that would have computed
        # one, so the checker never ran.
        tool_call = ('**medicaid_eligibility {"state": "CA", "age": 39}**', None)
        invented_verdict = (
            "Based on what you've told me you're likely eligible under today's rules.",
            "Medicaid eligibility for California",
        )

        self.assertGreater(
            score_llm_response(tool_call, 8000),
            score_llm_response(invented_verdict, 8000),
        )

    def test_invented_eligibility_verdict_is_penalized(self):
        # Only the medicaid_eligibility checker can produce a verdict. A model
        # asserting one from the conversation alone is guessing about
        # someone's health coverage.
        invented = (
            "Good news — based on your income you are eligible for Medicaid.",
            "Medicaid eligibility for California",
        )
        neutral = (
            "Medicaid income limits vary by state and household size.",
            "Medicaid eligibility for California",
        )

        self.assertEqual(
            score_llm_response(neutral, 8000) - score_llm_response(invented, 8000),
            8000 + INVENTED_ELIGIBILITY_VERDICT_PENALTY,
        )

    def test_invented_verdict_loses_to_a_higher_prior_tool_call(self):
        # The failure a fixed nudge could not stop: ONE quality-210 backend
        # contributes a truncated-history call (210**2//5 = 8820) and a
        # full-history one (210**2//4 = 11025). A 2000-point penalty left the
        # full-history invented verdict beating the truncated call's real
        # checker invocation, so the prior is forfeited instead.
        tool_call = ('**medicaid_eligibility {"state": "CA"}**', None)
        invented = (
            "Based on your income you do not qualify for Medicaid in California.",
            "Medicaid eligibility for California",
        )

        self.assertGreater(
            score_llm_response(tool_call, 210**2 // 5),
            score_llm_response(invented, 210**2 // 4),
        )

    def test_invented_verdict_forfeits_the_model_prior(self):
        invented = (
            "Based on your income you do not qualify for Medicaid.",
            "Medicaid eligibility",
        )

        low_prior = score_llm_response(invented, 2000)
        high_prior = score_llm_response(invented, 11000)

        # A better backend buys an invented verdict nothing at all.
        self.assertEqual(low_prior, high_prior)

    def test_invented_verdict_without_context_is_not_double_penalized(self):
        # A candidate with no context summary and no tool call only receives
        # call_score/100, so taking back the full call_score drove it
        # thousands of points below its peers and ranked invented verdicts by
        # model quality -- the inverse of the intended levelling.
        no_context = ("Based on your income you do not qualify for Medicaid.", None)

        strong = score_llm_response(no_context, 11000)
        weak = score_llm_response(no_context, 2000)

        self.assertEqual(strong, weak)

    def test_invented_verdict_is_not_hard_rejected(self):
        # Deliberately not -inf: the detector is a regex over free text, and
        # one false positive must not be able to empty a fan-out.
        invented = (
            "Based on your income you do not qualify for Medicaid.",
            "Medicaid eligibility",
        )

        self.assertNotEqual(score_llm_response(invented, 8000), float("-inf"))

    def test_verdict_is_not_penalized_once_the_checker_has_run(self):
        # After the tool computes a determination, relaying it is the point.
        verdict = (
            "Good news — based on your income you are eligible for Medicaid.",
            "Medicaid eligibility for California",
        )

        self.assertGreater(
            score_llm_response(verdict, 8000, eligibility_verified=True),
            score_llm_response(verdict, 8000),
        )

    def test_offering_the_check_is_not_a_verdict(self):
        # The Medicaid path asks the model to say exactly this before the
        # checker has run, so penalizing it would fight our own prompt.
        offer = (
            "You may be eligible for Medicaid — want me to run our "
            "experimental eligibility check with you?",
            "Medicaid question",
        )

        self.assertEqual(
            score_llm_response(offer, 8000),
            score_llm_response(offer, 8000, eligibility_verified=True),
        )

    def test_invented_verdict_loses_to_the_tool_call_that_would_compute_it(self):
        tool_call = ('**medicaid_eligibility {"state": "CA", "age": 39}**', None)
        invented = (
            "Based on your income you do not qualify for Medicaid in California.",
            "Medicaid eligibility for California",
        )

        self.assertGreater(
            score_llm_response(tool_call, 8000),
            score_llm_response(invented, 8000),
        )

    def test_missing_context_still_penalized_without_a_tool_call(self):
        with_context = ("A helpful answer for the user.", "Context summary")
        without_context = ("A helpful answer for the user.", None)

        self.assertGreater(
            score_llm_response(with_context, 8000),
            score_llm_response(without_context, 8000),
        )

    def test_none_result_returns_negative_inf(self):
        """None result should return -inf."""
        score = score_llm_response(None, 100)
        self.assertEqual(score, float("-inf"))

    def test_empty_response_returns_negative_inf(self):
        """Empty response and context should return -inf."""
        score = score_llm_response((None, None), 100)
        self.assertEqual(score, float("-inf"))

        score = score_llm_response(("", ""), 100)
        self.assertEqual(score, float("-inf"))

    def test_valid_response_gets_base_score(self):
        """Valid response should get positive score."""
        result = ("This is a helpful response.", "Context summary")
        score = score_llm_response(result, 100)
        self.assertGreater(score, 0)

    def test_primary_call_bonus(self):
        """Primary calls should get bonus score."""
        result = ("This is a helpful response.", "Context summary")
        primary_score = score_llm_response(result, 100, is_primary_call=True)
        retry_score = score_llm_response(result, 100, is_primary_call=False)
        self.assertGreater(primary_score, retry_score)

    def test_false_promise_penalty(self):
        """Responses with false promises should be penalized."""
        good_result = ("I can help you understand your options.", "Context")
        # False promises use phrases like "will definitely" or "guaranteed"
        bad_result = ("Your appeal will definitely succeed.", "Context")

        good_score = score_llm_response(good_result, 100)
        bad_score = score_llm_response(bad_result, 100)

        # False promise detection may reduce score significantly
        # Exact behavior depends on detect_false_promises implementation
        self.assertIsInstance(good_score, float)
        self.assertIsInstance(bad_score, float)


class TestCreateResponseScorer(TestCase):
    """Test response scorer factory function."""

    def test_creates_callable(self):
        """Should create a callable scoring function."""
        call_scores = {}
        scorer = create_response_scorer(call_scores)
        self.assertTrue(callable(scorer))

    def test_scorer_uses_call_scores(self):
        """Scorer should use provided call scores."""

        async def fake_call():
            pass

        call_scores = {fake_call: 50}
        scorer = create_response_scorer(call_scores)

        result = ("Response text", "Context")
        score = scorer(result, fake_call)
        self.assertIsInstance(score, float)

    def test_primary_calls_get_bonus(self):
        """Primary calls should get bonus in scorer."""

        async def primary_call():
            pass

        async def retry_call():
            pass

        call_scores = {primary_call: 50, retry_call: 50}
        scorer = create_response_scorer(call_scores, primary_calls=[primary_call])

        result = ("Response text", "Context")
        primary_score = scorer(result, primary_call)
        retry_score = scorer(result, retry_call)

        self.assertGreater(primary_score, retry_score)


class TestNormalizeText(TestCase):
    """Test text normalization for comparison."""

    def test_lowercase_and_strip(self):
        self.assertEqual(normalize_text("  Hello WORLD  "), "hello world")

    def test_collapse_whitespace(self):
        self.assertEqual(normalize_text("hello   world\n\tfoo"), "hello world foo")

    def test_empty_string(self):
        self.assertEqual(normalize_text(""), "")


class TestBagOfWords(TestCase):
    """Test bag-of-words extraction."""

    def test_basic_extraction(self):
        self.assertEqual(bag_of_words("Hello World Hello"), {"hello", "world"})

    def test_ignores_punctuation(self):
        self.assertEqual(bag_of_words("Hello, World!"), {"hello", "world"})

    def test_empty_string(self):
        self.assertEqual(bag_of_words(""), set())


class TestComputeRepetitionPenalty(TestCase):
    """Test repetition penalty computation."""

    def test_exact_match_user_message(self):
        """Exact match (ignoring case/spacing) with last user message => -500."""
        history = [{"role": "user", "content": "I need help with my denial"}]
        penalty = compute_repetition_penalty("  i need help with  my denial  ", history)
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_bag_of_words_match_user_message(self):
        """Same words, different order => -75."""
        history = [{"role": "user", "content": "help with my denial"}]
        penalty = compute_repetition_penalty("my denial with help", history)
        self.assertEqual(penalty, BAG_OF_WORDS_REPEAT_PENALTY)

    def test_exact_match_assistant_message(self):
        """Exact match with last assistant message => -500."""
        history = [
            {"role": "user", "content": "something different"},
            {"role": "assistant", "content": "Here is my response"},
        ]
        penalty = compute_repetition_penalty("here is my response", history)
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_no_match_no_penalty(self):
        """Completely different response => 0."""
        history = [{"role": "user", "content": "I need help with my denial"}]
        penalty = compute_repetition_penalty(
            "Let me look into your insurance case.", history
        )
        self.assertEqual(penalty, 0.0)

    def test_empty_history_no_penalty(self):
        """Empty chat_history => 0."""
        self.assertEqual(compute_repetition_penalty("some response", []), 0.0)

    def test_empty_response_no_penalty(self):
        """Empty response text => 0."""
        history = [{"role": "user", "content": "hello"}]
        self.assertEqual(compute_repetition_penalty("", history), 0.0)

    def test_partial_overlap_no_penalty(self):
        """Some shared words but not all => 0 (bag-of-words must be equal)."""
        history = [{"role": "user", "content": "I need help with my denial"}]
        penalty = compute_repetition_penalty("I need help with something else", history)
        self.assertEqual(penalty, 0.0)

    def test_case_insensitive_exact_match(self):
        """Case differences should still trigger exact match."""
        history = [{"role": "user", "content": "HELLO WORLD"}]
        penalty = compute_repetition_penalty("hello world", history)
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_whitespace_insensitive_exact_match(self):
        """Whitespace differences should still trigger exact match."""
        history = [{"role": "user", "content": "hello  world"}]
        penalty = compute_repetition_penalty("hello world", history)
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_older_assistant_message_match(self):
        """Matching an older assistant message => -20."""
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]
        # Response matches "first answer" (older assistant msg, not the last one)
        penalty = compute_repetition_penalty("first answer", history)
        self.assertEqual(penalty, OLDER_ASSISTANT_REPEAT_PENALTY)

    def test_older_user_message_match(self):
        """Matching an older user message => -10."""
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ]
        # Response matches "first question" (older user msg, not the last one)
        penalty = compute_repetition_penalty("first question", history)
        self.assertEqual(penalty, OLDER_USER_REPEAT_PENALTY)

    def test_current_message_exact_match(self):
        """Parroting the current user message (not yet in history) => -500."""
        penalty = compute_repetition_penalty(
            "I need help with my denial",
            [],
            current_message="I need help with my denial",
        )
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_current_message_bag_of_words_match(self):
        """Same words as current message in different order => -75."""
        penalty = compute_repetition_penalty(
            "my denial with help",
            [],
            current_message="help with my denial",
        )
        self.assertEqual(penalty, BAG_OF_WORDS_REPEAT_PENALTY)

    def test_current_message_no_match(self):
        """Different response from current message => 0."""
        penalty = compute_repetition_penalty(
            "Let me look into that for you.",
            [],
            current_message="I need help with my denial",
        )
        self.assertEqual(penalty, 0.0)

    def test_current_message_case_and_whitespace_insensitive(self):
        """Current message comparison ignores case and whitespace."""
        penalty = compute_repetition_penalty(
            "  HELLO   WORLD  ",
            [],
            current_message="hello world",
        )
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)

    def test_first_turn_echo_penalized(self):
        """On first turn (empty history), parroting the prompt is still caught."""
        penalty = compute_repetition_penalty(
            "Tell me about my insurance denial",
            [],
            current_message="Tell me about my insurance denial",
        )
        self.assertEqual(penalty, EXACT_REPEAT_PENALTY)


class TestUserRequestedRepeat(TestCase):
    """The explicit-repeat guard for hard rejection."""

    def test_detects_repeat_requests(self):
        for msg in [
            "can you repeat that?",
            "please say that again",
            "one more time please",
            "resend it",
            "show that again",
        ]:
            self.assertTrue(user_requested_repeat(msg), msg)

    def test_normal_messages_not_flagged(self):
        for msg in ["CA", "I'm in California", "what about my denial?", None, ""]:
            self.assertFalse(user_requested_repeat(msg), repr(msg))


class TestFindRepeatedReply(TestCase):
    """Detection of candidate replies that repeat the recent conversation."""

    def _history(self):
        return [
            {"role": "user", "content": "Help me with the new medicaid requirements."},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]

    def test_verbatim_repeat_of_last_assistant_reply(self):
        self.assertEqual(
            find_repeated_reply(LOOPED_REPLY, self._history(), "CA"),
            "repeats_recent_assistant_reply",
        )

    def test_alternating_loop_detected(self):
        # A-B-A loop: the repeat matches the assistant reply two turns back.
        history = self._history() + [
            {"role": "user", "content": "CA"},
            {"role": "assistant", "content": FRESH_REPLY},
            {"role": "user", "content": "yes please"},
        ]
        self.assertEqual(
            find_repeated_reply(LOOPED_REPLY, history, "yes please"),
            "repeats_recent_assistant_reply",
        )

    def test_echo_of_current_message(self):
        msg = "Help me figure out how to navigate the new medicaid requirements."
        self.assertEqual(
            find_repeated_reply(msg, [], msg),
            "echoes_user_message",
        )

    def test_fresh_reply_passes(self):
        self.assertIsNone(find_repeated_reply(FRESH_REPLY, self._history(), "CA"))

    def test_empty_inputs(self):
        self.assertIsNone(find_repeated_reply("", self._history(), "CA"))
        self.assertIsNone(find_repeated_reply(FRESH_REPLY, None, None))


class TestHardRepeatRejection(TestCase):
    """score_llm_response must hard-reject (-inf) near-verbatim repeats.

    Soft penalties are not enough: model base scores reach ~8800 via
    quality**2 scaling, so -500 still let the repeated reply win — the
    observed production chat loop.
    """

    def _history(self):
        return [
            {"role": "user", "content": "Help me with the new medicaid requirements."},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]

    def test_repeat_of_last_assistant_reply_rejected(self):
        score = score_llm_response(
            (LOOPED_REPLY, "Context summary"),
            call_score=8800,
            chat_history=self._history(),
            current_message="CA",
        )
        self.assertEqual(score, float("-inf"))

    def test_lightly_reworded_repeat_rejected(self):
        reworded = LOOPED_REPLY.replace("can be tricky", "are quite tricky")
        score = score_llm_response(
            (reworded, "Context summary"),
            call_score=8800,
            chat_history=self._history(),
            current_message="I'm in California",
        )
        self.assertEqual(score, float("-inf"))

    def test_fresh_reply_scores_normally(self):
        score = score_llm_response(
            (FRESH_REPLY, "Context summary"),
            call_score=100,
            chat_history=self._history(),
            current_message="CA",
        )
        self.assertGreater(score, 0)

    def test_user_requested_repeat_not_rejected(self):
        score = score_llm_response(
            (LOOPED_REPLY, "Context summary"),
            call_score=100,
            chat_history=self._history(),
            current_message="sorry, can you repeat that?",
        )
        self.assertGreater(score, float("-inf"))

    def test_rejection_stats_incremented(self):
        stats: dict = {}
        score_llm_response(
            (LOOPED_REPLY, "Context summary"),
            call_score=100,
            chat_history=self._history(),
            current_message="CA",
            rejection_stats=stats,
        )
        self.assertEqual(stats.get("repeated_rejected"), 1)

    def test_rejection_stats_untouched_for_fresh_reply(self):
        stats: dict = {}
        score_llm_response(
            (FRESH_REPLY, "Context summary"),
            call_score=100,
            chat_history=self._history(),
            current_message="CA",
            rejection_stats=stats,
        )
        self.assertNotIn("repeated_rejected", stats)


class TestAlternateIsPresentable(TestCase):
    """Filtering of runner-up answers for side-by-side display."""

    PRIMARY = FRESH_REPLY

    def test_distinct_clean_answer_is_presentable(self):
        alternate = (
            "California's Medicaid program is called Medi-Cal. You can call "
            "their member help line or I can walk you through the new work "
            "requirement rules — which would you prefer?"
        )
        self.assertTrue(alternate_is_presentable(alternate, self.PRIMARY))

    def test_near_duplicate_rejected(self):
        near_dup = self.PRIMARY.replace("Great —", "Good news:")
        self.assertFalse(alternate_is_presentable(near_dup, self.PRIMARY))

    def test_tool_call_rejected(self):
        alternate = (
            'Let me look that up for you. **medicaid_info {"state": '
            '"California", "topic": "", "limit": 5}**'
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_action_token_rejected(self):
        alternate = (
            "Here you go. **create_or_update_appeal**"
            '{"patient_name": "X", "appeal_text": "..."} and more text to '
            "reach the minimum length for an alternate answer."
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_panda_residue_rejected(self):
        alternate = (
            "Sure, here's another way to think about your appeal options "
            "🐼 user is in California asking about Medicaid"
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_too_short_rejected(self):
        self.assertFalse(alternate_is_presentable("Sure!", self.PRIMARY))
        self.assertFalse(alternate_is_presentable(None, self.PRIMARY))
        self.assertFalse(alternate_is_presentable("", self.PRIMARY))

    def test_repeat_of_history_rejected(self):
        history = [
            {"role": "user", "content": "help"},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        self.assertFalse(
            alternate_is_presentable(
                LOOPED_REPLY, self.PRIMARY, chat_history=history, current_message="CA"
            )
        )


class TestScoreLlmResponseRepetitionPenalty(TestCase):
    """Integration: repetition penalty within score_llm_response."""

    def test_echoing_current_message_heavily_penalized(self):
        """Response echoing the current user message should score much lower.

        In production, the current message is not yet in chat_history when
        scoring runs, so current_message must be passed separately.
        """
        current_msg = "I need help with my denial"
        echo_result = ("I need help with my denial", "Context")
        good_result = ("I can help you appeal that. Let me look into it.", "Context")

        echo_score = score_llm_response(echo_result, 100, current_message=current_msg)
        good_score = score_llm_response(good_result, 100, current_message=current_msg)
        self.assertGreater(good_score, echo_score)
        self.assertLess(echo_score, 0)

    def test_echoing_history_message_penalized(self):
        """Response echoing a message from chat history should be penalized."""
        history = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "Here is my response"},
        ]
        echo_result = ("Here is my response", "Context")
        good_result = ("Let me provide updated information.", "Context")

        echo_score = score_llm_response(echo_result, 100, chat_history=history)
        good_score = score_llm_response(good_result, 100, chat_history=history)
        self.assertGreater(good_score, echo_score)

    def test_bag_of_words_repeat_mildly_penalized(self):
        """Same words rearranged should be penalized but less than exact match."""
        current_msg = "help with my denial please"
        bow_result = ("my denial please help with", "Context")
        exact_result = ("help with my denial please", "Context")

        bow_score = score_llm_response(bow_result, 100, current_message=current_msg)
        exact_score = score_llm_response(exact_result, 100, current_message=current_msg)
        # Both penalized, but exact match more heavily
        self.assertGreater(bow_score, exact_score)


class TestCannedReplyExemption(TestCase):
    """Mandated verbatim replies (e.g. the Medicaid work-requirements block)
    may legitimately repeat across turns — they are exempt from HARD
    rejection while soft penalties still apply."""

    def test_canned_reply_repeat_not_hard_rejected(self):
        history = [
            {"role": "user", "content": "tell me about the work requirements"},
            {"role": "assistant", "content": CANNED_MEDICAID_REPLY},
        ]
        self.assertIsNone(
            find_repeated_reply(
                CANNED_MEDICAID_REPLY, history, "and the work requirements?"
            )
        )
        score = score_llm_response(
            (CANNED_MEDICAID_REPLY, "ctx"),
            call_score=100,
            chat_history=history,
            current_message="what about the work requirements again?",
        )
        self.assertGreater(score, float("-inf"))

    def test_non_canned_repeat_still_rejected(self):
        history = [
            {"role": "user", "content": "help"},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        self.assertEqual(
            find_repeated_reply(LOOPED_REPLY, history, "CA"),
            "repeats_recent_assistant_reply",
        )


class TestPenaltySeverityOrdering(TestCase):
    """Near-verbatim detection outranks bag-of-words equality: a LONG reply
    sharing the exact word set is a near-verbatim reorder and must get
    NEAR_REPEAT_PENALTY, while short reorders still land in the mild
    bag-of-words bucket."""

    def test_long_reordered_repeat_gets_near_penalty(self):
        paragraphs = LOOPED_REPLY.split("\n\n")
        reordered = "\n\n".join(reversed(paragraphs))
        history = [
            {"role": "user", "content": "something distinct entirely"},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        penalty = compute_repetition_penalty(reordered, history)
        self.assertEqual(penalty, NEAR_REPEAT_PENALTY)

    def test_short_reorder_still_bag_of_words(self):
        history = [{"role": "user", "content": "help with my denial"}]
        penalty = compute_repetition_penalty("my denial with help", history)
        self.assertEqual(penalty, BAG_OF_WORDS_REPEAT_PENALTY)


class TestScoresCloselyTied(TestCase):
    """Alternates only show when the fan-out's top two scores are close."""

    def test_within_ratio_is_tied(self):
        self.assertTrue(scores_closely_tied(100.0, 100.0 * ALTERNATE_CLOSE_TIE_RATIO))
        self.assertTrue(scores_closely_tied(2630.0, 2210.0))

    def test_below_ratio_not_tied(self):
        self.assertFalse(
            scores_closely_tied(100.0, 100.0 * ALTERNATE_CLOSE_TIE_RATIO - 0.1)
        )
        # Cross-tier gap (internal ~8000 base vs external ~2000): never tied.
        self.assertFalse(scores_closely_tied(8200.0, 2100.0))

    def test_non_positive_scores_never_tied(self):
        self.assertFalse(scores_closely_tied(0.0, 0.0))
        self.assertFalse(scores_closely_tied(-50.0, -40.0))
        self.assertFalse(scores_closely_tied(100.0, 0.0))

    def test_non_finite_scores_never_tied(self):
        self.assertFalse(scores_closely_tied(float("inf"), 100.0))
        self.assertFalse(scores_closely_tied(100.0, float("-inf")))


class TestRepeatOffenderDemotion(TestCase):
    """Backends that produced hard-rejected repeats earlier in the session
    have their base score decayed, and each rejection records a strike."""

    CONTEXT = "context for the reply"

    def _score(self, offenders, base=1000):
        task = object()
        score_fn = create_response_scorer(
            call_scores={task: base},
            chat_history=None,
            current_message=None,
            call_labels={task: "backend-a"},
            repeat_offenders=offenders,
        )
        return score_fn((FRESH_REPLY, self.CONTEXT), task)

    def test_strikes_decay_base_score(self):
        clean = self._score({})
        struck = self._score({"backend-a": 2})
        expected_decayed_base = int(1000 * (REPEAT_OFFENDER_DECAY**2))
        self.assertEqual(clean - struck, 1000 - expected_decayed_base)

    def test_strike_count_capped(self):
        at_cap = self._score({"backend-a": 4})
        past_cap = self._score({"backend-a": 40})
        self.assertEqual(at_cap, past_cap)

    def test_rejected_repeat_records_strike(self):
        task = object()
        offenders: dict = {}
        rejection_stats: dict = {}
        history = [
            {"role": "user", "content": "Help me with the new requirements."},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        score_fn = create_response_scorer(
            call_scores={task: 1000},
            chat_history=history,
            current_message="CA",
            rejection_stats=rejection_stats,
            call_labels={task: "backend-a"},
            repeat_offenders=offenders,
        )
        score = score_fn((LOOPED_REPLY, self.CONTEXT), task)
        self.assertEqual(score, float("-inf"))
        self.assertEqual(offenders, {"backend-a": 1})

    def test_score_log_records_final_scores(self):
        task = object()
        score_log: dict = {}
        score_fn = create_response_scorer(
            call_scores={task: 1000},
            chat_history=None,
            current_message=None,
            call_labels={task: "backend-a"},
            score_log=score_log,
        )
        score = score_fn((FRESH_REPLY, self.CONTEXT), task)
        self.assertEqual(score_log, {task: score})


class TestAlternateScreensRealToolCalls(TestCase):
    """The tool handlers detect with DOTALL|MULTILINE|IGNORECASE. A flag-less
    sweep over the `^...$`-anchored patterns only matched a tool call at the
    very start of a reply, so an alternate whose tool call followed a line of
    prose was shown to the user as raw syntax plus its JSON payload."""

    PRIMARY = "Here is a summary of your options for this denial."

    def test_appeal_tool_call_after_prose_is_rejected(self):
        alternate = (
            "I can draft that appeal for you.\n\n"
            'create_or_update_appeal {"patient_name": "Jane Doe", '
            '"appeal_text": "To whom it may concern..."}'
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_mixed_case_bold_tool_call_is_rejected(self):
        alternate = (
            "Sure, drafting now.\n\n"
            '**Create_Or_Update_Appeal** {"patient_name": "Jane Doe"}'
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_prior_auth_tool_call_after_prose_is_rejected(self):
        alternate = (
            "Let me start that prior auth.\n\n"
            'create_or_update_prior_auth {"procedure": "MRI"}'
        )
        self.assertFalse(alternate_is_presentable(alternate, self.PRIMARY))

    def test_plain_answer_still_presentable(self):
        alternate = (
            "Another angle: ask the plan for its clinical policy bulletin for "
            "this code, then cite the specific criteria you meet."
        )
        self.assertTrue(alternate_is_presentable(alternate, self.PRIMARY))


class TestRepeatOffenderStrikesArePerTurn(TestCase):
    """A backend contributes several candidates to one fan-out (the primary
    backend is listed twice, each gets a truncated- and full-history call,
    times message variants). Counting a strike per candidate ranked backends
    by how many slots they filled and saturated the cap inside one turn."""

    def test_one_looping_turn_costs_one_strike(self):
        history = [
            {"role": "user", "content": "Help me with the new requirements."},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        offenders: dict = {}
        tasks = [object() for _ in range(4)]
        score_fn = create_response_scorer(
            call_scores={t: 1000 for t in tasks},
            chat_history=history,
            current_message="CA",
            rejection_stats={},
            call_labels={t: "looping-backend" for t in tasks},
            repeat_offenders=offenders,
        )
        for task in tasks:
            self.assertEqual(score_fn((LOOPED_REPLY, "ctx"), task), float("-inf"))

        self.assertEqual(offenders, {"looping-backend": 1})

    def test_strikes_accumulate_across_turns(self):
        history = [
            {"role": "user", "content": "Help me with the new requirements."},
            {"role": "assistant", "content": LOOPED_REPLY},
        ]
        offenders: dict = {}
        for _ in range(3):  # three separate turns, each with a fresh scorer
            task = object()
            score_fn = create_response_scorer(
                call_scores={task: 1000},
                chat_history=history,
                current_message="CA",
                rejection_stats={},
                call_labels={task: "looping-backend"},
                repeat_offenders=offenders,
            )
            score_fn((LOOPED_REPLY, "ctx"), task)

        self.assertEqual(offenders, {"looping-backend": 3})
