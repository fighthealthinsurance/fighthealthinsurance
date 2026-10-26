"""A second key for one provider: SECONDARY_<name> next to the key's own
variable (api_keys_from_env). The transport sends the primary key until the
provider refuses it for a rate limit or a spent quota, then sends the same
request with the fallback key before filing anything against the provider,
and keeps sending the fallback while the primary is passed over. With every
key refused, or one key configured, a refusal lands exactly as before.

No network: ClientSession.post is replaced with the fakes in conftest.py, as
in test_external_model_failures.py.
"""

import asyncio
import os
from unittest.mock import patch

import aiohttp
import pytest

from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_models import (
    ApiKeyRing,
    DeepInfra,
    RemoteAnthropic,
    RemoteAzureClaude,
    RemoteFullOpenLike,
    api_keys_from_env,
    secondary_env_name,
)

LONG_REPLY = (
    "Dear Insurance Company, I am writing to appeal the denial of coverage for "
    "the requested procedure, which is medically necessary given the documented "
    "diagnosis and history. " * 3
)
GOOD_JSON = {"choices": [{"message": {"role": "assistant", "content": LONG_REPLY}}]}
MESSAGES_JSON = {"content": [{"type": "text", "text": LONG_REPLY}]}
CREDIT_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"Your credit balance is too low to access the Anthropic API. Please go to '
    'Plans & Billing to upgrade or purchase credits."}}'
)
INVALID_KEY_BODY = '{"error":{"message":"Invalid API key","type":"invalid_api_key"}}'
TWO_ANTHROPIC_KEYS = {
    "ANTHROPIC_API_KEY": "first-key",
    "SECONDARY_ANTHROPIC_API_KEY": "second-key",
}
TWO_AZURE_CLAUDE_KEYS = {
    "AZURE_ANTHROPIC_API_KEY": "first-key",
    "SECONDARY_AZURE_ANTHROPIC_API_KEY": "second-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/anthropic",
}


class _SequencedPost:
    """ClientSession.post stand-in answering each call with the next queued
    response (built with make_fake_model_post), keeping each request's
    headers so a test can see which key went out."""

    def __init__(self, *posts):
        self._responses = [post._response for post in posts]
        self.headers = []
        self._current = None

    def __call__(self, url, *args, headers=None, json=None, **kwargs):
        self.headers.append(headers or {})
        self._current = self._responses.pop(0)
        return self

    async def __aenter__(self):
        return self._current

    async def __aexit__(self, *exc):
        return False

    @property
    def bearer_tokens(self):
        return [h.get("Authorization", "").removeprefix("Bearer ") for h in self.headers]

    @property
    def api_keys(self):
        return [h.get("x-api-key") for h in self.headers]


class _SequencedSession:
    """aiohttp.ClientSession stand-in for the Messages API transport, whose
    ``post`` is a _SequencedPost."""

    def __init__(self, post):
        self.post = post

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _default_windows(monkeypatch):
    """The cooldowns these tests count on are the defaults, whatever the
    shell running tox exports."""
    for name in ("FHI_KEY_QUOTA_COOLDOWN_SECONDS", "FHI_AUTH_REFUSAL_COOLDOWN_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    yield
    # A back-off a test leaves on a shared per-class limiter would skip other
    # tests' calls before they are sent.
    RemoteAnthropic._rate_limiters.pop("claude-sonnet-4-6", None)
    RemoteAzureClaude._rate_limiters.pop("claude-opus-4-8", None)


def _two_keys(api_base: str = "http://provider.example/v1") -> RemoteFullOpenLike:
    """An external OpenAI-compatible backend with a fallback key."""
    return RemoteFullOpenLike(api_base, ["first-key", "second-key"], "some-model")


def _anthropic(env=None) -> RemoteAnthropic:
    RemoteAnthropic._rate_limiters.pop("claude-sonnet-4-6", None)
    with patch.dict(os.environ, env or TWO_ANTHROPIC_KEYS):
        return RemoteAnthropic(model="claude-sonnet-4-6")


def _azure_claude() -> RemoteAzureClaude:
    RemoteAzureClaude._rate_limiters.pop("claude-opus-4-8", None)
    with patch.dict(os.environ, TWO_AZURE_CLAUDE_KEYS):
        return RemoteAzureClaude(model="claude-opus-4-8")


async def _answered(monkeypatch, backend, posts):
    monkeypatch.setattr(aiohttp.ClientSession, "post", posts)
    return await backend._infer(system_prompts=["You are helpful."], prompt="Hi")


class TestApiKeysFromEnv:
    def test_the_fallback_variable_is_secondary_plus_the_name(self):
        assert secondary_env_name("DEEPINFRA_API") == "SECONDARY_DEEPINFRA_API"

    def test_nothing_set_gives_no_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            assert api_keys_from_env("DEEPINFRA_API") == []

    def test_the_primary_alone_is_one_key(self):
        with patch.dict(os.environ, {"DEEPINFRA_API": "k1"}, clear=True):
            assert api_keys_from_env("DEEPINFRA_API") == ["k1"]

    def test_the_fallback_follows_the_primary(self):
        env = {"DEEPINFRA_API": "k1", "SECONDARY_DEEPINFRA_API": "k2"}
        with patch.dict(os.environ, env, clear=True):
            assert api_keys_from_env("DEEPINFRA_API") == ["k1", "k2"]

    def test_a_blank_fallback_is_unset(self):
        env = {"DEEPINFRA_API": "k1", "SECONDARY_DEEPINFRA_API": ""}
        with patch.dict(os.environ, env, clear=True):
            assert api_keys_from_env("DEEPINFRA_API") == ["k1"]

    def test_a_fallback_equal_to_the_primary_is_dropped(self):
        env = {"DEEPINFRA_API": "k1", "SECONDARY_DEEPINFRA_API": "k1"}
        with patch.dict(os.environ, env, clear=True):
            assert api_keys_from_env("DEEPINFRA_API") == ["k1"]

    def test_the_fallback_variable_can_be_named(self):
        env = {"DEEPINFRA_API": "k1", "DEEPINFRA_SPARE": "k2"}
        with patch.dict(os.environ, env, clear=True):
            assert api_keys_from_env("DEEPINFRA_API", "DEEPINFRA_SPARE") == ["k1", "k2"]

    def test_a_fallback_without_a_primary_registers_no_backend(self):
        with patch.dict(os.environ, {"SECONDARY_DEEPINFRA_API": "k2"}, clear=True):
            with pytest.raises(Exception, match="No token"):
                DeepInfra(model="google/gemma-4-26B-A4B-it")

    def test_a_backend_reads_both_keys(self):
        env = {"DEEPINFRA_API": "k1", "SECONDARY_DEEPINFRA_API": "k2"}
        with patch.dict(os.environ, env, clear=True):
            backend = DeepInfra(model="google/gemma-4-26B-A4B-it")
        assert backend._key_ring.keys == ["k1", "k2"]
        assert backend.token == "k1"


class TestApiKeyRing:
    def test_the_first_key_is_sent_first(self):
        assert ApiKeyRing(["k1", "k2"]).current == "k1"

    def test_a_spent_key_is_passed_over(self):
        ring = ApiKeyRing(["k1", "k2"])
        assert ring.mark_spent("k1", 60) == "k2"
        assert ring.current == "k2"
        assert ring.spent("k1")

    def test_the_only_key_is_never_passed_over(self):
        ring = ApiKeyRing("k1")
        assert ring.mark_spent("k1", 60) is None
        assert ring.current == "k1"
        assert not ring.spent("k1")

    def test_a_spent_key_returns_when_its_cooldown_ends(self):
        ring = ApiKeyRing(["k1", "k2"])
        ring.mark_spent("k1", 0)
        assert ring.current == "k1"

    def test_with_every_key_spent_the_soonest_to_recover_is_sent(self):
        ring = ApiKeyRing(["k1", "k2"])
        ring.mark_spent("k1", 100)
        assert ring.mark_spent("k2", 50) is None
        assert ring.current == "k2"

    def test_a_key_that_answers_is_sent_again_at_once(self):
        ring = ApiKeyRing(["k1", "k2"])
        ring.mark_spent("k1", 100)
        ring.note_served("k1")
        assert ring.current == "k1"

    @pytest.mark.parametrize("token", ["", None])
    def test_a_backend_without_a_key_keeps_sending_what_it_did(self, token):
        """Our own servers are built with token="": the header they send
        must not change."""
        assert ApiKeyRing(token).current == token
        assert len(ApiKeyRing(token)) == 0

    def test_blank_and_repeated_keys_are_dropped(self):
        assert ApiKeyRing(["k1", "", None, "k1", "k2"]).keys == ["k1", "k2"]

    def test_keys_are_named_by_position_not_value(self):
        ring = ApiKeyRing(["k1", "k2", "k3"])
        assert ring.position("k1") == "primary key"
        assert ring.position("k2") == "secondary key"
        assert ring.position("k3") == "key 3 of 3"
        assert ring.position("other") == "key"


class TestSharedTransport:
    """The OpenAI-compatible transport every hosted backend but Azure
    Claude infers through."""

    @pytest.mark.asyncio
    async def test_a_rate_limited_key_is_replaced_within_the_call(
        self, monkeypatch, make_fake_model_post
    ):
        posts = _SequencedPost(
            make_fake_model_post(429, "rate limited"),
            make_fake_model_post(200, json_data=GOOD_JSON),
        )
        result = await _answered(monkeypatch, _two_keys(), posts)
        assert result is not None and result[0] == LONG_REPLY
        assert posts.bearer_tokens == ["first-key", "second-key"]

    @pytest.mark.asyncio
    async def test_the_fallback_key_serves_the_next_call(
        self, monkeypatch, make_fake_model_post
    ):
        backend = _two_keys()
        await _answered(
            monkeypatch,
            backend,
            _SequencedPost(
                make_fake_model_post(429, "rate limited"),
                make_fake_model_post(200, json_data=GOOD_JSON),
            ),
        )
        posts = _SequencedPost(make_fake_model_post(200, json_data=GOOD_JSON))
        result = await _answered(monkeypatch, backend, posts)
        assert result is not None and result[0] == LONG_REPLY
        assert posts.bearer_tokens == ["second-key"]

    @pytest.mark.asyncio
    async def test_a_quota_refusal_moves_to_the_fallback_without_pausing_the_provider(
        self, monkeypatch, make_fake_model_post
    ):
        backend = _anthropic()
        posts = _SequencedPost(
            make_fake_model_post(400, CREDIT_BODY),
            make_fake_model_post(200, json_data=GOOD_JSON),
        )
        result = await _answered(monkeypatch, backend, posts)
        assert result is not None and result[0] == LONG_REPLY
        assert posts.bearer_tokens == ["first-key", "second-key"]
        assert not spend.paused(RemoteAnthropic.SPEND_PROVIDER, "*")

    @pytest.mark.asyncio
    async def test_a_quota_refused_key_is_passed_over_for_the_cooldown(
        self, monkeypatch, make_fake_model_post
    ):
        backend = _anthropic()
        await _answered(
            monkeypatch,
            backend,
            _SequencedPost(
                make_fake_model_post(400, CREDIT_BODY),
                make_fake_model_post(200, json_data=GOOD_JSON),
            ),
        )
        assert backend._key_ring.spent("first-key")
        assert backend.token == "second-key"

    @pytest.mark.asyncio
    async def test_with_every_key_refused_the_call_fails_as_before(
        self, monkeypatch, make_fake_model_post
    ):
        backend = _anthropic()
        posts = _SequencedPost(
            make_fake_model_post(429, "rate limited"),
            make_fake_model_post(429, "rate limited"),
        )
        assert await _answered(monkeypatch, backend, posts) is None
        assert posts.bearer_tokens == ["first-key", "second-key"]
        assert backend.rate_limiter.get_status()["exhausted"]

    @pytest.mark.asyncio
    async def test_with_every_key_out_of_credit_the_provider_is_paused(
        self, monkeypatch, make_fake_model_post
    ):
        backend = _anthropic()
        posts = _SequencedPost(
            make_fake_model_post(400, CREDIT_BODY),
            make_fake_model_post(400, CREDIT_BODY),
        )
        assert await _answered(monkeypatch, backend, posts) is None
        assert posts.bearer_tokens == ["first-key", "second-key"]
        assert spend.paused(RemoteAnthropic.SPEND_PROVIDER, "*")

    @pytest.mark.asyncio
    async def test_one_key_is_not_sent_twice(self, monkeypatch, make_fake_model_post):
        backend = _anthropic({"ANTHROPIC_API_KEY": "first-key"})
        posts = _SequencedPost(make_fake_model_post(429, "rate limited"))
        assert await _answered(monkeypatch, backend, posts) is None
        assert posts.bearer_tokens == ["first-key"]
        assert backend.rate_limiter.get_status()["exhausted"]

    @pytest.mark.asyncio
    async def test_a_refusal_that_is_not_about_quota_is_not_retried(
        self, monkeypatch, make_fake_model_post
    ):
        """A revoked key (401) is filed as a refusal, as it always was: the
        fallback is for a spent key, not a wrong one."""
        posts = _SequencedPost(make_fake_model_post(401, INVALID_KEY_BODY))
        assert await _answered(monkeypatch, _two_keys(), posts) is None
        assert posts.bearer_tokens == ["first-key"]

    @pytest.mark.asyncio
    async def test_the_switch_is_logged_by_position_never_by_value(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        posts = _SequencedPost(
            make_fake_model_post(429, "rate limited"),
            make_fake_model_post(200, json_data=GOOD_JSON),
        )
        with log_capture() as cap:
            await _answered(monkeypatch, _two_keys(), posts)
        switches = [m for m in cap.messages("WARNING") if "secondary key" in m]
        assert len(switches) == 1
        assert "primary key" in switches[0]
        assert "rate limit" in switches[0]
        assert "first-key" not in switches[0] and "second-key" not in switches[0]


class TestAzureClaudeTransport:
    """Azure AI Foundry's Claude speaks the Messages API on its own
    transport, which must turn the ring the same way."""

    @pytest.mark.asyncio
    async def test_a_rate_limited_key_is_replaced_within_the_call(
        self, make_fake_model_post
    ):
        posts = _SequencedPost(
            make_fake_model_post(429, "rate limited"),
            make_fake_model_post(200, json_data=MESSAGES_JSON),
        )
        backend = _azure_claude()
        with patch.object(
            aiohttp, "ClientSession", return_value=_SequencedSession(posts)
        ):
            result = await backend._infer(system_prompts=["sys"], prompt="Hi")
        assert result is not None and result[0] == LONG_REPLY
        assert posts.api_keys == ["first-key", "second-key"]

    @pytest.mark.asyncio
    async def test_with_every_key_refused_the_model_backs_off(
        self, make_fake_model_post
    ):
        posts = _SequencedPost(
            make_fake_model_post(429, "rate limited"),
            make_fake_model_post(429, "rate limited"),
        )
        backend = _azure_claude()
        with patch.object(
            aiohttp, "ClientSession", return_value=_SequencedSession(posts)
        ):
            result = await backend._infer(system_prompts=["sys"], prompt="Hi")
        assert result is None
        assert posts.api_keys == ["first-key", "second-key"]
        assert backend.rate_limiter.get_status()["exhausted"]

    @pytest.mark.asyncio
    async def test_a_quota_refusal_moves_to_the_fallback_without_pausing_the_provider(
        self, make_fake_model_post
    ):
        posts = _SequencedPost(
            make_fake_model_post(400, CREDIT_BODY),
            make_fake_model_post(200, json_data=MESSAGES_JSON),
        )
        backend = _azure_claude()
        with patch.object(
            aiohttp, "ClientSession", return_value=_SequencedSession(posts)
        ):
            result = await backend._infer(system_prompts=["sys"], prompt="Hi")
        assert result is not None and result[0] == LONG_REPLY
        assert posts.api_keys == ["first-key", "second-key"]
        assert not spend.paused(RemoteAzureClaude.SPEND_PROVIDER, "*")
