"""
Tests for the Azure-hosted generative backends and model-name tracking.

Covers:
- RemoteAzureOpenAI / RemoteAzureClaude initialization and configuration
- Endpoint normalization, default + overridable model lists, cost tiers
- Rate-limit checking and 429 handling
- MLRouter stamping friendly names onto model instances (so the chooser
  workflow records readable names instead of object reprs)
- The ENABLED_REMOTE_MODELS allow-list
"""

import asyncio
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp

from fighthealthinsurance.ml.ml_models import (
    ModelDescription,
    RemoteAzureClaude,
    RemoteAzureOpenAI,
    RemoteAzureOpenLike,
    RemoteFullOpenLike,
    RemoteModelLike,
)
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.utils import RateLimiter

AZURE_OPENAI_ENV = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/openai/v1",
}
AZURE_CLAUDE_ENV = {
    "AZURE_ANTHROPIC_API_KEY": "test-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/openai/v1",
}


def _clear_azure_env():
    for k in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_MODELS",
        "AZURE_ANTHROPIC_API_KEY",
        "AZURE_ANTHROPIC_ENDPOINT",
        "AZURE_ANTHROPIC_MODELS",
        "ENABLED_REMOTE_MODELS",
    ):
        os.environ.pop(k, None)


class TestRemoteAzureOpenAIInit(unittest.TestCase):
    """Initialization of the Azure OpenAI backend."""

    def setUp(self):
        RemoteAzureOpenAI._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_init_with_valid_config(self):
        model = RemoteAzureOpenAI(model="gpt-5")
        self.assertEqual(model.model, "gpt-5")
        self.assertTrue(model.supports_system)
        self.assertTrue(model.external)
        self.assertEqual(model.get_max_context(), 128000)

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_init_creates_rate_limiter(self):
        model = RemoteAzureOpenAI(model="gpt-5")
        self.assertIn("gpt-5", RemoteAzureOpenAI._rate_limiters)
        self.assertIsInstance(model.rate_limiter, RateLimiter)

    @patch.dict(
        os.environ,
        {"AZURE_OPENAI_ENDPOINT": AZURE_OPENAI_ENV["AZURE_OPENAI_ENDPOINT"]},
        clear=True,
    )
    def test_init_without_api_key_raises(self):
        with self.assertRaises(EnvironmentError) as ctx:
            RemoteAzureOpenAI(model="gpt-5")
        self.assertIn("AZURE_OPENAI_API_KEY", str(ctx.exception))

    @patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}, clear=True)
    def test_init_without_endpoint_raises(self):
        with self.assertRaises(EnvironmentError) as ctx:
            RemoteAzureOpenAI(model="gpt-5")
        self.assertIn("AZURE_OPENAI_ENDPOINT", str(ctx.exception))

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test-key",
            # Operator pasted the full completions URL with a trailing slash.
            "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/openai/v1/chat/completions/",
        },
    )
    def test_endpoint_normalization_strips_completions_suffix(self):
        model = RemoteAzureOpenAI(model="gpt-5")
        self.assertEqual(model.api_base, "https://res.openai.azure.com/openai/v1")


class TestRemoteAzureClaudeInit(unittest.TestCase):
    """Initialization of the Azure AI Foundry (Claude) backend."""

    def setUp(self):
        RemoteAzureClaude._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_init_with_valid_config(self):
        model = RemoteAzureClaude(model="claude-opus-4-8")
        self.assertEqual(model.model, "claude-opus-4-8")
        self.assertTrue(model.external)
        self.assertEqual(model.get_max_context(), 200000)

    @patch.dict(os.environ, {"AZURE_ANTHROPIC_API_KEY": "k"}, clear=True)
    def test_init_without_endpoint_raises(self):
        with self.assertRaises(EnvironmentError) as ctx:
            RemoteAzureClaude(model="claude-opus-4-8")
        self.assertIn("AZURE_ANTHROPIC_ENDPOINT", str(ctx.exception))

    def test_providers_use_separate_rate_limiter_state(self):
        """Each concrete provider must own its rate-limiter dict."""
        self.assertIsNot(
            RemoteAzureOpenAI._rate_limiters, RemoteAzureClaude._rate_limiters
        )


class TestAzureModels(unittest.TestCase):
    """models() class method behavior for both Azure providers."""

    def setUp(self):
        _clear_azure_env()

    def tearDown(self):
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_openai_models_have_prefix(self):
        models = RemoteAzureOpenAI.models()
        self.assertEqual(len(models), 3)
        for m in models:
            self.assertTrue(
                m.name.startswith("azure-openai/"),
                f"{m.name} should start with azure-openai/",
            )

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_models_have_prefix_and_latest_ids(self):
        models = RemoteAzureClaude.models()
        names = {m.name for m in models}
        self.assertIn("azure-anthropic/claude-opus-4-8", names)
        self.assertIn("azure-anthropic/claude-sonnet-4-6", names)
        self.assertIn("azure-anthropic/claude-haiku-4-5", names)

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_openai_models_cost_ordering(self):
        models = {m.name: m for m in RemoteAzureOpenAI.models()}
        self.assertLess(
            models["azure-openai/gpt-4.1-mini"].cost,
            models["azure-openai/gpt-5-mini"].cost,
        )
        self.assertLess(
            models["azure-openai/gpt-5-mini"].cost,
            models["azure-openai/gpt-5"].cost,
        )

    def test_models_empty_without_env(self):
        # Neither key nor endpoint set.
        self.assertEqual(RemoteAzureOpenAI.models(), [])
        self.assertEqual(RemoteAzureClaude.models(), [])

    @patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "k"}, clear=True)
    def test_models_empty_with_key_but_no_endpoint(self):
        self.assertEqual(RemoteAzureOpenAI.models(), [])

    @patch.dict(
        os.environ,
        {**AZURE_OPENAI_ENV, "AZURE_OPENAI_MODELS": "gpt-5, my-custom-deploy "},
    )
    def test_models_override_via_env(self):
        models = RemoteAzureOpenAI.models()
        names = [m.name for m in models]
        self.assertEqual(names, ["azure-openai/gpt-5", "azure-openai/my-custom-deploy"])
        # Internal (deployment) name is the bare override entry, trimmed.
        internal = {m.internal_name for m in models}
        self.assertEqual(internal, {"gpt-5", "my-custom-deploy"})

    def test_base_class_yields_no_models(self):
        """The shared base carries no configuration and registers nothing."""
        self.assertEqual(RemoteAzureOpenLike.models(), [])


class TestAzureTiers(unittest.TestCase):
    def setUp(self):
        RemoteAzureOpenAI._rate_limiters.clear()
        RemoteAzureClaude._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_default_tier(self):
        self.assertEqual(RemoteAzureOpenAI(model="gpt-5").get_tier(), "premium")
        self.assertEqual(RemoteAzureOpenAI(model="gpt-4.1-mini").get_tier(), "speed")

    @patch.dict(os.environ, {**AZURE_OPENAI_ENV, "AZURE_OPENAI_MODELS": "foo-deploy"})
    def test_override_tier_is_custom(self):
        self.assertEqual(RemoteAzureOpenAI(model="foo-deploy").get_tier(), "custom")


class TestAzureInfer(unittest.TestCase):
    """Rate-limit and 429 handling for Azure backends."""

    def setUp(self):
        RemoteAzureOpenAI._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_infer_checks_rate_limit(self):
        async def run():
            model = RemoteAzureOpenAI(model="gpt-5")
            model.rate_limiter.mark_exhausted(60.0)
            result = await model._infer(
                system_prompts=["You are helpful."], prompt="Test"
            )
            self.assertIsNone(result)

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_infer_handles_429(self):
        async def run():
            model = RemoteAzureOpenAI(model="gpt-5")
            error = aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=429,
                message="Rate limited",
                headers={"Retry-After": "120"},
            )
            # super()._infer resolves to RemoteFullOpenLike (the base's parent).
            with patch.object(
                RemoteAzureOpenLike.__bases__[0],
                "_infer",
                new_callable=AsyncMock,
                side_effect=error,
            ):
                result = await model._infer(
                    system_prompts=["You are helpful."], prompt="Test"
                )
            self.assertIsNone(result)
            self.assertTrue(model.rate_limiter.get_status()["exhausted"])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_infer_other_http_errors_propagate(self):
        async def run():
            model = RemoteAzureOpenAI(model="gpt-5")
            error = aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Server error",
                headers={},
            )
            with patch.object(
                RemoteAzureOpenLike.__bases__[0],
                "_infer",
                new_callable=AsyncMock,
                side_effect=error,
            ):
                with self.assertRaises(aiohttp.ClientResponseError):
                    await model._infer(
                        system_prompts=["You are helpful."], prompt="Test"
                    )

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_model_is_ok(self):
        model = RemoteAzureOpenAI(model="gpt-5")
        self.assertTrue(model.model_is_ok())
        model.rate_limiter.mark_exhausted(60.0)
        self.assertFalse(model.model_is_ok())


# ---------------------------------------------------------------------------
# Model-name tracking: router stamping, __str__, ENABLED_REMOTE_MODELS
# ---------------------------------------------------------------------------


class _FakeModel(RemoteModelLike):
    """Minimal concrete model used to drive MLRouter in tests."""

    def __init__(self, model):
        self.model = model

    @property
    def external(self):
        return False

    @property
    def context_only(self):
        return False

    async def _infer(self, *args, **kwargs):
        return None


class _FakeBackend:
    """Backend stub whose models() returns pre-built _FakeModel instances."""

    @classmethod
    def models(cls):
        return [
            ModelDescription(
                cost=10,
                name="fake/alpha",
                internal_name="alpha-int",
                model=_FakeModel("alpha-int"),
            ),
            ModelDescription(
                cost=20,
                name="fake/beta",
                internal_name="beta-int",
                model=_FakeModel("beta-int"),
            ),
        ]


class TestModelNameTracking(unittest.TestCase):
    """The router must stamp friendly names so the chooser records them."""

    def setUp(self):
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def tearDown(self):
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def test_router_stamps_friendly_name_on_instances(self):
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeBackend],
        ):
            router = MLRouter()

        # Both friendly names registered.
        self.assertIn("fake/alpha", router.models_by_name)
        self.assertIn("fake/beta", router.models_by_name)

        # Each instance now carries its friendly name (what the chooser records
        # via getattr(model, "name", str(model))).
        for name, instances in router.models_by_name.items():
            for inst in instances:
                self.assertEqual(getattr(inst, "name", None), name)

    def test_chooser_style_lookup_returns_friendly_name(self):
        """Reproduce chooser_tasks.py's getattr(model, "name", str(model))."""
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeBackend],
        ):
            router = MLRouter()

        model = router.internal_models_by_cost[0]
        recorded = getattr(model, "name", str(model))
        self.assertEqual(recorded, "fake/alpha")
        # Must not be an opaque object repr.
        self.assertNotIn("object at 0x", recorded)

    def test_str_returns_friendly_name_when_stamped(self):
        stamped = _FakeModel("alpha-int")
        stamped.name = "fake/alpha"
        self.assertEqual(str(stamped), "fake/alpha")

    def test_str_descriptive_when_unstamped(self):
        unstamped = _FakeModel("alpha-int")
        rendered = str(unstamped)
        self.assertNotIn("object at 0x", rendered)
        self.assertIn("alpha-int", rendered)


class TestEnabledRemoteModels(unittest.TestCase):
    """ENABLED_REMOTE_MODELS restricts which models load."""

    def setUp(self):
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def tearDown(self):
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def test_unset_enables_all(self):
        self.assertIsNone(MLRouter._enabled_model_names())
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeBackend],
        ):
            router = MLRouter()
        self.assertIn("fake/alpha", router.models_by_name)
        self.assertIn("fake/beta", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "fake/alpha"})
    def test_allow_list_filters_by_friendly_name(self):
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeBackend],
        ):
            router = MLRouter()
        self.assertIn("fake/alpha", router.models_by_name)
        self.assertNotIn("fake/beta", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "beta-int"})
    def test_allow_list_also_matches_internal_name(self):
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeBackend],
        ):
            router = MLRouter()
        self.assertIn("fake/beta", router.models_by_name)
        self.assertNotIn("fake/alpha", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "  "})
    def test_blank_value_enables_all(self):
        self.assertIsNone(MLRouter._enabled_model_names())


if __name__ == "__main__":
    unittest.main()
