"""
Tests for the RemoteAnthropic class in fighthealthinsurance.ml.ml_models.

Tests cover:
- Model initialization and configuration
- Rate limit checking before inference
- 429 response handling
- Model registration and cost tiers
"""

import asyncio
import os
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp

# Set up environment before importing RemoteAnthropic
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from fighthealthinsurance.ml.ml_models import RemoteAnthropic
from fighthealthinsurance.utils import RateLimiter


class TestRemoteAnthropicInit(unittest.TestCase):
    """Tests for RemoteAnthropic initialization."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_init_with_valid_api_key(self):
        """Test RemoteAnthropic initializes with valid API key."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertEqual(model.model, "claude-sonnet-4-6")
        self.assertTrue(model.supports_system)
        self.assertTrue(model.external)

    @patch.dict(os.environ, {}, clear=True)
    def test_init_without_api_key_raises(self):
        """Test RemoteAnthropic raises EnvironmentError without API key."""
        if "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]

        with self.assertRaises(EnvironmentError) as context:
            RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertIn("ANTHROPIC_API_KEY", str(context.exception))

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_init_creates_rate_limiter(self):
        """Test RemoteAnthropic creates a rate limiter for the model."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertIn("claude-sonnet-4-6", RemoteAnthropic._rate_limiters)
        self.assertIsInstance(model.rate_limiter, RateLimiter)

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_all_models_use_200k_context(self):
        """Test all Claude models use 200K context length."""
        for model_name in RemoteAnthropic.MODEL_SPECS.keys():
            model = RemoteAnthropic(model=model_name)
            self.assertEqual(model.get_max_context(), 200000)

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_uses_anthropic_api_base(self):
        """Test that the api_base points at Anthropic's API."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertEqual(model.api_base, "https://api.anthropic.com/v1")


class TestRemoteAnthropicModels(unittest.TestCase):
    """Tests for RemoteAnthropic.models() class method."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_models_returns_three_models(self):
        """Test models() returns three Claude models (Haiku, Sonnet, Opus)."""
        models = RemoteAnthropic.models()

        self.assertEqual(len(models), 3)

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_models_have_anthropic_prefix(self):
        """Test models have anthropic/ prefix in their friendly name."""
        models = RemoteAnthropic.models()
        model_names = [m.name for m in models]

        for name in model_names:
            self.assertTrue(
                name.startswith("anthropic/"),
                f"{name} should start with 'anthropic/'",
            )

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_models_cost_ordering(self):
        """Haiku < Sonnet < Opus in cost."""
        models = RemoteAnthropic.models()
        models_by_name = {m.name: m for m in models}

        haiku_cost = models_by_name["anthropic/claude-haiku-4-5"].cost
        sonnet_cost = models_by_name["anthropic/claude-sonnet-4-6"].cost
        opus_cost = models_by_name["anthropic/claude-opus-4-7"].cost

        self.assertLess(haiku_cost, sonnet_cost)
        self.assertLess(sonnet_cost, opus_cost)

    @patch.dict(os.environ, {}, clear=True)
    def test_models_returns_empty_without_api_key(self):
        """Test models() returns empty list without API key."""
        if "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]

        models = RemoteAnthropic.models()

        self.assertEqual(len(models), 0)


class TestRemoteAnthropicTiers(unittest.TestCase):
    """Tests for model tier classification."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_haiku_is_speed_tier(self):
        """Test Haiku is in speed tier."""
        model = RemoteAnthropic(model="claude-haiku-4-5-20251001")

        self.assertEqual(model.get_tier(), "speed")

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_sonnet_is_quality_tier(self):
        """Test Sonnet is in quality tier."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertEqual(model.get_tier(), "quality")

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_opus_is_premium_tier(self):
        """Test Opus is in premium tier."""
        model = RemoteAnthropic(model="claude-opus-4-7")

        self.assertEqual(model.get_tier(), "premium")


class TestRemoteAnthropicInfer(unittest.TestCase):
    """Tests for RemoteAnthropic._infer method."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_checks_rate_limit(self):
        """Test that _infer returns None without calling API when rate limited."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        model.rate_limiter.mark_exhausted(60.0)

        result = await model._infer(
            system_prompts=["You are helpful."], prompt="Test prompt"
        )

        self.assertIsNone(result)

    def test_infer_checks_rate_limit(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_checks_rate_limit())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_handles_429(self):
        """Test that _infer handles 429 response and marks exhausted."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": "120"},
        )

        with patch.object(
            RemoteAnthropic.__bases__[0],
            "_infer",
            new_callable=AsyncMock,
            side_effect=error,
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        self.assertIsNone(result)

        status = model.rate_limiter.get_status()
        self.assertTrue(status["exhausted"])

    def test_infer_handles_429(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_handles_429())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_429_with_invalid_retry_after_uses_default(self):
        """Test 429 with non-numeric Retry-After falls back to default 60s."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )

        with patch.object(
            RemoteAnthropic.__bases__[0],
            "_infer",
            new_callable=AsyncMock,
            side_effect=error,
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        self.assertIsNone(result)

        status = model.rate_limiter.get_status()
        self.assertTrue(status["exhausted"])
        self.assertGreater(status["exhausted_until"], time.time() + 50)
        self.assertLess(status["exhausted_until"], time.time() + 70)

    def test_infer_429_with_invalid_retry_after_uses_default(self):
        """Run the async test for invalid Retry-After."""
        asyncio.run(self.async_test_infer_429_with_invalid_retry_after_uses_default())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_other_http_errors_propagate(self):
        """Test that non-429 HTTP errors are re-raised, not swallowed."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Server error",
            headers={},
        )

        with patch.object(
            RemoteAnthropic.__bases__[0],
            "_infer",
            new_callable=AsyncMock,
            side_effect=error,
        ):
            with self.assertRaises(aiohttp.ClientResponseError):
                await model._infer(
                    system_prompts=["You are helpful."], prompt="Test prompt"
                )

    def test_infer_other_http_errors_propagate(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_other_http_errors_propagate())


class TestRemoteAnthropicModelIsOk(unittest.TestCase):
    """Tests for RemoteAnthropic.model_is_ok method."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_model_is_ok_when_available(self):
        """Test model_is_ok returns True when not rate limited."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertTrue(model.model_is_ok())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_model_is_ok_false_when_rate_limited(self):
        """Test model_is_ok returns False when rate limited."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        model.rate_limiter.mark_exhausted(60.0)

        self.assertFalse(model.model_is_ok())

    def test_model_is_ok_false_without_api_key(self):
        """Test model_is_ok returns False without API key."""
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        del os.environ["ANTHROPIC_API_KEY"]
        try:
            self.assertFalse(model.model_is_ok())
        finally:
            os.environ["ANTHROPIC_API_KEY"] = "test-api-key"


class TestRemoteAnthropicIntegration(unittest.TestCase):
    """Integration tests for RemoteAnthropic with MLRouter."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_anthropic_models_are_external(self):
        """Test that all Anthropic models are marked as external."""
        models = RemoteAnthropic.models()

        for model_desc in models:
            if model_desc.model is None:
                model_desc.model = RemoteAnthropic(model=model_desc.internal_name)

            self.assertTrue(model_desc.model.external)


if __name__ == "__main__":
    unittest.main()
