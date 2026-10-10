"""External models that stop working: a retired model, a refused key, an
account out of credit, a host that is gone.

The transport (ml_models) files each of these the first time it sees one, so
later calls are skipped without a request and the router stops picking the
model, while probes (raise_http_errors=True) still ask the provider live and
report what it says. No network: ClientSession.post is replaced with the
fakes in conftest.py (make_fake_model_post, log_capture), as in
test_missing_model_disable.py and test_transport_resilience.py.
"""

import asyncio
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest

from fighthealthinsurance.ml import ml_models, spend
from fighthealthinsurance.ml.ml_models import (
    DeepInfra,
    ProviderUnavailable,
    RemoteAnthropic,
    RemoteAzureClaude,
    RemoteFullOpenLike,
    RemotePerplexity,
    _connect_failed,
    _http_error_indicates_retired_model,
)

LONG_REPLY = (
    "Dear Insurance Company, I am writing to appeal the denial of coverage for "
    "the requested procedure, which is medically necessary given the documented "
    "diagnosis and history. " * 3
)
GOOD_JSON = {"choices": [{"message": {"role": "assistant", "content": LONG_REPLY}}]}
GONE_BODY = '{"error":{"message":"This model has been retired."}}'
INVALID_KEY_BODY = '{"error":{"message":"Invalid API key","type":"invalid_api_key"}}'
CREDIT_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"Your credit balance is too low to access the Anthropic API. Please go to '
    'Plans & Billing to upgrade or purchase credits."}}'
)
DEPLOYMENT_NOT_FOUND_BODY = (
    '{"error":{"code":"DeploymentNotFound","message":'
    '"The API deployment for this resource does not exist."}}'
)
AZURE_CLAUDE_ENV = {
    "AZURE_ANTHROPIC_API_KEY": "test-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/anthropic",
}


@pytest.fixture(autouse=True)
def _default_cooldown_windows(monkeypatch):
    """The windows these tests count on are the defaults, whatever the
    shell running tox exports. (The spend ledger, where a credit refusal
    pauses its provider, is reset around every test by tests/conftest.py.)"""
    for name in ("FHI_AUTH_REFUSAL_COOLDOWN_SECONDS", "FHI_TRANSPORT_COOLDOWN_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def _plain(api_base: str = "http://provider.example/v1") -> RemoteFullOpenLike:
    """An external OpenAI-compatible backend with no spend provider."""
    return RemoteFullOpenLike(api_base, "tok", "some-model")


class _OurServer(RemoteFullOpenLike):
    """One of our own model servers rather than a provider's."""

    @property
    def external(self):
        return False


def _deepinfra() -> DeepInfra:
    with patch.dict(os.environ, {"DEEPINFRA_API": "test-key"}):
        return DeepInfra(model="google/gemma-4-26B-A4B-it")


def _anthropic() -> RemoteAnthropic:
    # The rate limiters are per class and per model: a back-off another test
    # left on this one would skip the call before it is sent.
    RemoteAnthropic._rate_limiters.pop("claude-sonnet-4-6", None)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        return RemoteAnthropic(model="claude-sonnet-4-6")


def _azure_claude() -> RemoteAzureClaude:
    RemoteAzureClaude._rate_limiters.pop("claude-opus-4-8", None)
    with patch.dict(os.environ, AZURE_CLAUDE_ENV):
        return RemoteAzureClaude(model="claude-opus-4-8")


def _perplexity() -> RemotePerplexity:
    with patch.dict(os.environ, {"PERPLEXITY_API": "test-key"}):
        return RemotePerplexity(model="sonar")


async def _ask(model, **kwargs):
    return await model._infer(system_prompts=["sys"], prompt="hi", **kwargs)


async def _answered(monkeypatch, model, fake_post, **kwargs):
    """Ask ``model`` once with ``fake_post`` standing in for the provider."""
    monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)
    return await _ask(model, **kwargs)


async def _skip_reason(model) -> str:
    """The failure reason a call that should be skipped is recorded with."""
    with patch.object(ml_models, "record_ml_failure") as failure:
        await _ask(model)
    return failure.call_args.args[1]


class TestRetiredModelClassifier:
    @pytest.mark.parametrize(
        "status, body",
        [
            pytest.param(410, "", id="410-gone"),
            pytest.param(
                404,
                "The model gpt-3.5-turbo-0301 has been deprecated",
                id="openai-404-has-been-deprecated",
            ),
            pytest.param(
                400,
                '{"error":{"code":"ModelDeprecated","message":'
                '"This deployment can no longer be used."}}',
                id="azure-400-ModelDeprecated",
            ),
            pytest.param(
                400,
                '{"error":{"message":"Invalid model '
                "'llama-3.1-sonar-small-128k-online'. Permitted models can be "
                "found in the documentation at "
                'https://docs.perplexity.ai/guides/model-cards.",'
                '"type":"invalid_model","code":400}}',
                id="perplexity-400-invalid_model",
            ),
            pytest.param(
                400,
                '{"error":{"message":"The model `llama3-70b-8192` has been '
                'decommissioned and is no longer supported.",'
                '"type":"invalid_request_error","code":"model_decommissioned"}}',
                id="400-model_decommissioned",
            ),
            pytest.param(
                404,
                '{"error":{"message":"Model is not available"}}',
                id="404-model-is-not-available",
            ),
        ],
    )
    def test_a_retired_or_unknown_model_is_recognised(self, status, body):
        assert _http_error_indicates_retired_model(status, body)

    @pytest.mark.parametrize(
        "status, body",
        [
            # A deprecated parameter: the next request can avoid it, and
            # filing it would park a working model for an hour.
            pytest.param(
                400,
                "temperature is deprecated for this model",
                id="400-parameter-deprecated-for-this-model",
            ),
            pytest.param(
                400,
                "The model parameter max_tokens is deprecated",
                id="400-model-parameter-deprecated",
            ),
            pytest.param(400, "invalid model parameter", id="400-invalid-parameter"),
            # Regional or passing on a 400; only a 404 says it is gone.
            pytest.param(
                400,
                "Model is not available in your region",
                id="400-not-available-in-region",
            ),
            pytest.param(400, CREDIT_BODY, id="400-credit"),
            # A wrong URL, not a missing model.
            pytest.param(404, '{"detail":"Not Found"}', id="404-wrong-path"),
            pytest.param(401, "model has been deprecated", id="401-auth"),
        ],
    )
    def test_other_errors_are_not_read_as_a_retired_model(self, status, body):
        assert not _http_error_indicates_retired_model(status, body)


class TestRetiredModelIsParked:
    """DeepInfra answering 410 Gone for a model it retired."""

    @pytest.mark.asyncio
    async def test_a_410_returns_no_answer(self, monkeypatch, make_fake_model_post):
        result = await _answered(
            monkeypatch, _deepinfra(), make_fake_model_post(410, GONE_BODY)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_410_takes_the_model_out_of_routing(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        await _answered(monkeypatch, model, make_fake_model_post(410, GONE_BODY))
        assert model.is_available() is False

    @pytest.mark.asyncio
    async def test_the_reason_says_the_model_is_not_served(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        await _answered(monkeypatch, model, make_fake_model_post(410, GONE_BODY))
        assert "not served" in (model.unavailable_reason() or "")

    @pytest.mark.asyncio
    async def test_the_next_call_is_skipped_without_a_request(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        fake_post = make_fake_model_post(410, GONE_BODY)
        await _answered(monkeypatch, model, fake_post)

        reason = await _skip_reason(model)

        assert (fake_post.calls, reason) == (1, "skipped_missing_model")

    @pytest.mark.asyncio
    async def test_a_probe_still_asks_and_sees_the_410(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        fake_post = make_fake_model_post(410, GONE_BODY)
        await _answered(monkeypatch, model, fake_post)

        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            await _ask(model, raise_http_errors=True)

        assert (fake_post.calls, excinfo.value.status) == (2, 410)


class TestRefusedKeyCooldown:
    """A revoked key or disabled account (HTTP 401) is skipped for a while
    instead of being asked, and refused, on every request."""

    @pytest.mark.asyncio
    async def test_the_next_call_is_skipped_as_refused(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        fake_post = make_fake_model_post(401, INVALID_KEY_BODY)
        await _answered(monkeypatch, model, fake_post)

        reason = await _skip_reason(model)

        assert (fake_post.calls, reason) == (1, "skipped_refused")

    @pytest.mark.asyncio
    async def test_a_refused_model_is_out_of_routing(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        await _answered(monkeypatch, model, make_fake_model_post(401, INVALID_KEY_BODY))
        assert model.is_available() is False

    @pytest.mark.asyncio
    async def test_a_refusal_warns_once_and_never_errors(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        model = _plain()
        with log_capture() as cap:
            await _answered(
                monkeypatch, model, make_fake_model_post(401, INVALID_KEY_BODY)
            )
            await _ask(model)
        assert (len(cap.messages("WARNING")), cap.messages("ERROR")) == (1, [])

    @pytest.mark.asyncio
    async def test_a_probe_still_asks_a_refused_model(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        fake_post = make_fake_model_post(401, INVALID_KEY_BODY)
        await _answered(monkeypatch, model, fake_post)

        with pytest.raises(aiohttp.ClientResponseError):
            await _ask(model, raise_http_errors=True)

        assert fake_post.calls == 2

    @pytest.mark.asyncio
    async def test_a_probe_refused_does_not_start_the_cooldown(
        self, monkeypatch, make_fake_model_post
    ):
        """The deploy check reports what it sees; it must not take a model
        out of production routing for fifteen minutes on its own."""
        model = _plain()
        with pytest.raises(aiohttp.ClientResponseError):
            await _answered(
                monkeypatch,
                model,
                make_fake_model_post(401, INVALID_KEY_BODY),
                raise_http_errors=True,
            )
        assert model.is_available() is True

    @pytest.mark.asyncio
    async def test_after_the_window_the_next_call_asks_again(
        self, monkeypatch, make_fake_model_post
    ):
        """A rotated key or a reinstated account is picked up on its own."""
        model = _plain()
        fake_post = make_fake_model_post(401, INVALID_KEY_BODY)
        with patch.dict(os.environ, {"FHI_AUTH_REFUSAL_COOLDOWN_SECONDS": "0.05"}):
            await _answered(monkeypatch, model, fake_post)
        await asyncio.sleep(0.1)

        await _ask(model)

        assert fake_post.calls == 2


class TestCreditRefusal:
    """Anthropic answers an empty balance with a 400, not a 402 or 429."""

    @pytest.mark.asyncio
    async def test_it_pauses_the_provider_for_every_use(
        self, monkeypatch, make_fake_model_post
    ):
        await _answered(
            monkeypatch, _anthropic(), make_fake_model_post(400, CREDIT_BODY)
        )
        assert spend.paused(spend.ANTHROPIC, "*")

    @pytest.mark.asyncio
    async def test_it_returns_no_answer(self, monkeypatch, make_fake_model_post):
        result = await _answered(
            monkeypatch, _anthropic(), make_fake_model_post(400, CREDIT_BODY)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_it_raises_provider_unavailable_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        with pytest.raises(ProviderUnavailable):
            await _answered(
                monkeypatch,
                _anthropic(),
                make_fake_model_post(400, CREDIT_BODY),
                raise_on_unavailable=True,
            )

    @pytest.mark.asyncio
    async def test_the_next_call_is_skipped_as_budget(
        self, monkeypatch, make_fake_model_post
    ):
        model = _anthropic()
        fake_post = make_fake_model_post(400, CREDIT_BODY)
        await _answered(monkeypatch, model, fake_post)

        reason = await _skip_reason(model)

        assert (fake_post.calls, reason) == (1, "skipped_budget")


class TestAzureClaudeRefusals:
    """Claude on Azure speaks the Messages API through its own transport,
    which must file refusals the way the shared one does."""

    @pytest.mark.asyncio
    async def test_a_deployment_not_found_returns_no_answer(
        self, monkeypatch, make_fake_model_post
    ):
        result = await _answered(
            monkeypatch,
            _azure_claude(),
            make_fake_model_post(404, DEPLOYMENT_NOT_FOUND_BODY),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_deployment_not_found_is_counted_as_a_missing_model(
        self, monkeypatch, make_fake_model_post
    ):
        """Not as a bad request, where a retired deployment would hide."""
        with patch.object(ml_models, "record_ml_failure") as failure:
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(404, DEPLOYMENT_NOT_FOUND_BODY),
            )
        reasons = [c.args[1] for c in failure.call_args_list]
        assert reasons == ["missing_model"]

    @pytest.mark.asyncio
    async def test_a_deployment_not_found_marks_the_deployment(
        self, monkeypatch, make_fake_model_post
    ):
        model = _azure_claude()
        await _answered(
            monkeypatch, model, make_fake_model_post(404, DEPLOYMENT_NOT_FOUND_BODY)
        )
        assert model._model_marked_missing(model.api_base, model.model)

    @pytest.mark.asyncio
    async def test_a_deployment_not_found_raises_provider_unavailable_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        with pytest.raises(ProviderUnavailable, match="not served"):
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(404, DEPLOYMENT_NOT_FOUND_BODY),
                raise_on_unavailable=True,
            )

    @pytest.mark.asyncio
    async def test_a_credit_refusal_pauses_claude_on_azure(
        self, monkeypatch, make_fake_model_post
    ):
        await _answered(
            monkeypatch, _azure_claude(), make_fake_model_post(400, CREDIT_BODY)
        )
        assert spend.paused(spend.AZURE_ANTHROPIC, "*")

    @pytest.mark.asyncio
    async def test_a_credit_refusal_leaves_azure_openai_running(
        self, monkeypatch, make_fake_model_post
    ):
        """The sponsored Azure OpenAI deployments bill separately."""
        await _answered(
            monkeypatch, _azure_claude(), make_fake_model_post(400, CREDIT_BODY)
        )
        assert not spend.paused(spend.AZURE, "*")


class TestPerplexityHealth:
    """Perplexity lists no models, so its health is what inference saw."""

    def test_a_fresh_backend_is_ok(self):
        assert _perplexity().model_is_ok() is True

    @pytest.mark.asyncio
    async def test_a_refused_key_makes_it_not_ok(
        self, monkeypatch, make_fake_model_post
    ):
        model = _perplexity()
        await _answered(monkeypatch, model, make_fake_model_post(401, INVALID_KEY_BODY))
        assert model.model_is_ok() is False

    def test_the_router_reads_its_health_live(self):
        assert _perplexity().health_checked_live is True


def _connector_refused(*args, **kwargs):
    raise aiohttp.ClientConnectorError(
        SimpleNamespace(host="gone.example", port=443, ssl=True),
        ConnectionRefusedError(111, "Connection refused"),
    )


def _cooldowns(model, outages: int, connect_failed=True, answered_between=False):
    """How long each of ``outages`` outages in a row cools the pair, each
    three failures, on a pinned clock with the 120s base cooldown."""
    key = (model.api_base, model.model)
    lengths = []
    with patch.dict(os.environ, {"FHI_TRANSPORT_COOLDOWN_SECONDS": "120"}), patch(
        "fighthealthinsurance.ml.ml_models.time.monotonic"
    ) as clock:
        now = 1000.0
        for _ in range(outages):
            clock.return_value = now
            for _ in range(3):
                model._note_transport_failure(
                    model.api_base,
                    model.model,
                    "connection refused",
                    connect_failed=connect_failed,
                )
            lengths.append(model._transport_cooldowns[key] - now)
            if answered_between:
                model._note_answered(model.api_base, model.model)
            # Past the longest cooldown and the strike window.
            now += 4000.0
    return lengths


class TestUnreachableHostCooldown:
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                aiohttp.ClientConnectorError(
                    SimpleNamespace(host="h", port=443, ssl=True),
                    ConnectionRefusedError(111, "Connection refused"),
                ),
                id="refused",
            ),
            pytest.param(
                aiohttp.ConnectionTimeoutError("connect"), id="connect-timeout"
            ),
            pytest.param(socket.gaierror(-2, "Name or service not known"), id="dns"),
        ],
    )
    def test_a_failure_to_connect_is_connect_phase(self, exc):
        assert _connect_failed(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(aiohttp.ServerDisconnectedError(), id="disconnected"),
            pytest.param(asyncio.TimeoutError(), id="budget-timeout"),
        ],
    )
    def test_a_failure_after_connecting_is_not_connect_phase(self, exc):
        assert not _connect_failed(exc)

    def test_an_external_host_cools_four_times_longer_each_outage(self):
        assert _cooldowns(_plain(), 2) == [120.0, 480.0]

    def test_the_cooldown_stops_growing_at_an_hour(self):
        assert _cooldowns(_plain(), 4)[-1] == 3600.0

    def test_our_own_server_keeps_the_flat_cooldown(self):
        """One back from a restart is asked again soon."""
        server = _OurServer("http://ours.example/v1", "tok", "our-model")
        assert _cooldowns(server, 2) == [120.0, 120.0]

    def test_a_failure_after_connecting_keeps_the_flat_cooldown(self):
        assert _cooldowns(_plain(), 2, connect_failed=False) == [120.0, 120.0]

    def test_an_answer_starts_the_next_outage_from_the_short_cooldown(self):
        assert _cooldowns(_plain(), 2, answered_between=True) == [120.0, 120.0]

    @pytest.mark.asyncio
    async def test_refused_connections_through_the_transport_escalate(
        self, monkeypatch
    ):
        model = _plain("http://gone.example/v1")
        key = (model.api_base, model.model)
        monkeypatch.setattr(aiohttp.ClientSession, "post", _connector_refused)
        with patch.dict(os.environ, {"FHI_TRANSPORT_COOLDOWN_SECONDS": "120"}):
            for _ in range(3):
                await _ask(model)
            # The first outage's cooldown is over; the host is still gone.
            model._transport_cooldowns[key] = time.monotonic() - 1
            for _ in range(3):
                await _ask(model)
        left = model._transport_cooldowns[key] - time.monotonic()
        assert 470.0 < left <= 480.0

    @pytest.mark.asyncio
    async def test_an_answer_through_the_transport_ends_the_escalation(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        key = (model.api_base, model.model)
        model._transport_recools[key] = 2
        await _answered(
            monkeypatch, model, make_fake_model_post(200, "{}", json_data=GOOD_JSON)
        )
        assert key not in model._transport_recools


class TestMessagesResponseParsing:
    def test_a_text_block_whose_text_is_none_is_skipped(self):
        parsed = _azure_claude()._parse_messages_response(
            {
                "content": [
                    {"type": "text", "text": None},
                    {"type": "text", "text": LONG_REPLY},
                ]
            }
        )
        assert parsed == (LONG_REPLY, [])

    def test_a_reply_with_no_text_but_none_is_no_answer(self):
        parsed = _azure_claude()._parse_messages_response(
            {"content": [{"type": "text", "text": None}]}
        )
        assert parsed is None
