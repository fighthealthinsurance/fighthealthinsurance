"""
Tests for the RemoteAnthropic class in fighthealthinsurance.ml.ml_models.

Tests cover:
- Model initialization and configuration
- Rate limit checking before inference
- 429 response handling
- The Models API liveness check (model_is_ok)
- Model registration and cost tiers
"""

import asyncio
import os
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp
import requests

from fighthealthinsurance.ml.ml_models import (
    ProviderUnavailable,
    RemoteAnthropic,
    RemoteFullOpenLike,
)
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
        opus_cost = models_by_name["anthropic/claude-opus-4-8"].cost

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
        model = RemoteAnthropic(model="claude-opus-4-8")

        self.assertEqual(model.get_tier(), "premium")

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_quality_opus_above_sonnet(self):
        """Premium tier (Opus) outranks quality tier (Sonnet)."""
        opus = RemoteAnthropic(model="claude-opus-4-8")
        sonnet = RemoteAnthropic(model="claude-sonnet-4-6")

        self.assertGreater(opus.quality(), sonnet.quality())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_quality_sonnet_above_haiku(self):
        """Quality tier (Sonnet) outranks speed tier (Haiku)."""
        sonnet = RemoteAnthropic(model="claude-sonnet-4-6")
        haiku = RemoteAnthropic(model="claude-haiku-4-5-20251001")

        self.assertGreater(sonnet.quality(), haiku.quality())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_quality_stays_below_internal_threshold(self):
        """External Claude quality stays below the internal models' range (>=101)
        so internal backends stay preferred in mixed scoring."""
        opus = RemoteAnthropic(model="claude-opus-4-8")

        self.assertLess(opus.quality(), 101)


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
            RemoteFullOpenLike,
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
            RemoteFullOpenLike,
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

    @staticmethod
    def _server_error() -> aiohttp.ClientResponseError:
        """A non-429 HTTP error from the parent transport."""
        return aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Server error",
            headers={},
        )

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_other_http_errors_return_none(self):
        """A non-429 HTTP error returns None to a caller that did not ask to
        raise, as on the shared transport: the caller moves on to the next
        model instead of counting a provider's 5xx as a bug."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        with patch.object(
            RemoteFullOpenLike,
            "_infer",
            new_callable=AsyncMock,
            side_effect=self._server_error(),
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        self.assertIsNone(result)

    def test_infer_other_http_errors_return_none(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_other_http_errors_return_none())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_other_http_errors_say_unavailable_when_asked(self):
        """raise_on_unavailable turns the HTTP error into ProviderUnavailable."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        with patch.object(
            RemoteFullOpenLike,
            "_infer",
            new_callable=AsyncMock,
            side_effect=self._server_error(),
        ):
            with self.assertRaises(ProviderUnavailable):
                await model._infer(
                    system_prompts=["You are helpful."],
                    prompt="Test prompt",
                    raise_on_unavailable=True,
                )

    def test_infer_other_http_errors_say_unavailable_when_asked(self):
        """Run the async test."""
        asyncio.run(
            self.async_test_infer_other_http_errors_say_unavailable_when_asked()
        )

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_other_http_errors_reach_a_probe(self):
        """A probe (raise_http_errors) still gets the raw status."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        with patch.object(
            RemoteFullOpenLike,
            "_infer",
            new_callable=AsyncMock,
            side_effect=self._server_error(),
        ):
            with self.assertRaises(aiohttp.ClientResponseError):
                await model._infer(
                    system_prompts=["You are helpful."],
                    prompt="Test prompt",
                    raise_http_errors=True,
                )

    def test_infer_other_http_errors_reach_a_probe(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_other_http_errors_reach_a_probe())

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    async def async_test_infer_429_through_real_infer_marks_exhausted(self):
        """End-to-end: a 429 from the underlying HTTP call must reach the
        subclass handler and trigger backoff, not be swallowed by the parent's
        catch-all exception handler.

        Mocks the inner `__infer` (the actual HTTP request) rather than the
        subclass's `_infer`, so the real `RemoteOpenLike._infer` runs in
        between and any regression in its exception handling will surface.
        """
        model = RemoteAnthropic(model="claude-sonnet-4-6")

        error = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=429,
            message="Rate limited",
            headers={"Retry-After": "120"},
        )

        # Name-mangled private method on RemoteOpenLike
        with patch.object(
            model,
            "_RemoteOpenLike__infer",
            new_callable=AsyncMock,
            side_effect=error,
        ):
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test prompt"
            )

        self.assertIsNone(result)
        self.assertTrue(model.rate_limiter.get_status()["exhausted"])

    def test_infer_429_through_real_infer_marks_exhausted(self):
        """Run the async test."""
        asyncio.run(self.async_test_infer_429_through_real_infer_marks_exhausted())


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
class TestRemoteAnthropicModelIsOk(unittest.TestCase):
    """model_is_ok asks Anthropic's Models API whether the model is served
    to our key, so the health sweep sees a refused key, a retired model or
    an outage, not just whether a key is set."""

    def setUp(self):
        """Reset class-level state before each test."""
        RemoteAnthropic._rate_limiters.clear()

    def _check(self, status=200, error=None, model=None):
        """model_is_ok with the Models API answering ``status`` (or raising
        ``error``); the patched requests.get, for what it was asked."""
        model = model or RemoteAnthropic(model="claude-sonnet-4-6")
        with patch("fighthealthinsurance.ml.ml_models.requests.get") as get:
            if error is not None:
                get.side_effect = error
            else:
                get.return_value = MagicMock(status_code=status)
            ok = model.model_is_ok()
        return ok, get

    def test_ok_when_the_models_api_serves_the_model(self):
        ok, _get = self._check(200)
        self.assertTrue(ok)

    def test_asks_the_models_api_for_this_model(self):
        _ok, get = self._check(200)
        self.assertEqual(
            get.call_args.args[0],
            "https://api.anthropic.com/v1/models/claude-sonnet-4-6",
        )

    def test_sends_the_native_api_key_and_version_headers(self):
        _ok, get = self._check(200)
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
        )

    def test_down_when_the_key_is_refused(self):
        ok, _get = self._check(401)
        self.assertFalse(ok)

    def test_down_when_the_model_is_retired(self):
        ok, _get = self._check(404)
        self.assertFalse(ok)

    def test_down_when_anthropic_is_overloaded(self):
        ok, _get = self._check(529)
        self.assertFalse(ok)

    def test_down_when_anthropic_cannot_be_reached(self):
        ok, _get = self._check(error=requests.ConnectionError("refused"))
        self.assertFalse(ok)

    def test_down_without_api_key(self):
        model = RemoteAnthropic(model="claude-sonnet-4-6")
        # patch.dict restores the pre-test environment on exit, so deleting
        # inside the patched scope leaks nothing to later tests.
        del os.environ["ANTHROPIC_API_KEY"]
        ok, _get = self._check(200, model=model)
        self.assertFalse(ok)

    def test_without_api_key_nothing_is_asked(self):
        model = RemoteAnthropic(model="claude-sonnet-4-6")
        del os.environ["ANTHROPIC_API_KEY"]
        _ok, get = self._check(200, model=model)
        get.assert_not_called()

    def test_a_rate_limit_back_off_leaves_the_check_passing(self):
        """is_available() reads the back-off live; in the check, the sweep
        would hold it until the model was next checked."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")
        model.rate_limiter.mark_exhausted(60.0)
        ok, _get = self._check(200, model=model)
        self.assertTrue(ok)

    def test_a_rate_limit_back_off_still_makes_it_unavailable(self):
        model = RemoteAnthropic(model="claude-sonnet-4-6")
        model.rate_limiter.mark_exhausted(60.0)
        self.assertFalse(model.is_available())

    def test_the_router_reads_the_sweep_for_it(self):
        """With a network check of its own, routing gates it on the sweep's
        verdict like any other probed backend."""
        model = RemoteAnthropic(model="claude-sonnet-4-6")
        self.assertFalse(model.health_checked_live)


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
