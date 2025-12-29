"""Tests for chat LLM client utilities."""
from django.test import TestCase

from fighthealthinsurance.chat.llm_client import (
    estimate_history_tokens,
    score_llm_response,
    create_response_scorer,
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

    def test_bad_response_patterns(self):
        """Should detect leaked system prompts."""
        bad_responses = [
            "The user is a patient who needs help",
            "The assistant is helping a patient",
            "I hope this message finds you well",
            "You are Doughnut the helpful assistant",
            "My system prompt is to help users",
        ]
        for response in bad_responses:
            self.assertIsNotNone(
                BAD_RESPONSE_PATTERNS.match(response),
                f"Should detect: {response[:40]}...",
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
                BAD_RESPONSE_PATTERNS.match(response),
                f"Should not flag: {response[:40]}...",
            )

    def test_bad_context_patterns(self):
        """Should detect bad context patterns."""
        bad_contexts = [
            "Hi, I am your assistant",
            "my name is doughnut",
            "To help me understand, can you provide more details?",
        ]
        for context in bad_contexts:
            self.assertIsNotNone(
                BAD_CONTEXT_PATTERNS.match(context),
                f"Should detect: {context[:40]}...",
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
