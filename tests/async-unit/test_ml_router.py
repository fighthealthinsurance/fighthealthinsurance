import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
from typing import Optional

from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.ml.ml_models import RemoteModelLike


class TestMLRouterGenerateTextBackendNames(unittest.TestCase):
    """Tests for MLRouter.generate_text_backend_names method."""

    def setUp(self):
        """Set up test fixtures."""
        self.router = MLRouter()

    def test_generate_text_backend_names_returns_friendly_names(self):
        """Test that generate_text_backend_names returns friendly names (keys from models_by_name).

        This is a regression test for a bug where the method was returning internal
        model paths (e.g., 'TotallyLegitCo/fighthealthinsurance_model_v0.5') instead
        of friendly names (e.g., 'fhi-legacy').
        """
        # Create mock models
        mock_model1 = MagicMock(spec=RemoteModelLike)
        mock_model1.external = False
        mock_model1.model = "internal/path/model1"  # Internal path, NOT friendly name

        mock_model2 = MagicMock(spec=RemoteModelLike)
        mock_model2.external = False
        mock_model2.model = "internal/path/model2"

        # Set up router with friendly names as keys
        self.router.internal_models_by_cost = [mock_model1, mock_model2]
        self.router.models_by_name = {
            "friendly-name-1": [mock_model1],
            "friendly-name-2": [mock_model2],
        }

        # Get the names
        names = self.router.generate_text_backend_names(use_external=False)

        # Names should be the friendly names (keys), not internal paths
        for name in names:
            self.assertIn(
                name,
                self.router.models_by_name,
                f"Returned name '{name}' is not a key in models_by_name. "
                f"Available keys: {list(self.router.models_by_name.keys())}",
            )
            # Should NOT be the internal path
            self.assertNotIn(
                "/",
                name,
                f"Name '{name}' looks like an internal path, not a friendly name",
            )

    def test_generate_text_backend_names_can_be_looked_up(self):
        """Test that all returned names can be looked up in models_by_name.

        This ensures the names returned are actually usable for model lookup.
        """
        # Use real router initialization (which may have real or no models)
        names = self.router.generate_text_backend_names(use_external=False)

        # Every name returned should be a valid key in models_by_name
        for name in names:
            self.assertIn(
                name,
                self.router.models_by_name,
                f"Name '{name}' returned by generate_text_backend_names cannot be "
                f"looked up in models_by_name. This would cause 'No backend for {name}' errors.",
            )


class TestMLRouterChatBackends(unittest.TestCase):
    """Tests for MLRouter chat-related methods."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a minimal router instance
        self.router = MLRouter()

    def test_get_chat_backends_with_fallback_no_external(self):
        """Test get_chat_backends_with_fallback with use_external=False."""
        primary_models, fallback_models = self.router.get_chat_backends_with_fallback(
            use_external=False
        )

        # Primary models should be returned
        self.assertIsInstance(primary_models, list)
        # Fallback models should be empty when use_external=False
        self.assertEqual(len(fallback_models), 0)
        self.assertIsInstance(fallback_models, list)

    def test_get_chat_backends_with_fallback_with_external(self):
        """Test get_chat_backends_with_fallback with use_external=True."""
        # Create mock external models with different quality levels
        mock_ext_model1 = MagicMock(spec=RemoteModelLike)
        mock_ext_model1.external = True
        mock_ext_model1.quality.return_value = 80

        mock_ext_model2 = MagicMock(spec=RemoteModelLike)
        mock_ext_model2.external = True
        mock_ext_model2.quality.return_value = 90

        mock_ext_model3 = MagicMock(spec=RemoteModelLike)
        mock_ext_model3.external = True
        mock_ext_model3.quality.return_value = 70

        mock_int_model = MagicMock(spec=RemoteModelLike)
        mock_int_model.external = False

        # Set up router with mixed models
        self.router.all_models_by_cost = [
            mock_int_model,
            mock_ext_model1,
            mock_ext_model2,
            mock_ext_model3,
        ]

        primary_models, fallback_models = self.router.get_chat_backends_with_fallback(
            use_external=True
        )

        # Primary models should be returned
        self.assertIsInstance(primary_models, list)
        # Fallback models should contain external models only
        self.assertGreater(len(fallback_models), 0)
        # All fallback models should be external
        for model in fallback_models:
            self.assertTrue(model.external)

    def test_get_chat_backends_with_fallback_sorts_by_quality(self):
        """Test that fallback models are sorted by quality (highest first)."""
        # Create mock external models with different quality levels
        mock_ext_model1 = MagicMock(spec=RemoteModelLike)
        mock_ext_model1.external = True
        mock_ext_model1.quality.return_value = 60

        mock_ext_model2 = MagicMock(spec=RemoteModelLike)
        mock_ext_model2.external = True
        mock_ext_model2.quality.return_value = 95

        mock_ext_model3 = MagicMock(spec=RemoteModelLike)
        mock_ext_model3.external = True
        mock_ext_model3.quality.return_value = 75

        # Set up router with external models
        self.router.all_models_by_cost = [
            mock_ext_model1,
            mock_ext_model2,
            mock_ext_model3,
        ]

        _, fallback_models = self.router.get_chat_backends_with_fallback(
            use_external=True
        )

        # Verify models are sorted by quality (highest first)
        if len(fallback_models) >= 2:
            qualities = [m.quality() for m in fallback_models]
            self.assertEqual(qualities, sorted(qualities, reverse=True))

    def test_get_chat_backends_with_fallback_limits_to_four(self):
        """Test that fallback models are limited to 4 models."""
        # Create many mock external models
        mock_models = []
        for i in range(10):
            mock_model = MagicMock(spec=RemoteModelLike)
            mock_model.external = True
            mock_model.quality.return_value = 50 + i
            mock_models.append(mock_model)

        self.router.all_models_by_cost = mock_models

        _, fallback_models = self.router.get_chat_backends_with_fallback(
            use_external=True
        )

        # Should limit to 4 models
        self.assertLessEqual(len(fallback_models), 4)

    def test_get_chat_backends_with_fallback_no_external_available(self):
        """Test behavior when no external models are available."""
        # Create only internal models
        mock_int_model1 = MagicMock(spec=RemoteModelLike)
        mock_int_model1.external = False

        mock_int_model2 = MagicMock(spec=RemoteModelLike)
        mock_int_model2.external = False

        self.router.all_models_by_cost = [mock_int_model1, mock_int_model2]

        primary_models, fallback_models = self.router.get_chat_backends_with_fallback(
            use_external=True
        )

        # Primary models should be returned
        self.assertIsInstance(primary_models, list)
        # Fallback should be empty since no external models exist
        self.assertEqual(len(fallback_models), 0)

    def test_get_chat_backends_with_fallback_calls_get_chat_backends(self):
        """Test that get_chat_backends_with_fallback reuses get_chat_backends."""
        with patch.object(
            self.router, "get_chat_backends", return_value=["mock_model"]
        ) as mock_get_chat:
            primary_models, _ = self.router.get_chat_backends_with_fallback(
                use_external=False
            )

            # Verify get_chat_backends was called with use_external=False
            mock_get_chat.assert_called_once_with(use_external=False)
            self.assertEqual(primary_models, ["mock_model"])


class TestMLRouterSummarizeChatHistory(unittest.TestCase):
    """Tests for MLRouter.summarize_chat_history method."""

    def setUp(self):
        """Set up test fixtures."""
        self.router = MLRouter()

    async def async_test_summarize_chat_history_short_history(self):
        """Test that short history returns None without summarization."""
        # History with fewer messages than max_messages
        short_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "Can you help me?"},
        ]

        result = await self.router.summarize_chat_history(
            history=short_history, max_messages=10
        )

        # Should return None since history is shorter than max_messages
        self.assertIsNone(result)

    def test_summarize_chat_history_short_history(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_short_history())

    async def async_test_summarize_chat_history_empty_history(self):
        """Test that empty history returns None."""
        result = await self.router.summarize_chat_history(history=[], max_messages=10)

        self.assertIsNone(result)

    def test_summarize_chat_history_empty_history(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_empty_history())

    async def async_test_summarize_chat_history_exactly_max_messages(self):
        """Test that history with exactly max_messages returns None."""
        history = [{"role": "user", "content": f"Message {i}"} for i in range(10)]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=10
        )

        # Should return None since history length equals max_messages
        self.assertIsNone(result)

    def test_summarize_chat_history_exactly_max_messages(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_exactly_max_messages())

    async def async_test_summarize_chat_history_long_history(self):
        """Test that long history gets summarized."""
        # Create history longer than max_messages
        history = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        # Create a mock model that returns a summary
        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = (
            "This is a summary of the conversation"
        )

        self.router.internal_models_by_cost = [mock_model]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=10
        )

        # Should return the summary
        self.assertIsNotNone(result)
        self.assertEqual(result, "This is a summary of the conversation")
        # Verify _infer_no_context was called
        mock_model._infer_no_context.assert_called_once()

    def test_summarize_chat_history_long_history(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_long_history())

    async def async_test_summarize_chat_history_empty_content(self):
        """Test handling of messages with empty content."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": ""},  # Empty content
            {"role": "user", "content": "Are you there?"},
            {"role": "assistant", "content": ""},  # Empty content
        ] * 4  # Make it long enough to trigger summarization

        # Create a mock model
        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = "Summary of non-empty messages"

        self.router.internal_models_by_cost = [mock_model]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=5
        )

        # Should still return a summary, skipping empty content
        self.assertIsNotNone(result)

    def test_summarize_chat_history_empty_content(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_empty_content())

    async def async_test_summarize_chat_history_truncates_long_messages(self):
        """Test that very long messages are truncated."""
        # Create a message with very long content (>500 chars)
        long_content = "A" * 600
        history = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "Short response"},
        ] * 8  # Make it long enough to trigger summarization

        # Create a mock model
        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = "Summary with truncated messages"

        self.router.internal_models_by_cost = [mock_model]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=5
        )

        # Verify the prompt passed to the model contains truncated content
        call_args = mock_model._infer_no_context.call_args
        prompt = call_args.kwargs.get("prompt", "")

        # Should contain the truncation indicator
        self.assertIn("...", prompt)
        # Should not contain the full 600 characters
        self.assertNotIn("A" * 600, prompt)

    def test_summarize_chat_history_truncates_long_messages(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_truncates_long_messages())

    async def async_test_summarize_chat_history_model_failure(self):
        """Test that model failure returns None."""
        history = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        # Create a mock model that raises an exception
        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.side_effect = Exception("Model error")

        self.router.internal_models_by_cost = [mock_model]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=10
        )

        # Should return None when model fails
        self.assertIsNone(result)

    def test_summarize_chat_history_model_failure(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_model_failure())

    async def async_test_summarize_chat_history_multiple_model_failures(self):
        """Test that tries multiple models when first fails."""
        history = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        # First model fails, second succeeds
        mock_model1 = AsyncMock(spec=RemoteModelLike)
        mock_model1._infer_no_context.side_effect = Exception("Model 1 error")

        mock_model2 = AsyncMock(spec=RemoteModelLike)
        mock_model2._infer_no_context.return_value = "Summary from model 2"

        self.router.internal_models_by_cost = [mock_model1, mock_model2]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=10
        )

        # Should return summary from second model
        self.assertEqual(result, "Summary from model 2")
        # Both models should have been tried
        mock_model1._infer_no_context.assert_called_once()
        mock_model2._infer_no_context.assert_called_once()

    def test_summarize_chat_history_multiple_model_failures(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_multiple_model_failures())

    async def async_test_summarize_chat_history_short_summary_rejected(self):
        """Test that very short summaries (<=10 chars) are rejected."""
        history = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        # First model returns too short summary, second returns good one
        mock_model1 = AsyncMock(spec=RemoteModelLike)
        mock_model1._infer_no_context.return_value = "Short"  # 5 chars

        mock_model2 = AsyncMock(spec=RemoteModelLike)
        mock_model2._infer_no_context.return_value = "This is a proper summary"

        self.router.internal_models_by_cost = [mock_model1, mock_model2]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=10
        )

        # Should use second model's summary since first was too short
        self.assertEqual(result, "This is a proper summary")

    def test_summarize_chat_history_short_summary_rejected(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_short_summary_rejected())

    async def async_test_summarize_chat_history_all_empty_content(self):
        """Test that history with all empty content returns None."""
        history = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
        ] * 8

        result = await self.router.summarize_chat_history(
            history=history, max_messages=5
        )

        # Should return None when all content is empty
        self.assertIsNone(result)

    def test_summarize_chat_history_all_empty_content(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_all_empty_content())

    async def async_test_summarize_chat_history_missing_keys(self):
        """Test handling of messages with missing role or content keys."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant"},  # Missing content
            {"content": "Message without role"},  # Missing role
            {"role": "user", "content": "Another message"},
        ] * 4

        # Create a mock model
        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = "Summary despite missing keys"

        self.router.internal_models_by_cost = [mock_model]

        result = await self.router.summarize_chat_history(
            history=history, max_messages=5
        )

        # Should handle missing keys gracefully
        self.assertIsNotNone(result)

    def test_summarize_chat_history_missing_keys(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_missing_keys())

    async def async_test_summarize_chat_history_uses_correct_system_prompt(self):
        """Test that summarization uses appropriate system prompt."""
        history = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = "Test summary"

        self.router.internal_models_by_cost = [mock_model]

        await self.router.summarize_chat_history(history=history, max_messages=10)

        # Verify the system prompt contains relevant keywords
        call_args = mock_model._infer_no_context.call_args
        system_prompts = call_args.kwargs.get("system_prompts", [])

        self.assertTrue(len(system_prompts) > 0)
        system_prompt = system_prompts[0]
        # Check for keywords related to health insurance
        self.assertIn("health insurance", system_prompt.lower())

    def test_summarize_chat_history_uses_correct_system_prompt(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_uses_correct_system_prompt())

    async def async_test_summarize_chat_history_only_summarizes_older_messages(self):
        """Test that only older messages are summarized, keeping recent ones."""
        history = [
            {"role": "user", "content": f"Old message {i}"} for i in range(8)
        ] + [{"role": "user", "content": f"Recent message {i}"} for i in range(5)]

        mock_model = AsyncMock(spec=RemoteModelLike)
        mock_model._infer_no_context.return_value = "Summary"

        self.router.internal_models_by_cost = [mock_model]

        await self.router.summarize_chat_history(history=history, max_messages=5)

        # Verify only older messages are in the prompt
        call_args = mock_model._infer_no_context.call_args
        prompt = call_args.kwargs.get("prompt", "")

        # Should contain old messages
        self.assertIn("Old message", prompt)
        # Should not contain recent messages (they should be kept unsummarized)
        self.assertNotIn("Recent message", prompt)

    def test_summarize_chat_history_only_summarizes_older_messages(self):
        """Run the async test using asyncio."""
        asyncio.run(
            self.async_test_summarize_chat_history_only_summarizes_older_messages()
        )

    async def async_test_summarize_chat_history_max_messages_zero(self):
        """Test summarize_chat_history with max_messages=0 summarizes all messages."""
        # Create mock model
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="Summary of all messages")
        self.router.internal_models_by_cost = [mock_model]

        history = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Reply 2"},
        ]
        result = await self.router.summarize_chat_history(
            history=history, max_messages=0
        )

        self.assertEqual(result, "Summary of all messages")
        # Verify model was called
        mock_model._infer_no_context.assert_called_once()
        # All messages should be in the prompt
        call_args = mock_model._infer_no_context.call_args
        prompt = call_args.kwargs.get("prompt", "")
        self.assertIn("Message 1", prompt)
        self.assertIn("Reply 1", prompt)
        self.assertIn("Message 2", prompt)
        self.assertIn("Reply 2", prompt)

    def test_summarize_chat_history_max_messages_zero(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_max_messages_zero())

    async def async_test_summarize_chat_history_role_normalization(self):
        """Test that 'agent' and 'system' roles are normalized to 'assistant'."""
        # Create mock model
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(
            return_value="Summary with normalized roles"
        )
        self.router.internal_models_by_cost = [mock_model]

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "agent", "content": "I'm an agent"},
            {"role": "system", "content": "System message"},
            {"role": "assistant", "content": "I'm an assistant"},
        ]
        result = await self.router.summarize_chat_history(
            history=history, max_messages=0
        )

        self.assertEqual(result, "Summary with normalized roles")
        # Verify model was called
        mock_model._infer_no_context.assert_called_once()
        # Check the prompt contains normalized roles
        call_args = mock_model._infer_no_context.call_args
        prompt = call_args.kwargs.get("prompt", "")
        # All assistant-like roles should be normalized
        self.assertIn("assistant:", prompt.lower())

    def test_summarize_chat_history_role_normalization(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_summarize_chat_history_role_normalization())

    async def async_test_summarize_chat_history_consecutive_same_role_merged(self):
        """Test that consecutive messages with the same role are merged."""
        # Create mock model
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="Merged summary")
        self.router.internal_models_by_cost = [mock_model]

        history = [
            {"role": "user", "content": "Message A"},
            {"role": "user", "content": "Message B"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "assistant", "content": "Reply 2"},
        ]
        result = await self.router.summarize_chat_history(
            history=history, max_messages=0
        )

        self.assertEqual(result, "Merged summary")
        # Verify model was called
        mock_model._infer_no_context.assert_called_once()
        call_args = mock_model._infer_no_context.call_args
        prompt = call_args.kwargs.get("prompt", "")
        # Both user messages should be present (merged)
        self.assertIn("Message A", prompt)
        self.assertIn("Message B", prompt)
        # Both replies should be present (merged)
        self.assertIn("Reply 1", prompt)
        self.assertIn("Reply 2", prompt)

    def test_summarize_chat_history_consecutive_same_role_merged(self):
        """Run the async test using asyncio."""
        asyncio.run(
            self.async_test_summarize_chat_history_consecutive_same_role_merged()
        )
