"""RemoteOpenAIDirect: OpenAI's own API (api.openai.com) as a paid backend.

It registers only with OPENAI_DIRECT_API_KEY, honors OPENAI_DIRECT_MODELS and
SECONDARY_OPENAI_DIRECT_API_KEY, ranks below every sponsored model in the
default fan-out, and sends an OpenAI chat completions request. No network:
ClientSession.post is replaced with a recording fake.
"""

import os
from unittest.mock import patch

import aiohttp
import pytest

from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_models import (
    DeepInfra,
    ModelDescription,
    RemoteAnthropic,
    RemoteAzureClaude,
    RemoteAzureOpenAI,
    RemoteOpenAIDirect,
)
from fighthealthinsurance.ml.ml_router import MLRouter

LONG_REPLY = (
    "Dear Insurance Company, I am writing to appeal the denial of coverage for "
    "the requested procedure, which is medically necessary given the documented "
    "diagnosis and history. " * 3
)
GOOD_JSON = {"choices": [{"message": {"role": "assistant", "content": LONG_REPLY}}]}
KEY_ENV = {"OPENAI_DIRECT_API_KEY": "direct-key"}
ALL_ENV = {
    **KEY_ENV,
    "AZURE_OPENAI_API_KEY": "k",
    "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/openai/v1",
    "AZURE_ANTHROPIC_API_KEY": "k",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/anthropic",
    "ANTHROPIC_API_KEY": "k",
    "DEEPINFRA_API": "k",
}
_ENV_NAMES = (
    "OPENAI_DIRECT_API_KEY",
    "OPENAI_DIRECT_MODELS",
    "SECONDARY_OPENAI_DIRECT_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    RemoteOpenAIDirect._rate_limiters.clear()
    yield
    RemoteOpenAIDirect._rate_limiters.clear()


class _RecordingPost:
    """ClientSession.post stand-in that keeps each request's url, headers and
    body and answers with the canned response."""

    def __init__(self, post):
        self._response = post._response
        self.requests = []

    def __call__(self, url, *args, headers=None, json=None, **kwargs):
        self.requests.append({"url": str(url), "headers": headers or {}, "json": json})
        return self

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class TestRegistration:
    def test_nothing_registers_without_the_key(self):
        assert RemoteOpenAIDirect.models() == []
        assert RemoteOpenAIDirect.config_status()[0] != "configured"

    def test_the_old_openai_key_does_not_register_it(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "other-use"}):
            assert RemoteOpenAIDirect.models() == []

    def test_the_key_alone_registers_gpt_5_5(self):
        with patch.dict(os.environ, KEY_ENV):
            models = RemoteOpenAIDirect.models()
            assert [(m.name, m.internal_name) for m in models] == [
                ("openai/gpt-5.5", "gpt-5.5")
            ]
            assert RemoteOpenAIDirect.config_status()[0] == "configured"

    def test_a_fallback_key_alone_registers_nothing(self):
        with patch.dict(os.environ, {"SECONDARY_OPENAI_DIRECT_API_KEY": "second"}):
            assert RemoteOpenAIDirect.models() == []

    def test_the_model_list_can_be_overridden(self):
        with patch.dict(
            os.environ, {**KEY_ENV, "OPENAI_DIRECT_MODELS": "gpt-5.5, gpt-5-mini"}
        ):
            names = [m.name for m in RemoteOpenAIDirect.models()]
            assert names == ["openai/gpt-5.5", "openai/gpt-5-mini"]
            assert RemoteOpenAIDirect(model="gpt-5.5").get_tier() == "premium"
            assert RemoteOpenAIDirect(model="gpt-5-mini").get_tier() == "custom"

    def test_it_is_an_external_paid_provider_that_can_be_paused(self):
        with patch.dict(os.environ, KEY_ENV):
            m = RemoteOpenAIDirect(model="gpt-5.5")
        assert m.external
        assert m.api_base == "https://api.openai.com/v1"
        assert RemoteOpenAIDirect.SPEND_PROVIDER == spend.OPENAI
        assert spend.OPENAI in spend.PROVIDERS
        assert spend.OPENAI in spend.UNBUDGETED

    def test_it_reads_the_fallback_key(self):
        with patch.dict(
            os.environ, {**KEY_ENV, "SECONDARY_OPENAI_DIRECT_API_KEY": "second"}
        ):
            m = RemoteOpenAIDirect(model="gpt-5.5")
        assert m._key_ring.keys == ["direct-key", "second"]


def _fan_out(env, limit=3):
    """The names MLRouter.best_external_models picks from every external
    provider ``env`` configures, with each one available."""
    descriptions = []
    with patch.dict(os.environ, env):
        for backend in (
            RemoteAzureOpenAI,
            RemoteAzureClaude,
            RemoteAnthropic,
            RemoteOpenAIDirect,
            DeepInfra,
        ):
            for d in backend.models():
                d.model = backend(model=d.internal_name)
                d.model.name = d.name
                descriptions.append(d)
    router = object.__new__(MLRouter)
    router.external_models_by_cost = [d.model for d in sorted(descriptions)]
    with patch.object(MLRouter, "_selectable", return_value=True):
        return [m.name for m in router.best_external_models(limit=limit)]


class TestFanOutPosition:
    def test_it_never_outranks_the_sponsored_or_opus_models(self):
        ranked = _fan_out(ALL_ENV, limit=10)
        position = ranked.index("openai/gpt-5.5")
        for stronger in (
            "azure-openai/gpt-5.5",
            "azure-anthropic/claude-fable-5",
            "anthropic/claude-opus-4-8",
            "azure-anthropic/claude-opus-4-8",
        ):
            assert ranked.index(stronger) < position
        assert position < ranked.index("deepseek-ai/DeepSeek-V4-Pro-0813")

    def test_with_everything_configured_it_is_not_in_the_default_three(self):
        assert "openai/gpt-5.5" not in _fan_out(ALL_ENV)

    def test_without_azure_openai_it_backs_up_the_remaining_models(self):
        env = {
            k: v
            for k, v in ALL_ENV.items()
            if not k.startswith("AZURE_") and k != "ANTHROPIC_API_KEY"
        }
        assert _fan_out(env)[0] == "openai/gpt-5.5"


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_it_posts_chat_completions_to_openai_with_the_key(
        self, monkeypatch, make_fake_model_post
    ):
        with patch.dict(os.environ, KEY_ENV):
            backend = RemoteOpenAIDirect(model="gpt-5.5")
            posts = _RecordingPost(make_fake_model_post(200, json_data=GOOD_JSON))
            monkeypatch.setattr(aiohttp.ClientSession, "post", posts)
            result = await backend._infer(
                system_prompts=["You are helpful."], prompt="Hi"
            )
        assert result is not None and result[0] == LONG_REPLY
        request = posts.requests[0]
        assert request["url"] == "https://api.openai.com/v1/chat/completions"
        assert request["headers"]["Authorization"] == "Bearer direct-key"
        assert request["json"]["model"] == "gpt-5.5"
        roles = [m["role"] for m in request["json"]["messages"]]
        assert roles[0] == "system" and roles[-1] == "user"
