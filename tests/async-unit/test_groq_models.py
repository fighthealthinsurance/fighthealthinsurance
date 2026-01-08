"""
Tests for the RemoteGroq class in fighthealthinsurance.ml.ml_models.

Tests cover:
- Model initialization and configuration
- Load balancing across quality tier models
- Fallback to speed tier when quality exhausted
- Rate limit checking before inference
- 429 response handling
"""

import asyncio
import os
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp

# Set up environment before importing RemoteGroq
os.environ.setdefault("GROQ_API_KEY", "test-api-key")

from fighthealthinsurance.ml.ml_models import RemoteGroq
from fighthealthinsurance.utils import RateLimiter


class TestRemoteGroqInit(unittest.TestCase):
    """Tests for RemoteGroq initialization."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_init_with_valid_api_key(self):
        """Test RemoteGroq initializes with valid API key."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        self.assertEqual(model.model, "meta-llama/llama-3.3-70b-versatile")
        self.assertTrue(model.supports_system)
        self.assertTrue(model.external)

    @patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=True)
    def test_init_without_api_key_raises(self):
        """Test RemoteGroq raises exception without API key."""
        # Clear the key
        if "GROQ_API_KEY" in os.environ:
            del os.environ["GROQ_API_KEY"]

        with self.assertRaises(Exception) as context:
            RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        self.assertIn("GROQ_API_KEY", str(context.exception))

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_init_creates_rate_limiter(self):
        """Test RemoteGroq creates a rate limiter for the model."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        self.assertIn("meta-llama/llama-3.3-70b-versatile", RemoteGroq._rate_limiters)
        self.assertIsInstance(model.rate_limiter, RateLimiter)

    @patch.dict(
        os.environ,
        {"GROQ_API_KEY": "test-key", "GROQ_RPM_LIMIT": "20", "GROQ_RPD_LIMIT": "500"},
    )
    def test_init_respects_env_rate_limits(self):
        """Test RemoteGroq uses environment variable rate limits."""
        RemoteGroq._rate_limiters.clear()  # Clear to force re-creation
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        self.assertEqual(model.rate_limiter.rpm_limit, 20)
        self.assertEqual(model.rate_limiter.rpd_limit, 500)

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_all_models_use_128k_context(self):
        """Test all models use 128K context length."""
        for model_name in RemoteGroq.MODEL_SPECS.keys():
            model = RemoteGroq(model=model_name)
            self.assertEqual(model.get_max_context(), 128000)


class TestRemoteGroqModels(unittest.TestCase):
    """Tests for RemoteGroq.models() class method."""

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_models_returns_four_models(self):
        """Test models() returns all four Groq models."""
        models = RemoteGroq.models()

        self.assertEqual(len(models), 4)

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_models_have_correct_names(self):
        """Test models have correct names with groq/ prefix."""
        models = RemoteGroq.models()
        model_names = [m.name for m in models]

        self.assertIn("groq/llama-4-maverick-17b-128e-instruct", model_names)
        self.assertIn("groq/llama-4-scout-17b-16e-instruct", model_names)
        self.assertIn("groq/llama-3.3-70b-versatile", model_names)
        self.assertIn("groq/llama-3.1-8b-instant", model_names)

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_models_cost_tiers(self):
        """Test models have correct cost tiers (quality=4, speed=6)."""
        models = RemoteGroq.models()
        models_by_name = {m.name: m for m in models}

        # Quality tier models should have cost=4
        self.assertEqual(
            models_by_name["groq/llama-4-maverick-17b-128e-instruct"].cost, 4
        )
        self.assertEqual(models_by_name["groq/llama-4-scout-17b-16e-instruct"].cost, 4)
        self.assertEqual(models_by_name["groq/llama-3.3-70b-versatile"].cost, 4)

        # Speed tier model should have cost=6
        self.assertEqual(models_by_name["groq/llama-3.1-8b-instant"].cost, 6)

    @patch.dict(os.environ, {}, clear=True)
    def test_models_returns_empty_without_api_key(self):
        """Test models() returns empty list without API key."""
        if "GROQ_API_KEY" in os.environ:
            del os.environ["GROQ_API_KEY"]

        models = RemoteGroq.models()

        self.assertEqual(len(models), 0)

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_groq_cheaper_than_deepinfra_llama32(self):
        """Test Groq models are cheaper than DeepInfra llama-3.2-3B (cost=8)."""
        models = RemoteGroq.models()

        for model_desc in models:
            self.assertLess(
                model_desc.cost,
                8,
                f"{model_desc.name} should be cheaper than DeepInfra llama-3.2-3B",
            )


class TestRemoteGroqTiers(unittest.TestCase):
    """Tests for model tier classification."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_quality_tier_models(self):
        """Test quality tier models are correctly identified."""
        quality_models = [
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-3.3-70b-versatile",
        ]

        for model_name in quality_models:
            model = RemoteGroq(model=model_name)
            self.assertEqual(
                model.get_tier(), "quality", f"{model_name} should be quality tier"
            )

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_speed_tier_model(self):
        """Test speed tier model is correctly identified."""
        model = RemoteGroq(model="meta-llama/llama-3.1-8b-instant")

        self.assertEqual(model.get_tier(), "speed")


class TestRemoteGroqModelSelection(unittest.TestCase):
    """Tests for model selection."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_select_available_model(self):
        """Test select_available_model returns a quality tier model."""
        selected = RemoteGroq.select_available_model()

        # Should select from quality tier
        self.assertIsNotNone(selected)
        self.assertIn(
            selected,
            [
                "meta-llama/llama-4-maverick-17b-128e-instruct",
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "meta-llama/llama-3.3-70b-versatile",
            ],
        )

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_select_returns_none_when_all_rate_limited(self):
        """Test that None is returned when all models are rate limited."""
        # First, initialize all models
        for model_name in RemoteGroq.MODEL_SPECS.keys():
            RemoteGroq._ensure_rate_limiter(model_name)

        # Mark all as exhausted
        for limiter in RemoteGroq._rate_limiters.values():
            limiter.mark_exhausted(retry_after_seconds=60.0)

        selected = RemoteGroq.select_available_model()

        self.assertIsNone(selected)


class TestRemoteGroqLoadBalancing(unittest.TestCase):
    """Tests for load balancing across quality tier models."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_load_balancing_randomizes_quality_tier(self):
        """Test that selection randomizes across quality tier models."""
        selected_models = set()

        # Make many selections and track which models are selected
        for _ in range(100):
            selected = RemoteGroq.select_available_model()
            if selected:
                selected_models.add(selected)

        # Should have selected multiple different quality tier models
        quality_models = {
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-3.3-70b-versatile",
        }

        # At least 2 different models should have been selected
        self.assertGreaterEqual(len(selected_models & quality_models), 2)


class TestRemoteGroqFallback(unittest.TestCase):
    """Tests for fallback to speed tier."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_fallback_to_instant_when_quality_exhausted(self):
        """Test fallback to Instant tier when quality tier is exhausted."""
        # Initialize rate limiters
        for model_name in RemoteGroq.MODEL_SPECS.keys():
            RemoteGroq._ensure_rate_limiter(model_name)

        # Exhaust all quality tier models
        quality_models = [
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-3.3-70b-versatile",
        ]
        for model_name in quality_models:
            RemoteGroq._rate_limiters[model_name].mark_exhausted(60.0)

        selected = RemoteGroq.select_available_model()

        # Should fall back to Instant
        self.assertEqual(selected, "meta-llama/llama-3.1-8b-instant")


class TestRemoteGroqInfer(unittest.TestCase):
    """Tests for RemoteGroq._infer method."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def async_test_infer_checks_rate_limit(self):
        """Test that _infer checks rate limit before calling API."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        # Exhaust the rate limiter
        model.rate_limiter.mark_exhausted(60.0)

        result = await model._infer(
            system_prompts=["You are helpful."], prompt="Test prompt"
        )

        # Should return None without calling API
        self.assertIsNone(result)

    def test_infer_checks_rate_limit(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_checks_rate_limit())

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def async_test_infer_records_request(self):
        """Test that _infer records the request in rate limiter."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        initial_count = model.rate_limiter.get_status()["rpd_current"]

        # Mock the parent _infer to avoid actual API call
        with patch.object(
            RemoteGroq.__bases__[0],
            "_infer",
            new_callable=AsyncMock,
            return_value=("Response", []),
        ):
            await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        new_count = model.rate_limiter.get_status()["rpd_current"]

        # Should have recorded the request
        self.assertEqual(new_count, initial_count + 1)

    def test_infer_records_request(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_records_request())

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def async_test_infer_handles_429(self):
        """Test that _infer handles 429 response and marks exhausted."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        # Create a mock 429 error
        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": "120"},
        )

        # Mock parent _infer to raise 429
        with patch.object(
            RemoteGroq.__bases__[0], "_infer", new_callable=AsyncMock, side_effect=error
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        # Should return None
        self.assertIsNone(result)

        # Should have marked the limiter as exhausted
        status = model.rate_limiter.get_status()
        self.assertTrue(status["exhausted"])

    def test_infer_handles_429(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_handles_429())

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def async_test_infer_handles_429_with_http_date(self):
        """Test that _infer handles 429 response with HTTP-date Retry-After."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        # Create a future HTTP date (2 minutes from now)
        future_time = datetime.now(timezone.utc) + timedelta(minutes=2)
        http_date = future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")

        # Create a mock 429 error with HTTP-date Retry-After
        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": http_date},
        )

        # Mock parent _infer to raise 429
        with patch.object(
            RemoteGroq.__bases__[0], "_infer", new_callable=AsyncMock, side_effect=error
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        # Should return None
        self.assertIsNone(result)

        # Should have marked the limiter as exhausted with calculated delay
        status = model.rate_limiter.get_status()
        self.assertTrue(status["exhausted"])
        # The delay should be approximately 120 seconds (2 minutes)
        self.assertGreater(status["exhausted_until"], time.time() + 100)
        self.assertLess(status["exhausted_until"], time.time() + 140)

    def test_infer_handles_429_with_http_date(self):
        """Run the async test for HTTP-date Retry-After."""
        asyncio.run(self.async_test_infer_handles_429_with_http_date())

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    async def async_test_infer_handles_429_with_invalid_retry_after(self):
        """Test that _infer handles 429 response with invalid Retry-After."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        # Create a mock 429 error with invalid Retry-After
        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": "invalid-date-format"},
        )

        # Mock parent _infer to raise 429
        with patch.object(
            RemoteGroq.__bases__[0], "_infer", new_callable=AsyncMock, side_effect=error
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        # Should return None
        self.assertIsNone(result)

        # Should have marked the limiter as exhausted with default 60 seconds
        status = model.rate_limiter.get_status()
        self.assertTrue(status["exhausted"])
        # The delay should be approximately 60 seconds (default)
        self.assertGreater(status["exhausted_until"], time.time() + 50)
        self.assertLess(status["exhausted_until"], time.time() + 70)

    def test_infer_handles_429_with_invalid_retry_after(self):
        """Run the async test for invalid Retry-After."""
        asyncio.run(self.async_test_infer_handles_429_with_invalid_retry_after())


class TestRemoteGroqModelIsOk(unittest.TestCase):
    """Tests for RemoteGroq.model_is_ok method."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_model_is_ok_when_available(self):
        """Test model_is_ok returns True when not rate limited."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        self.assertTrue(model.model_is_ok())

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_model_is_ok_false_when_rate_limited(self):
        """Test model_is_ok returns False when rate limited."""
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        model.rate_limiter.mark_exhausted(60.0)

        self.assertFalse(model.model_is_ok())

    @patch.dict(os.environ, {}, clear=True)
    def test_model_is_ok_false_without_api_key(self):
        """Test model_is_ok returns False without API key."""
        # Need to create model with key, then remove key
        os.environ["GROQ_API_KEY"] = "test-key"
        model = RemoteGroq(model="meta-llama/llama-3.3-70b-versatile")

        # Remove key
        del os.environ["GROQ_API_KEY"]

        self.assertFalse(model.model_is_ok())


class TestRemoteGroqIntegration(unittest.TestCase):
    """Integration tests for RemoteGroq with MLRouter."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteGroq._rate_limiters.clear()

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_groq_models_are_external(self):
        """Test that all Groq models are marked as external."""
        models = RemoteGroq.models()

        for model_desc in models:
            # Initialize the model if not already
            if model_desc.model is None:
                model_desc.model = RemoteGroq(model=model_desc.internal_name)

            self.assertTrue(model_desc.model.external)

    @patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_groq_cost_between_internal_and_deepinfra(self):
        """Test Groq costs are between AlphaRemoteInternal (3) and DeepInfra (>=8)."""
        models = RemoteGroq.models()

        for model_desc in models:
            self.assertGreater(
                model_desc.cost,
                3,
                f"{model_desc.name} should cost more than AlphaRemoteInternal",
            )
            self.assertLess(
                model_desc.cost, 8, f"{model_desc.name} should cost less than DeepInfra"
            )


if __name__ == "__main__":
    unittest.main()
