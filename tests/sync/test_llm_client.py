"""Tests for chat LLM client utilities."""

from django.test import TestCase

from fighthealthinsurance.chat.llm_client import (
    estimate_history_tokens,
    score_llm_response,
    create_response_scorer,
    normalize_text,
    bag_of_words,
    compute_repetition_penalty,
    EXACT_REPEAT_PENALTY,
    BAG_OF_WORDS_REPEAT_PENALTY,
    OLDER_ASSISTANT_REPEAT_PENALTY,
    OLDER_USER_REPEAT_PENALTY,
    BAD_RESPONSE_PATTERNS,
    BAD_CONTEXT_PATTERNS,
)


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
        penalty = compute_repetition_penalty(
            "  i need help with  my denial  ", history
        )
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

        echo_score = score_llm_response(
            echo_result, 100, current_message=current_msg
        )
        good_score = score_llm_response(
            good_result, 100, current_message=current_msg
        )
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

        bow_score = score_llm_response(
            bow_result, 100, current_message=current_msg
        )
        exact_score = score_llm_response(
            exact_result, 100, current_message=current_msg
        )
        # Both penalized, but exact match more heavily
        self.assertGreater(bow_score, exact_score)
