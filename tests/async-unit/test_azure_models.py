"""
Tests for the Azure-hosted generative backends and model-name tracking.

Covers the Azure OpenAI / Claude backends (config, model lists, endpoint
normalization, 429 handling), the MLRouter name-stamping that lets the chooser
record readable model names, and the ENABLED_REMOTE_MODELS allow-list.
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

AZURE_OPENAI_ENV = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/openai/v1",
}
AZURE_CLAUDE_ENV = {
    "AZURE_ANTHROPIC_API_KEY": "test-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/anthropic",
}


def _clear_azure_env():
    """Remove all Azure/allow-list env vars so tests start from a clean slate."""
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


class _FakeAiohttpResponse:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, json_data=None, status=200, headers=None):
        """Capture the canned JSON body, HTTP status, and response headers."""
        self._json_data = json_data or {}
        self.status = status
        self._headers = headers or {}

    async def __aenter__(self):
        """Enter the ``async with`` response context."""
        return self

    async def __aexit__(self, *exc):
        """Exit the response context without suppressing exceptions."""
        return False

    async def json(self):
        """Return the canned JSON body."""
        return self._json_data

    def raise_for_status(self):
        """Raise ClientResponseError for >=400 statuses (like aiohttp)."""
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=self.status,
                message="error",
                headers=self._headers,
            )


class _FakeAiohttpSession:
    """Stand-in for aiohttp.ClientSession that records the POST arguments."""

    def __init__(self, response, capture=None):
        """Wrap the response to return and an optional dict to record args in."""
        self._response = response
        self._capture = capture if capture is not None else {}

    async def __aenter__(self):
        """Enter the ``async with`` session context."""
        return self

    async def __aexit__(self, *exc):
        """Exit the session context without suppressing exceptions."""
        return False

    def post(self, url, headers=None, json=None):
        """Record the request and return the canned response context manager."""
        self._capture["url"] = url
        self._capture["headers"] = headers
        self._capture["json"] = json
        return self._response


class TestAzureBackends(unittest.TestCase):
    """Config, model registration, and endpoint handling for both providers."""

    def setUp(self):
        """Reset rate-limiter state and Azure env vars before each test."""
        RemoteAzureOpenAI._rate_limiters.clear()
        RemoteAzureClaude._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        """Clear Azure env vars set during the test."""
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_openai_init(self):
        """Azure OpenAI init wires model, external/system flags, context, tier."""
        m = RemoteAzureOpenAI(model="gpt-5")
        self.assertEqual(m.model, "gpt-5")
        self.assertTrue(m.external)
        self.assertTrue(m.supports_system)
        self.assertEqual(m.get_max_context(), 128000)
        self.assertEqual(m.get_tier(), "premium")

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_init(self):
        """Azure Claude init is external and uses the 200K context window."""
        m = RemoteAzureClaude(model="claude-opus-4-8")
        self.assertTrue(m.external)
        self.assertEqual(m.get_max_context(), 200000)

    @patch.dict(
        os.environ,
        {"AZURE_OPENAI_ENDPOINT": AZURE_OPENAI_ENV["AZURE_OPENAI_ENDPOINT"]},
        clear=True,
    )
    def test_missing_key_raises(self):
        """Missing API key raises EnvironmentError naming the key env var."""
        with self.assertRaises(EnvironmentError) as ctx:
            RemoteAzureOpenAI(model="gpt-5")
        self.assertIn("AZURE_OPENAI_API_KEY", str(ctx.exception))

    @patch.dict(os.environ, {"AZURE_ANTHROPIC_API_KEY": "k"}, clear=True)
    def test_missing_endpoint_raises(self):
        """Missing endpoint raises EnvironmentError naming the endpoint env var."""
        with self.assertRaises(EnvironmentError) as ctx:
            RemoteAzureClaude(model="claude-opus-4-8")
        self.assertIn("AZURE_ANTHROPIC_ENDPOINT", str(ctx.exception))

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test-key",
            # Operator pasted the full completions URL with a trailing slash.
            "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/openai/v1/chat/completions/",
        },
    )
    def test_endpoint_normalized(self):
        """A pasted /chat/completions URL is normalized back to the base."""
        m = RemoteAzureOpenAI(model="gpt-5")
        self.assertEqual(m.api_base, "https://res.openai.azure.com/openai/v1")

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_openai_models_prefixed_and_cost_ordered(self):
        """models() yields azure-openai/-prefixed entries, cheapest first."""
        models = RemoteAzureOpenAI.models()
        self.assertEqual(len(models), 3)
        self.assertTrue(all(m.name.startswith("azure-openai/") for m in models))
        costs = [m.cost for m in models]
        self.assertEqual(costs, sorted(costs))  # cheapest -> premium

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_models_use_latest_ids(self):
        """Azure Claude exposes the latest Haiku/Sonnet/Opus-4.8 deployments."""
        names = {m.name for m in RemoteAzureClaude.models()}
        self.assertEqual(
            names,
            {
                "azure-anthropic/claude-haiku-4-5",
                "azure-anthropic/claude-sonnet-4-6",
                "azure-anthropic/claude-opus-4-8",
            },
        )

    def test_models_disabled_without_env(self):
        """Without env config, providers (and the base) register no models."""
        # No key/endpoint -> providers register nothing; base never registers.
        self.assertEqual(RemoteAzureOpenAI.models(), [])
        self.assertEqual(RemoteAzureClaude.models(), [])
        self.assertEqual(RemoteAzureOpenLike.models(), [])

    @patch.dict(
        os.environ,
        {**AZURE_OPENAI_ENV, "AZURE_OPENAI_MODELS": "gpt-5, my-deploy "},
    )
    def test_models_env_override(self):
        """AZURE_OPENAI_MODELS overrides the deployment list (tier=custom)."""
        models = RemoteAzureOpenAI.models()
        self.assertEqual([m.internal_name for m in models], ["gpt-5", "my-deploy"])
        self.assertEqual(
            [m.name for m in models],
            ["azure-openai/gpt-5", "azure-openai/my-deploy"],
        )
        # An overridden (non-default) deployment reports the "custom" tier.
        self.assertEqual(RemoteAzureOpenAI(model="my-deploy").get_tier(), "custom")

    @patch.dict(
        os.environ,
        {**AZURE_OPENAI_ENV, "AZURE_OPENAI_MODELS": " , , "},
    )
    def test_models_env_override_blank_falls_back_to_defaults(self):
        """A separators-only AZURE_OPENAI_MODELS (no real names) falls back to
        the defaults instead of silently disabling the provider."""
        models = RemoteAzureOpenAI.models()
        self.assertEqual(
            [m.internal_name for m in models],
            ["gpt-4.1-mini", "gpt-5-mini", "gpt-5"],
        )

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_model_is_ok(self):
        """model_is_ok() is True when configured, False once rate limited."""
        m = RemoteAzureOpenAI(model="gpt-5")
        self.assertTrue(m.model_is_ok())
        m.rate_limiter.mark_exhausted(60.0)
        self.assertFalse(m.model_is_ok())


class TestAzureInfer(unittest.TestCase):
    """Rate-limit and 429 handling for Azure backends."""

    def setUp(self):
        """Reset rate-limiter state and Azure env vars before each test."""
        RemoteAzureOpenAI._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        """Clear Azure env vars set during the test."""
        _clear_azure_env()

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_skips_when_rate_limited(self):
        """_infer short-circuits to None while the limiter is backing off."""

        async def run():
            """Exhaust the limiter, then assert _infer returns None."""
            m = RemoteAzureOpenAI(model="gpt-5")
            m.rate_limiter.mark_exhausted(60.0)
            self.assertIsNone(await m._infer(system_prompts=["x"], prompt="y"))

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_429_backs_off(self):
        """A 429 marks the limiter exhausted (honoring Retry-After)."""

        async def run():
            """Force a 429 from the parent _infer and assert back-off."""
            m = RemoteAzureOpenAI(model="gpt-5")
            error = aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=429,
                message="Rate limited",
                headers={"Retry-After": "120"},
            )
            # super()._infer resolves through RemoteFullOpenLike.
            with patch.object(
                RemoteFullOpenLike, "_infer", new_callable=AsyncMock, side_effect=error
            ):
                self.assertIsNone(await m._infer(system_prompts=["x"], prompt="y"))
            self.assertTrue(m.rate_limiter.get_status()["exhausted"])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_non_429_propagates(self):
        """Non-429 HTTP errors propagate rather than being swallowed."""

        async def run():
            """Force a 500 from the parent _infer and assert it raises."""
            m = RemoteAzureOpenAI(model="gpt-5")
            error = aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Server error",
                headers={},
            )
            with patch.object(
                RemoteFullOpenLike, "_infer", new_callable=AsyncMock, side_effect=error
            ):
                with self.assertRaises(aiohttp.ClientResponseError):
                    await m._infer(system_prompts=["x"], prompt="y")

        asyncio.run(run())


class TestAzureClaudeMessages(unittest.TestCase):
    """RemoteAzureClaude's Anthropic Messages API transport (Foundry)."""

    def setUp(self):
        """Reset Claude rate-limiter state and Azure env before each test."""
        RemoteAzureClaude._rate_limiters.clear()
        _clear_azure_env()

    def tearDown(self):
        """Clear Azure env vars set during the test."""
        _clear_azure_env()

    def test_normalize_endpoint_variants(self):
        """Endpoint normalization yields the …/anthropic base for every form."""
        norm = RemoteAzureClaude._normalize_endpoint
        base = "https://res.services.ai.azure.com/anthropic"
        self.assertEqual(norm(base), base)
        self.assertEqual(norm(base + "/"), base)
        # A pasted full target URI is trimmed back to the base.
        self.assertEqual(norm(base + "/v1/messages"), base)
        # A bare resource host gets the /anthropic segment appended.
        self.assertEqual(norm("https://res.services.ai.azure.com"), base)

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_messages_api_success(self):
        """_infer posts the Messages wire format to /anthropic/v1/messages and
        concatenates text content blocks from the response."""

        async def run():
            """Drive _infer against a fake 200 response and inspect the request."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            capture: dict = {}
            response = _FakeAiohttpResponse(
                {
                    "content": [
                        {"type": "text", "text": "Dear "},
                        {"type": "text", "text": "insurer"},
                    ]
                }
            )
            session = _FakeAiohttpSession(response, capture)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(
                    system_prompts=["be helpful"], prompt="write an appeal"
                )
            self.assertEqual(result, ("Dear insurer", []))
            self.assertEqual(
                capture["url"],
                "https://res.services.ai.azure.com/anthropic/v1/messages",
            )
            self.assertEqual(capture["headers"]["x-api-key"], "test-key")
            self.assertEqual(capture["headers"]["anthropic-version"], "2023-06-01")
            self.assertEqual(capture["json"]["model"], "claude-sonnet-4-6")
            # System prompt is a top-level field, not a system-role message.
            self.assertEqual(capture["json"]["system"], "be helpful")
            self.assertIn("max_tokens", capture["json"])
            self.assertTrue(
                all(msg["role"] != "system" for msg in capture["json"]["messages"])
            )

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_messages_api_clamps_temperature(self):
        """Temperature is clamped to the Messages API's [0, 1] range so a shared
        router value that's valid on the OpenAI surface (up to 2.0) can't 400."""

        async def run():
            """Pass an out-of-range temperature and inspect the request body."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            capture: dict = {}
            response = _FakeAiohttpResponse(
                {"content": [{"type": "text", "text": "ok"}]}
            )
            session = _FakeAiohttpSession(response, capture)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                await m._infer(system_prompts=["x"], prompt="y", temperature=1.8)
            self.assertEqual(capture["json"]["temperature"], 1.0)

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_messages_api_429_backs_off(self):
        """A 429 from the Messages endpoint marks the limiter exhausted."""

        async def run():
            """Force a 429 response and assert back-off + None result."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            response = _FakeAiohttpResponse(status=429, headers={"Retry-After": "90"})
            session = _FakeAiohttpSession(response)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                self.assertIsNone(await m._infer(system_prompts=["x"], prompt="y"))
            self.assertTrue(m.rate_limiter.get_status()["exhausted"])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_messages_api_non_429_propagates(self):
        """Non-429 HTTP errors propagate rather than being swallowed."""

        async def run():
            """Force a 500 response and assert it raises ClientResponseError."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            response = _FakeAiohttpResponse(status=500, headers={})
            session = _FakeAiohttpSession(response)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                with self.assertRaises(aiohttp.ClientResponseError):
                    await m._infer(system_prompts=["x"], prompt="y")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Model-name tracking: router stamping, __str__, ENABLED_REMOTE_MODELS
# ---------------------------------------------------------------------------


class _FakeModel(RemoteModelLike):
    """Minimal concrete model used to drive MLRouter in tests."""

    def __init__(self, model, external=False, context_only=False):
        """Build a fake model with configurable external/context_only flags."""
        self.model = model
        self._external = external
        self._context_only = context_only

    @property
    def external(self):
        """Whether this fake is treated as an external (remote) model."""
        return self._external

    @property
    def context_only(self):
        """Whether this fake is a context-only (citation) model."""
        return self._context_only

    async def _infer(self, *args, **kwargs):
        """No-op inference; the router tests never call it."""
        return None


class _FakeBackend:
    """Backend stub of local/internal models (for name-tracking tests)."""

    @classmethod
    def models(cls):
        """Return two local (internal) fake models."""
        return [
            ModelDescription(
                cost=10,
                name="fake/alpha",
                internal_name="alpha-int",
                model=_FakeModel("alpha-int", external=False),
            ),
            ModelDescription(
                cost=20,
                name="fake/beta",
                internal_name="beta-int",
                model=_FakeModel("beta-int", external=False),
            ),
        ]


class _FakeMixedBackend:
    """Backend stub with remote (external) generation models, a local model,
    and a context-only model -- to verify ENABLED_REMOTE_MODELS gates only
    remote generation models."""

    @classmethod
    def models(cls):
        """Return two remote, one local, and one context-only fake model."""
        return [
            ModelDescription(
                cost=10,
                name="remote/alpha",
                internal_name="ralpha-int",
                model=_FakeModel("ralpha-int", external=True),
            ),
            ModelDescription(
                cost=20,
                name="remote/beta",
                internal_name="rbeta-int",
                model=_FakeModel("rbeta-int", external=True),
            ),
            ModelDescription(
                cost=5,
                name="local/keep",
                internal_name="lkeep-int",
                model=_FakeModel("lkeep-int", external=False),
            ),
            ModelDescription(
                cost=5,
                name="ctx/cite",
                internal_name="ctx-int",
                model=_FakeModel("ctx-int", external=True, context_only=True),
            ),
        ]


class TestModelNameTracking(unittest.TestCase):
    """The router must stamp friendly names so the chooser records them."""

    def setUp(self):
        """Clear ENABLED_REMOTE_MODELS before each test."""
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def tearDown(self):
        """Clear ENABLED_REMOTE_MODELS set during the test."""
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def test_router_stamps_friendly_name_on_instances(self):
        """The router stamps each model instance with its friendly name."""
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
        """str(model) returns the stamped friendly name."""
        stamped = _FakeModel("alpha-int")
        stamped.name = "fake/alpha"
        self.assertEqual(str(stamped), "fake/alpha")

    def test_str_descriptive_when_unstamped(self):
        """An unstamped model still renders a descriptive (non-repr) string."""
        unstamped = _FakeModel("alpha-int")
        rendered = str(unstamped)
        self.assertNotIn("object at 0x", rendered)
        self.assertIn("alpha-int", rendered)


class TestEnabledRemoteModels(unittest.TestCase):
    """ENABLED_REMOTE_MODELS restricts which *remote* models load; local
    models are always enabled."""

    def setUp(self):
        """Clear ENABLED_REMOTE_MODELS before each test."""
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def tearDown(self):
        """Clear ENABLED_REMOTE_MODELS set during the test."""
        os.environ.pop("ENABLED_REMOTE_MODELS", None)

    def _build(self):
        """Build an MLRouter backed only by the mixed fake backend."""
        with patch(
            "fighthealthinsurance.ml.ml_router.candidate_model_backends",
            [_FakeMixedBackend],
        ):
            return MLRouter()

    def test_unset_enables_all(self):
        """With the var unset, every model (remote and local) is enabled."""
        self.assertIsNone(MLRouter._enabled_model_names())
        router = self._build()
        self.assertIn("remote/alpha", router.models_by_name)
        self.assertIn("remote/beta", router.models_by_name)
        self.assertIn("local/keep", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "remote/alpha"})
    def test_allow_list_filters_remote_by_friendly_name(self):
        """The allow-list drops unlisted remote models but keeps locals."""
        router = self._build()
        self.assertIn("remote/alpha", router.models_by_name)
        self.assertNotIn("remote/beta", router.models_by_name)
        # Local models load regardless of the allow-list.
        self.assertIn("local/keep", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "rbeta-int"})
    def test_allow_list_also_matches_internal_name(self):
        """The allow-list matches a model's internal name too, not just friendly."""
        router = self._build()
        self.assertIn("remote/beta", router.models_by_name)
        self.assertNotIn("remote/alpha", router.models_by_name)
        self.assertIn("local/keep", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "nonexistent/model"})
    def test_local_and_context_only_always_enabled_when_not_listed(self):
        """Local and context-only (citation) models load even though they are
        not in the allow-list, while unlisted remote models are dropped."""
        router = self._build()
        self.assertIn("local/keep", router.models_by_name)
        self.assertIn("ctx/cite", router.models_by_name)  # context-only, always on
        self.assertNotIn("remote/alpha", router.models_by_name)
        self.assertNotIn("remote/beta", router.models_by_name)

    @patch.dict(os.environ, {"ENABLED_REMOTE_MODELS": "  "})
    def test_blank_value_enables_all(self):
        """A blank/whitespace value is treated as 'no restriction' (None)."""
        self.assertIsNone(MLRouter._enabled_model_names())


if __name__ == "__main__":
    unittest.main()
