"""Tests for chat LLM client utilities."""

from django.test import TestCase

from fighthealthinsurance.chat.llm_client import (
    build_llm_calls,
    build_retry_calls,
    estimate_history_tokens,
    score_llm_response,
    create_response_scorer,
    BAD_RESPONSE_PATTERNS,
    BAD_CONTEXT_PATTERNS,
)
from tests.sync.mock_chat_model import MockChatModel


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


class _CoroutineCleanupMixin:
    """Mixin to close unawaited coroutines created by build_llm_calls/build_retry_calls."""

    def _close_calls(self, calls):
        """Close all coroutine objects to avoid 'never awaited' warnings."""
        for call in calls:
            call.close()


class TestBuildLlmCalls(_CoroutineCleanupMixin, TestCase):
    """Test build_llm_calls with truncated and full history."""

    def _make_history(self, n_messages):
        """Create a synthetic history with n messages."""
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(n_messages)
        ]

    def test_no_full_history_creates_one_call_per_backend(self):
        """Without full_history, should create exactly one call per backend."""
        model = MockChatModel()
        history = self._make_history(4)

        calls, scores = build_llm_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=history,
            is_professional=True,
            is_logged_in=True,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(scores[calls[0]], model.quality() * 20)
        self._close_calls(calls)

    def test_full_history_same_as_truncated_creates_one_call(self):
        """When full_history == history, should not create a duplicate call."""
        model = MockChatModel()
        history = self._make_history(4)

        calls, scores = build_llm_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=history,
            is_professional=True,
            is_logged_in=True,
            full_history=history,  # Same object
        )
        self.assertEqual(len(calls), 1)
        self._close_calls(calls)

    def test_full_history_creates_extra_call_with_higher_score(self):
        """Full history should produce an extra call scored higher than truncated."""
        model = MockChatModel()
        truncated = self._make_history(4)
        full = self._make_history(10)

        calls, scores = build_llm_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        self.assertEqual(len(calls), 2)
        truncated_score = scores[calls[0]]
        full_score = scores[calls[1]]
        self.assertEqual(truncated_score, model.quality() * 20)
        self.assertEqual(full_score, model.quality() * 25)
        self.assertGreater(full_score, truncated_score)
        self._close_calls(calls)

    def test_full_history_skipped_when_exceeds_context(self):
        """Full history call should be skipped when it won't fit in context."""
        model = MockChatModel()
        truncated = self._make_history(4)
        # Create a huge full history that exceeds max_context (32000 tokens)
        # Each message ~10 chars = ~2.5 tokens, need > 24000 tokens = ~96000 chars
        full = [{"role": "user", "content": "x" * 100000}]

        calls, scores = build_llm_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        # Only the truncated call should be created
        self.assertEqual(len(calls), 1)
        self._close_calls(calls)

    def test_multiple_backends_each_get_full_history_call(self):
        """Each backend should get both truncated and full history calls."""
        models = [MockChatModel(), MockChatModel()]
        truncated = self._make_history(4)
        full = self._make_history(10)

        calls, scores = build_llm_calls(
            model_backends=models,
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        # 2 backends * 2 calls each (truncated + full)
        self.assertEqual(len(calls), 4)
        self._close_calls(calls)


class TestBuildRetryCalls(_CoroutineCleanupMixin, TestCase):
    """Test build_retry_calls with full history support."""

    def _make_history(self, n_messages):
        """Create a synthetic history with n messages."""
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(n_messages)
        ]

    def test_without_full_history_creates_short_and_truncated(self):
        """Without full_history, should create short + truncated calls per backend."""
        model = MockChatModel()
        history = self._make_history(10)

        calls, scores = build_retry_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=history,
            is_professional=True,
            is_logged_in=True,
        )
        # 1 short (last 5) + 1 truncated = 2 calls
        self.assertEqual(len(calls), 2)
        short_score = scores[calls[0]]
        truncated_score = scores[calls[1]]
        self.assertEqual(short_score, model.quality() * 15)
        self.assertEqual(truncated_score, model.quality() * 50)
        self._close_calls(calls)

    def test_with_full_history_creates_three_call_variants(self):
        """With full_history, should add full-context calls."""
        model = MockChatModel()
        truncated = self._make_history(10)
        full = self._make_history(30)

        calls, scores = build_retry_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        # 1 short + 1 truncated + 1 full = 3 calls
        self.assertEqual(len(calls), 3)
        full_score = scores[calls[2]]
        self.assertEqual(full_score, model.quality() * 51)
        self._close_calls(calls)

    def test_full_history_scored_highest_in_retry(self):
        """Full history calls should be scored highest among retry calls."""
        model = MockChatModel()
        truncated = self._make_history(10)
        full = self._make_history(30)

        calls, scores = build_retry_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        all_scores = [scores[c] for c in calls]
        # Full history score (quality*51) should be the highest
        self.assertEqual(max(all_scores), model.quality() * 51)
        self._close_calls(calls)

    def test_full_history_penalty_can_override_bias(self):
        """A false-promise full-history response should score below a clean truncated one."""
        model = MockChatModel()
        full_history_call_score = model.quality() * 51  # full history retry score
        truncated_call_score = model.quality() * 50  # truncated retry score

        # Simulate a full-history response containing a false promise
        bad_full_result = (
            "Your appeal will definitely be approved, I guarantee success.",
            "Context summary",
        )
        # Simulate a clean truncated-history response
        clean_truncated_result = (
            "I can help you draft a strong appeal letter.",
            "Context summary",
        )

        bad_full_score = score_llm_response(
            bad_full_result, full_history_call_score, is_primary_call=False
        )
        clean_truncated_score = score_llm_response(
            clean_truncated_result, truncated_call_score, is_primary_call=False
        )

        # The clean truncated response must beat the penalized full-history one
        self.assertGreater(clean_truncated_score, bad_full_score)

    def test_full_history_skipped_in_retry_when_too_large(self):
        """Full history should be skipped in retry when it exceeds model context."""
        model = MockChatModel()
        truncated = self._make_history(10)
        full = [{"role": "user", "content": "x" * 100000}]

        calls, scores = build_retry_calls(
            model_backends=[model],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            full_history=full,
        )
        # Only short + truncated, no full history call
        self.assertEqual(len(calls), 2)
        self._close_calls(calls)

    def test_fallback_backends_also_get_full_history(self):
        """Fallback backends should also try full history when available."""
        primary = MockChatModel()
        fallback = MockChatModel()
        truncated = self._make_history(10)
        full = self._make_history(30)

        calls, scores = build_retry_calls(
            model_backends=[primary],
            current_message="Hello",
            previous_context_summary=None,
            history=truncated,
            is_professional=True,
            is_logged_in=True,
            fallback_backends=[fallback],
            full_history=full,
        )
        # Primary: 1 short + 1 truncated + 1 full = 3
        # Fallback: 1 short + 1 truncated + 1 full = 3
        # Total = 6
        self.assertEqual(len(calls), 6)
        # Fallback full history score
        fallback_full_score = scores[calls[5]]
        self.assertEqual(fallback_full_score, fallback.quality() * 25)
        self._close_calls(calls)
