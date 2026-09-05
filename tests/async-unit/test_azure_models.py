"""
Tests for the Azure-hosted generative backends and model-name tracking.

Covers the Azure OpenAI / Claude backends (config, model lists, endpoint
normalization, 429 handling), the MLRouter name-stamping that lets the chooser
record readable model names, and the ENABLED_REMOTE_MODELS allow-list.
"""

import asyncio
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp

from fighthealthinsurance.ml.ml_models import (
    _CLAUDE_NO_SAMPLING_PREFIXES,
    ModelDescription,
    RemoteAzureClaude,
    RemoteAzureOpenAI,
    RemoteAzureOpenLike,
    RemoteFullOpenLike,
    RemoteModelLike,
)
from fighthealthinsurance.ml.ml_router import MLRouter
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


# Provider wording for a temperature rejection. The two surfaces phrase it
# differently, and both must reach _http_error_indicates_unsupported_temperature.
OPENAI_TEMPERATURE_REJECTION_TEXT = (
    "Unsupported value: 'temperature' does not support 0.7 with this "
    "model. Only the default (1) value is supported."
)
ANTHROPIC_TEMPERATURE_REJECTION_TEXT = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"temperature: Extra inputs are not permitted"}}'
)


class _FakeAiohttpResponse:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, json_data=None, status=200, headers=None, text=""):
        """Capture the canned JSON body, HTTP status, headers, and body text."""
        self._json_data = json_data or {}
        self.status = status
        self._headers = headers or {}
        self._text = text

    async def __aenter__(self):
        """Enter the ``async with`` response context."""
        return self

    async def __aexit__(self, *exc):
        """Exit the response context without suppressing exceptions."""
        return False

    async def json(self):
        """Return the canned JSON body."""
        return self._json_data

    async def text(self):
        """Return the canned body text (read by error-handling paths)."""
        return self._text

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


class _FakeSequencedSession:
    """Session stand-in returning queued responses, recording every POST body."""

    def __init__(self, responses, bodies):
        """Take the response queue and the list to append request bodies to."""
        self._responses = list(responses)
        self._bodies = bodies

    async def __aenter__(self):
        """Enter the ``async with`` session context."""
        return self

    async def __aexit__(self, *exc):
        """Exit the session context without suppressing exceptions."""
        return False

    def post(self, url, headers=None, json=None):
        """Record the body and return the next queued response."""
        self._bodies.append(json)
        return self._responses.pop(0)


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
        m = RemoteAzureOpenAI(model="gpt-5.5")
        self.assertEqual(m.model, "gpt-5.5")
        self.assertTrue(m.external)
        self.assertTrue(m.supports_system)
        self.assertEqual(m.get_max_context(), 128000)
        self.assertEqual(m.get_tier(), "frontier")

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
        """models() yields azure-openai/-prefixed entries, cheapest first.

        Only gpt-5.5 is provisioned on our sponsored resource, so the default
        list has one entry; the prefixing and ordering contract is unchanged
        and an env override can still add more.
        """
        models = RemoteAzureOpenAI.models()
        self.assertEqual(len(models), 1)
        self.assertTrue(all(m.name.startswith("azure-openai/") for m in models))
        costs = [m.cost for m in models]
        self.assertEqual(costs, sorted(costs))  # cheapest -> premium

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_models_use_latest_ids(self):
        """Azure Claude exposes exactly the deployments we have provisioned.

        Haiku 4.5 and Sonnet 4.6 were never deployed on our resource, so
        listing them produced entries that would 404 on first use.
        """
        names = {m.name for m in RemoteAzureClaude.models()}
        self.assertEqual(
            names,
            {
                "azure-anthropic/claude-opus-4-8",
                "azure-anthropic/claude-fable-5",
            },
        )

    @patch.dict(os.environ, {**AZURE_OPENAI_ENV, **AZURE_CLAUDE_ENV})
    def test_sponsored_gpt_leads_the_external_fanout(self):
        """The sponsored deployment must LEAD the external fan-out, which takes
        two independent properties, asserted separately here.

        First its proxy cost has to fall below the paid-external band, because
        ``external_models_by_cost`` is what feeds selection and anything sorting
        after the paid backends can never lead. Second it has to survive
        ``best_external_models``, which sorts by quality descending and only
        falls back to cost order for ties, so too low a tier drops it before
        cost is ever consulted. Asserting either alone lets the other regress
        silently and quietly hands traffic back to a paid backend.
        """
        gpt_desc = RemoteAzureOpenAI.models()[0]
        self.assertEqual(gpt_desc.internal_name, "gpt-5.5")

        # 20 is the bottom of the paid-external proxy band in this codebase. If
        # a paid backend is ever priced below it, this is the assertion that
        # should fail and force that decision back into the open rather than
        # letting paid traffic quietly take the lead.
        paid_external_floor = 20
        self.assertLess(
            gpt_desc.cost,
            paid_external_floor,
            "the sponsored deployment must undercut the paid-external band",
        )

        # And it must actually win the router's own selection against an
        # equally capable paid backend, not merely look cheaper in a table.
        gpt = RemoteAzureOpenAI(model="gpt-5.5")
        paid = MagicMock(spec=RemoteModelLike)
        paid.external = True
        paid.quality.return_value = gpt.quality()
        paid.is_available.return_value = True
        paid.health_checked_live = True

        router = MLRouter()
        router.external_models_by_cost = [gpt, paid]
        self.assertIs(router.best_external_models(limit=2)[0], gpt)

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_fable_is_the_priciest_deployment(self):
        """Fable 5 sorts last (priciest) in models()."""
        models = RemoteAzureClaude.models()
        costs = [m.cost for m in models]
        self.assertEqual(costs, sorted(costs))  # cheapest -> priciest
        self.assertEqual(models[-1].name, "azure-anthropic/claude-fable-5")
        self.assertEqual(models[-1].cost, 175)

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_claude_fable_outranks_opus_for_routing(self):
        """Fable is frontier-tier, which must outrank Opus's premium: cost
        only breaks ties WITHIN a tier, so an equal tier would sort the
        priciest model last and keep it out of the default fan-out."""
        fable = RemoteAzureClaude(model="claude-fable-5")
        opus = RemoteAzureClaude(model="claude-opus-4-8")
        self.assertEqual(fable.get_tier(), "frontier")
        self.assertEqual(opus.get_tier(), "premium")
        self.assertGreater(fable.quality(), opus.quality())
        # Still below the internal backends' floor, which stay preferred.
        self.assertLess(fable.quality(), 101)

    @patch.dict(
        os.environ,
        {**AZURE_CLAUDE_ENV, **AZURE_OPENAI_ENV, "ANTHROPIC_API_KEY": "test-key"},
    )
    def test_router_picks_fable_in_the_default_fan_out(self):
        """The tier is only worth anything if the router actually reaches the
        model: with every provider configured, Fable must survive the default
        limit=3 slice rather than sorting behind the premium models."""
        names = [str(m) for m in MLRouter().best_external_models(3)]
        self.assertIn("azure-anthropic/claude-fable-5", names)

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
        self.assertEqual([m.internal_name for m in models], ["gpt-5.5"])

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

    def _capture_chat_completions_body(self, model: str) -> dict:
        """Drive the OpenAI-compatible transport end-to-end against a fake
        session and return the captured request body."""
        capture: dict = {}

        async def run():
            """Run _infer with a canned 200 chat-completions response."""
            m = RemoteAzureOpenAI(model=model)
            response = _FakeAiohttpResponse(
                {"choices": [{"message": {"content": "Dear insurer, please."}}]}
            )
            session = _FakeAiohttpSession(response, capture)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                await m._infer(system_prompts=["be helpful"], prompt="appeal this")

        asyncio.run(run())
        return capture["json"]

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_gpt5_request_omits_temperature(self):
        """gpt-5-family deployments reject any non-default temperature with
        HTTP 400, so the request body must omit the field entirely."""
        body = self._capture_chat_completions_body("gpt-5")
        self.assertNotIn("temperature", body)

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_gpt5_mini_request_omits_temperature(self):
        """The gpt-5 prefix match covers the -mini variant too."""
        body = self._capture_chat_completions_body("gpt-5-mini")
        self.assertNotIn("temperature", body)

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_non_reasoning_model_request_keeps_temperature(self):
        """Non-reasoning deployments keep the router-supplied temperature."""
        body = self._capture_chat_completions_body("gpt-4.1-mini")
        self.assertIn("temperature", body)

    _TEMPERATURE_REJECTION_TEXT = OPENAI_TEMPERATURE_REJECTION_TEXT
    _OK_COMPLETION = {"choices": [{"message": {"content": "Dear insurer, please."}}]}

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_custom_deployment_temperature_rejection_retries_without(self):
        """A reasoning model behind a custom deployment name (which the
        name-based check can't recognize) must trigger a single retry with
        temperature stripped instead of failing with the 400."""

        async def run():
            """Queue a temperature-rejection 400 then a 200 and inspect bodies."""
            m = RemoteAzureOpenAI(model="appeals-prod")
            bodies: list = []
            session = _FakeSequencedSession(
                [
                    _FakeAiohttpResponse(
                        status=400, text=self._TEMPERATURE_REJECTION_TEXT
                    ),
                    _FakeAiohttpResponse(self._OK_COMPLETION),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(system_prompts=["x"], prompt="y")
            self.assertIsNotNone(result)
            self.assertEqual(len(bodies), 2)
            self.assertIn("temperature", bodies[0])
            self.assertNotIn("temperature", bodies[1])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_temperature_rejection_memoized_for_later_calls(self):
        """After one rejection the deployment is remembered: the next call's
        first attempt already omits temperature (no wasted 400 round-trip)."""

        async def run():
            """Run two calls through one queue and inspect the third body."""
            m = RemoteAzureOpenAI(model="appeals-prod")
            bodies: list = []
            session = _FakeSequencedSession(
                [
                    _FakeAiohttpResponse(
                        status=400, text=self._TEMPERATURE_REJECTION_TEXT
                    ),
                    _FakeAiohttpResponse(self._OK_COMPLETION),
                    _FakeAiohttpResponse(self._OK_COMPLETION),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                await m._infer(system_prompts=["x"], prompt="y")
                await m._infer(system_prompts=["x"], prompt="y")
            self.assertEqual(len(bodies), 3)
            self.assertNotIn("temperature", bodies[2])

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

    def _capture_messages_body(self, model: str, **infer_kwargs) -> dict:
        """Drive the Messages transport against a fake session and return the
        captured request body."""
        capture: dict = {}

        async def run():
            """Run _infer with a canned 200 Messages response."""
            m = RemoteAzureClaude(model=model)
            response = _FakeAiohttpResponse(
                {"content": [{"type": "text", "text": "ok"}]}
            )
            session = _FakeAiohttpSession(response, capture)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(
                    system_prompts=["x"], prompt="y", **infer_kwargs
                )
            # _infer swallows post-POST failures and returns None, which would
            # leave the captured body asserting over a crashed call.
            self.assertEqual(result, ("ok", []))

        asyncio.run(run())
        return capture["json"]

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_fable_request_omits_temperature(self):
        """Claude 4.7+ deployments (Fable 5 here) reject the sampling
        parameters with HTTP 400, so the body must omit temperature."""
        self.assertNotIn("temperature", self._capture_messages_body("claude-fable-5"))

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_opus_48_request_omits_temperature(self):
        """The same removal applies to the Opus 4.8 deployment."""
        self.assertNotIn("temperature", self._capture_messages_body("claude-opus-4-8"))

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_sonnet_46_request_keeps_temperature(self):
        """Claude 4.6 and older still accept a custom temperature."""
        body = self._capture_messages_body("claude-sonnet-4-6")
        self.assertEqual(body["temperature"], 0.7)

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_custom_claude_deployment_temperature_rejection_retries_without(self):
        """A sampling-free Claude behind a custom AZURE_ANTHROPIC_MODELS name
        can't be recognized by name, so the Messages API's own rejection
        wording must trigger a single retry with temperature stripped instead
        of failing the call."""

        async def run():
            """Queue a temperature-rejection 400 then a 200 and inspect bodies."""
            m = RemoteAzureClaude(model="appeals-fable")
            bodies: list = []
            session = _FakeSequencedSession(
                [
                    _FakeAiohttpResponse(
                        status=400, text=ANTHROPIC_TEMPERATURE_REJECTION_TEXT
                    ),
                    _FakeAiohttpResponse({"content": [{"type": "text", "text": "ok"}]}),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(system_prompts=["x"], prompt="y")
            self.assertEqual(result, ("ok", []))
            self.assertEqual(len(bodies), 2)
            self.assertIn("temperature", bodies[0])
            self.assertNotIn("temperature", bodies[1])
            # Remembered, so a later call skips the wasted 400 round-trip.
            self.assertIn("appeals-fable", m._temperature_unsupported_models)

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_error_body_does_not_overwrite_http_reason_phrase(self):
        """The provider body rides alongside ClientResponseError.message, never
        over it: model_health_check._categorize_http_error classifies a 400 as
        MODEL_NOT_FOUND when .message mentions "model", and every log site and
        the persisted attempt record interpolate it."""

        async def run():
            """Force a 400 whose body mentions "model" and inspect the error."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            response = _FakeAiohttpResponse(
                status=400,
                text="max_tokens: 8192 exceeds the maximum for this model",
            )
            session = _FakeAiohttpSession(response)
            with patch.object(aiohttp, "ClientSession", return_value=session):
                with self.assertRaises(aiohttp.ClientResponseError) as ctx:
                    await m._infer(system_prompts=["x"], prompt="y")
            self.assertEqual(ctx.exception.message, "error")
            self.assertIn("max_tokens", getattr(ctx.exception, "fhi_response_body"))

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_OPENAI_ENV)
    def test_gpt5_point_releases_drop_temperature(self):
        """The gpt-5 family 400s on a custom temperature, and it ships DOTTED
        point releases as well as hyphenated variants.

        The matcher recognised "gpt-5" and the "gpt-5-" prefix only, so every
        dotted release looked temperature-capable: the first call to it posted
        a temperature, ate an HTTP 400, and only then landed in the runtime
        fallback. Harmless when no dotted release was configured; gpt-5.5 is a
        shipped default now, so that rejected request is on the hot path.
        """
        m = RemoteAzureOpenAI(model="gpt-5.5")
        for name in (
            "gpt-5",
            "gpt-5-mini",
            "gpt-5.5",
            "gpt-5.1",
            "gpt-5.4-mini",
            "azure-openai/gpt-5.5",  # provider-prefixed registrations too
        ):
            with self.subTest(model=name):
                self.assertFalse(m._supports_custom_temperature(name))
        # Non-reasoning deployments must keep the parameter...
        self.assertTrue(m._supports_custom_temperature("gpt-4.1-mini"))
        # ...including operator-named deployments that merely start "gpt-5.".
        # Azure deployment names are chosen by whoever creates them, so a bare
        # prefix match would strip temperature from a non-reasoning model behind
        # a name like this, which is a silent wrong answer rather than a 400.
        for name in ("gpt-5.production", "gpt-5.legacy-mini", "gpt-5x"):
            with self.subTest(model=name):
                self.assertTrue(m._supports_custom_temperature(name))

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_every_no_sampling_prefix_is_recognized(self):
        """Each id in _CLAUDE_NO_SAMPLING_PREFIXES must actually be matched — a
        typo there ships silently and 400s every call to that deployment."""
        m = RemoteAzureClaude(model="claude-sonnet-4-6")
        for name in _CLAUDE_NO_SAMPLING_PREFIXES:
            with self.subTest(model=name):
                self.assertFalse(m._supports_custom_temperature(name))
                # Dated snapshot ids of the same model match too.
                self.assertFalse(m._supports_custom_temperature(f"{name}-20260115"))

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_full_appeal_does_not_fan_out_identical_legs(self):
        """parallel_infer explores with two temperatures; for a model that
        drops the field both legs would post the same body, so it must submit
        one leg instead of paying twice for an identical request."""
        with patch.object(
            RemoteAzureClaude, "_blocking_checked_infer", return_value=("x", None)
        ):
            fable = RemoteAzureClaude(model="claude-fable-5")
            sonnet = RemoteAzureClaude(model="claude-sonnet-4-6")
            executor = ThreadPoolExecutor(max_workers=2)
            try:
                kwargs = dict(
                    prompt="appeal this",
                    infer_type="full",
                    patient_context=None,
                    plan_context=None,
                    pubmed_context=None,
                    submit_executor=executor,
                )
                fable_calls = len(fable.parallel_infer(**kwargs))
                sonnet_calls = len(sonnet.parallel_infer(**kwargs))
            finally:
                executor.shutdown(wait=True)
        # Same system prompts either way, so the ratio is the temperature legs.
        self.assertEqual(sonnet_calls, 2 * fable_calls)

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_concurrent_legs_rejection_is_picked_up_mid_call(self):
        """parallel_infer fans out concurrently, so a sibling leg can record a
        rejection while this call is between system prompts. The memo is
        re-read per prompt so the next request skips the field instead of
        spending another 400 to learn what the process already knows."""

        async def run():
            """Have a sibling record the rejection after the first request."""
            m = RemoteAzureClaude(model="appeals-fable")
            bodies: list = []

            class _SiblingLearnsSession(_FakeSequencedSession):
                """Stand-in whose first POST simulates a concurrent leg
                recording the deployment as sampling-free."""

                def post(self, url, headers=None, json=None):
                    """Record the body, then poison the memo like a sibling."""
                    response = super().post(url, headers=headers, json=json)
                    m._temperature_unsupported_models.add("appeals-fable")
                    return response

            ok = {"content": [{"type": "text", "text": ""}]}
            session = _SiblingLearnsSession(
                [
                    _FakeAiohttpResponse(ok),
                    _FakeAiohttpResponse({"content": [{"type": "text", "text": "ok"}]}),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(system_prompts=["a", "b"], prompt="y")
            self.assertEqual(result, ("ok", []))
            self.assertEqual(len(bodies), 2)
            self.assertIn("temperature", bodies[0])
            self.assertNotIn("temperature", bodies[1])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_learned_rejection_skips_the_field_on_the_next_prompt(self):
        """A rejection learned on one system prompt stops the field being sent
        on the next one in the same call."""

        async def run():
            """Reject the first prompt's request, then inspect both bodies."""
            m = RemoteAzureClaude(model="appeals-fable")
            bodies: list = []
            ok = {"content": [{"type": "text", "text": ""}]}
            session = _FakeSequencedSession(
                [
                    # Prompt 1: rejection, then the stripped retry -- which
                    # returns no text, so _do_infer moves to prompt 2.
                    _FakeAiohttpResponse(
                        status=400, text=ANTHROPIC_TEMPERATURE_REJECTION_TEXT
                    ),
                    _FakeAiohttpResponse(ok),
                    _FakeAiohttpResponse({"content": [{"type": "text", "text": "ok"}]}),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                result = await m._infer(system_prompts=["a", "b"], prompt="y")
            self.assertEqual(result, ("ok", []))
            # 1st sent it, 1st retry stripped it, and the 2nd prompt never
            # sent it again -- three requests, not four.
            self.assertEqual(len(bodies), 3)
            self.assertIn("temperature", bodies[0])
            self.assertNotIn("temperature", bodies[1])
            self.assertNotIn("temperature", bodies[2])

        asyncio.run(run())

    @patch.dict(os.environ, AZURE_CLAUDE_ENV)
    def test_non_temperature_400_still_propagates_without_retrying(self):
        """A 400 that isn't a temperature rejection raises on the first
        attempt: the classifier matches loosely, so a mismatch would other-
        wise turn every unrelated 400 into two paid round-trips and poison
        the deployment's memo."""

        async def run():
            """Force an unrelated 400 and count the POSTs it produced."""
            m = RemoteAzureClaude(model="claude-sonnet-4-6")
            bodies: list = []
            session = _FakeSequencedSession(
                [
                    _FakeAiohttpResponse(status=400, text="invalid_request: nope"),
                    _FakeAiohttpResponse({"content": [{"type": "text", "text": "ok"}]}),
                ],
                bodies,
            )
            with patch.object(aiohttp, "ClientSession", return_value=session):
                with self.assertRaises(aiohttp.ClientResponseError):
                    await m._infer(system_prompts=["x"], prompt="y")
            self.assertEqual(len(bodies), 1)
            self.assertNotIn("claude-sonnet-4-6", m._temperature_unsupported_models)

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
