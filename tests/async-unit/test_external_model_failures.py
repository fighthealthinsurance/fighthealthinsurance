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
import datetime
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import ANY, patch

import aiohttp
import pytest

from fighthealthinsurance.ml import ml_models, retired_models, spend
from fighthealthinsurance.ml.ml_models import (
    DeepInfra,
    NoAnswerText,
    ProviderUnavailable,
    RemoteAnthropic,
    RemoteAzureClaude,
    RemoteFullOpenLike,
    RemotePerplexity,
    RetiredEndpointError,
    _connect_failed,
    _http_error_indicates_retired_model,
    _http_error_indicates_unsupported_temperature,
)
from fighthealthinsurance.ml.retired_models import Retirement

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
# Anthropic's wording for a model that takes no sampling parameters.
TEMPERATURE_DEPRECATED_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"`temperature` is deprecated for this model."}}'
)
# A 200 whose only choice has no text: a reasoning model that spent its
# budget thinking.
NO_TEXT_JSON = {
    "choices": [
        {"message": {"role": "assistant", "content": None}, "finish_reason": "length"}
    ]
}
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
            pytest.param(
                400, TEMPERATURE_DEPRECATED_BODY, id="400-anthropic-temperature"
            ),
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

    def test_a_reply_of_only_whitespace_is_no_answer(self):
        """As on the shared transport, a blank reply is no text."""
        parsed = _azure_claude()._parse_messages_response(
            {"content": [{"type": "text", "text": "\n\n"}]}
        )
        assert parsed is None


_CHECKED_INFER_KWARGS = dict(
    prompt="denial text",
    patient_context=None,
    plan_context=None,
    infer_type="medically_necessary",
    pubmed_context=None,
    system_prompt="sys",
    temperature=0.5,
)


class _SequencedPost:
    """ClientSession.post stand-in answering each call with the next queued
    response (built with make_fake_model_post), keeping each request body."""

    def __init__(self, *posts):
        self._responses = [post._response for post in posts]
        self.bodies = []
        self._current = None

    def __call__(self, url, *args, json=None, **kwargs):
        self.bodies.append(json)
        self._current = self._responses.pop(0)
        return self

    async def __aenter__(self):
        return self._current

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def _fresh_rate_limiters():
    """A back-off a test leaves on a shared per-class limiter would skip
    other tests' calls before they are sent."""
    yield
    RemoteAnthropic._rate_limiters.pop("claude-sonnet-4-6", None)
    RemoteAzureClaude._rate_limiters.pop("claude-opus-4-8", None)


class TestRateLimitedHttpErrors:
    """Anthropic and Azure re-raised a 5xx, a bare 404 or an unclassified
    400 to every caller, so chat counted it as a bug and skipped its second
    try, and the appeal path recorded a provider outage as an error."""

    @pytest.mark.asyncio
    async def test_a_5xx_returns_no_answer_to_a_plain_caller(
        self, monkeypatch, make_fake_model_post
    ):
        result = await _answered(
            monkeypatch, _anthropic(), make_fake_model_post(503, "overloaded")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_5xx_raises_provider_unavailable_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        with pytest.raises(ProviderUnavailable, match="503"):
            await _answered(
                monkeypatch,
                _anthropic(),
                make_fake_model_post(503, "overloaded"),
                raise_on_unavailable=True,
            )

    @pytest.mark.asyncio
    async def test_a_probe_still_gets_the_raw_5xx(
        self, monkeypatch, make_fake_model_post
    ):
        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            await _answered(
                monkeypatch,
                _anthropic(),
                make_fake_model_post(503, "overloaded"),
                raise_http_errors=True,
            )
        assert excinfo.value.status == 503

    @pytest.mark.asyncio
    async def test_a_messages_api_5xx_returns_no_answer_to_a_plain_caller(
        self, monkeypatch, make_fake_model_post
    ):
        result = await _answered(
            monkeypatch, _azure_claude(), make_fake_model_post(500, "boom")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_5xx_is_logged_once_with_its_body(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        """The transport's WARNING with the body preview is the one line; the
        rate-limited wrapper adds no WARNING or ERROR of its own."""
        with log_capture() as cap:
            await _answered(
                monkeypatch, _anthropic(), make_fake_model_post(503, "overloaded")
            )
        assert (len(cap.messages("WARNING")), cap.messages("ERROR")) == (1, [])


class TestRetryAfterAnOutage:
    """A model that could not be reached on the appeal path's first try and
    its retry used to be filed as a model that answered nothing."""

    @pytest.mark.asyncio
    async def test_an_outage_on_both_tries_raises_provider_unavailable(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        monkeypatch.setattr(
            aiohttp.ClientSession, "post", make_fake_model_post(503, "overloaded")
        )
        with pytest.raises(ProviderUnavailable, match="503"):
            await model._checked_infer(**_CHECKED_INFER_KWARGS)

    @pytest.mark.asyncio
    async def test_an_outage_on_both_tries_is_an_unavailable_result(
        self, monkeypatch, make_fake_model_post
    ):
        model = _plain()
        fake_post = make_fake_model_post(503, "overloaded")
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)
        with patch.object(ml_models, "record_ml_result") as recorded:
            with pytest.raises(ProviderUnavailable):
                await model._checked_infer(**_CHECKED_INFER_KWARGS)
        assert (fake_post.calls, recorded.call_args.args[2]) == (2, "unavailable")

    @pytest.mark.asyncio
    async def test_an_outage_with_no_time_to_retry_is_an_unavailable_result(
        self, monkeypatch, make_fake_model_post
    ):
        """Asked once, not reached, and too little time left for the retry:
        it used to be filed as a model that answered nothing."""
        model = _plain()
        fake_post = make_fake_model_post(503, "overloaded")
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)
        with patch.object(ml_models, "record_ml_result") as recorded:
            with pytest.raises(ProviderUnavailable, match="503"):
                await model._checked_infer(
                    **_CHECKED_INFER_KWARGS, deadline=time.monotonic() + 5.0
                )
        assert (fake_post.calls, recorded.call_args.args[2]) == (1, "unavailable")

    @pytest.mark.asyncio
    async def test_a_rate_limited_providers_outage_is_not_an_error_result(
        self, monkeypatch, make_fake_model_post
    ):
        """Its retry used to re-raise the raw 5xx, which _checked_infer
        recorded as "error", the bucket for a bug in this path."""
        model = _anthropic()
        monkeypatch.setattr(
            aiohttp.ClientSession, "post", make_fake_model_post(503, "overloaded")
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch.object(
            ml_models, "record_ml_result"
        ) as recorded:
            with pytest.raises(ProviderUnavailable):
                await model._checked_infer(**_CHECKED_INFER_KWARGS)
        assert recorded.call_args.args[2] == "unavailable"

    @pytest.mark.asyncio
    async def test_an_empty_answer_on_both_tries_stays_no_completion(
        self, monkeypatch, make_fake_model_post
    ):
        """A model that was reached and answered nothing is not an outage."""
        model = _plain()
        monkeypatch.setattr(
            aiohttp.ClientSession,
            "post",
            make_fake_model_post(200, "{}", json_data=NO_TEXT_JSON),
        )
        with patch.object(ml_models, "record_ml_result") as recorded:
            result = await model._checked_infer(**_CHECKED_INFER_KWARGS)
        assert (result, recorded.call_args.args[2]) == ([], "no_completion")


class TestOdd200sAreFailedReads:
    """Entity extraction (raise_on_unavailable) read an empty or malformed
    200 as a letter without the field, and only on some providers."""

    @pytest.mark.asyncio
    async def test_no_text_raises_no_answer_text_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        with pytest.raises(NoAnswerText):
            await _answered(
                monkeypatch,
                _plain(),
                make_fake_model_post(200, "{}", json_data=NO_TEXT_JSON),
                raise_on_unavailable=True,
            )

    @pytest.mark.asyncio
    async def test_no_choices_raises_provider_unavailable_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        """A 200 without a completion failed; it did not answer nothing."""
        with pytest.raises(ProviderUnavailable) as excinfo:
            await _answered(
                monkeypatch,
                _plain(),
                make_fake_model_post(200, "{}", json_data={"choices": []}),
                raise_on_unavailable=True,
            )
        assert type(excinfo.value) is ProviderUnavailable

    @pytest.mark.asyncio
    async def test_a_non_json_200_raises_provider_unavailable_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        post = make_fake_model_post(200, "<html>Bad gateway</html>")

        async def not_json():
            raise aiohttp.ContentTypeError(post._response.request_info, ())

        post._response.json = not_json
        with pytest.raises(ProviderUnavailable, match="unreadable"):
            await _answered(monkeypatch, _deepinfra(), post, raise_on_unavailable=True)

    @pytest.mark.asyncio
    async def test_an_empty_answer_still_returns_none_to_a_plain_caller(
        self, monkeypatch, make_fake_model_post
    ):
        result = await _answered(
            monkeypatch,
            _plain(),
            make_fake_model_post(200, "{}", json_data=NO_TEXT_JSON),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_entity_extraction_reports_an_empty_answer_as_a_failed_read(
        self, monkeypatch, make_fake_model_post
    ):
        monkeypatch.setattr(
            aiohttp.ClientSession,
            "post",
            make_fake_model_post(200, "{}", json_data=NO_TEXT_JSON),
        )
        with pytest.raises(ProviderUnavailable):
            await _plain().get_entity("a denial letter", "fax number")

    @pytest.mark.asyncio
    async def test_a_messages_api_empty_answer_raises_no_answer_text_when_asked(
        self, monkeypatch, make_fake_model_post
    ):
        """The same for Claude on Azure, whose transport is its own."""
        with pytest.raises(NoAnswerText):
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(200, "{}", json_data={"content": []}),
                raise_on_unavailable=True,
            )


class TestTemperatureDeprecatedWording:
    """Anthropic says "`temperature` is deprecated for this model."; the
    fallback did not recognise it, so the model failed every call."""

    def test_the_wording_is_a_temperature_rejection(self):
        assert _http_error_indicates_unsupported_temperature(
            400, TEMPERATURE_DEPRECATED_BODY
        )

    def test_haiku_5_5_is_sent_no_temperature(self):
        assert _anthropic()._supports_custom_temperature("claude-haiku-5-5") is False

    @pytest.mark.asyncio
    async def test_the_call_is_retried_without_temperature(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        post = _SequencedPost(
            make_fake_model_post(400, TEMPERATURE_DEPRECATED_BODY),
            make_fake_model_post(200, "{}", json_data=GOOD_JSON),
        )
        result = await _answered(monkeypatch, model, post)
        assert (result is not None, "temperature" in post.bodies[1]) == (True, False)

    @pytest.mark.asyncio
    async def test_the_deployment_is_remembered(
        self, monkeypatch, make_fake_model_post
    ):
        model = _deepinfra()
        post = _SequencedPost(
            make_fake_model_post(400, TEMPERATURE_DEPRECATED_BODY),
            make_fake_model_post(200, "{}", json_data=GOOD_JSON),
        )
        await _answered(monkeypatch, model, post)
        assert model.model in model._temperature_unsupported_models

    @pytest.mark.asyncio
    async def test_the_model_is_not_parked(self, monkeypatch, make_fake_model_post):
        model = _deepinfra()
        post = _SequencedPost(
            make_fake_model_post(400, TEMPERATURE_DEPRECATED_BODY),
            make_fake_model_post(200, "{}", json_data=GOOD_JSON),
        )
        await _answered(monkeypatch, model, post)
        assert model.is_available() is True


@pytest.mark.usefixtures("_fresh_rate_limiters")
class TestRateLimitBackOff:
    """A plain 429 takes a rate-limited provider out of routing for its
    Retry-After; it used to give no reason, and its skips went uncounted."""

    def test_the_reason_says_it_is_backing_off(self):
        model = _anthropic()
        model.rate_limiter.mark_exhausted(30.0)
        assert (model.unavailable_reason() or "").startswith("rate limited")

    def test_the_reason_clears_when_the_back_off_ends(self):
        model = _anthropic()
        model.rate_limiter.mark_exhausted(30.0)
        model.rate_limiter._exhausted_until = time.time() - 1
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            assert model.unavailable_reason() is None

    def test_a_missing_key_reads_as_not_configured(self, monkeypatch):
        model = _anthropic()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert model.unavailable_reason() == "not configured"

    @pytest.mark.asyncio
    async def test_a_skipped_call_is_counted_with_its_reason(self):
        model = _anthropic()
        model.rate_limiter.mark_exhausted(30.0)
        with patch.object(ml_models, "record_ml_call") as call, patch.object(
            ml_models, "record_ml_failure"
        ) as failure:
            await _ask(model)
        assert (call.call_args.args[1], failure.call_args.args[1]) == (
            "none",
            "skipped_rate_limited",
        )

    @pytest.mark.asyncio
    async def test_a_429_logs_one_warning(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        with log_capture() as cap:
            await _answered(
                monkeypatch, _anthropic(), make_fake_model_post(429, "slow down")
            )
        assert len(cap.messages("WARNING")) == 1


@pytest.mark.usefixtures("_fresh_rate_limiters")
class TestAzureClaudeParity:
    """Claude on Azure's own transport records and logs a failure as the
    shared transport does."""

    @pytest.mark.asyncio
    async def test_an_empty_answer_warns_once(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        with log_capture() as cap:
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(200, "{}", json_data={"content": []}),
            )
        assert len(cap.messages("WARNING")) == 1

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_counted_as_no_text(
        self, monkeypatch, make_fake_model_post
    ):
        with patch.object(ml_models, "record_ml_failure") as failure:
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(200, "{}", json_data={"content": []}),
            )
        assert [c.args[1] for c in failure.call_args_list] == ["no_text"]

    @pytest.mark.asyncio
    async def test_a_refused_connection_is_counted_as_no_answer(self, monkeypatch):
        """outcome "none", as the shared transport counts it, not "error"."""
        monkeypatch.setattr(aiohttp.ClientSession, "post", _connector_refused)
        with patch.object(ml_models, "record_ml_call") as call:
            await _ask(_azure_claude())
        assert [c.args[1] for c in call.call_args_list] == ["none"]

    @pytest.mark.asyncio
    async def test_a_refused_connect_after_a_cooldown_logs_no_warning(
        self, monkeypatch, log_capture
    ):
        """The cooldown's own WARNING says the outage goes on; a line per
        failed connect after it adds nothing."""
        model = _azure_claude()
        model._transport_recools[(model.api_base, model.model)] = 1
        monkeypatch.setattr(aiohttp.ClientSession, "post", _connector_refused)
        with log_capture() as cap:
            await _ask(model)
        assert cap.messages("WARNING") == []


def _dual() -> RemoteFullOpenLike:
    return RemoteFullOpenLike(
        "http://primary.example/v1",
        "tok",
        "primary-model",
        backup_api_base="http://backup.example/v1",
        backup_model="backup-model",
        dual_mode=True,
    )


class TestDualModeAsksAFailedBackupOnce:
    """The race asked the backup, and the sequential fallback asked it
    again in the same call, doubling its requests and its strikes."""

    @staticmethod
    async def _asked(model, backup_leg, **kwargs):
        """Which models one call asked, with ``backup_leg`` as the backup."""
        asked = []

        async def leg(*args, **leg_kwargs):
            asked.append(leg_kwargs.get("model"))
            if leg_kwargs.get("model") == "backup-model":
                return backup_leg(leg_kwargs)
            return None

        with patch.object(model, "_RemoteOpenLike__timeout_infer", new=leg):
            await model._infer(system_prompts=["sys"], prompt="hi", **kwargs)
        return asked

    @pytest.mark.asyncio
    async def test_an_unreachable_backup_is_asked_once(self):
        def refused(leg_kwargs):
            leg_kwargs["transport_failures"].append("backup-model: refused")
            return None

        asked = await self._asked(_dual(), refused)
        assert asked.count("backup-model") == 1

    @pytest.mark.asyncio
    async def test_a_backup_http_error_is_asked_once(self):
        def server_error(leg_kwargs):
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=500, message="Server Error"
            )

        asked = await self._asked(_dual(), server_error)
        assert asked.count("backup-model") == 1

    @pytest.mark.asyncio
    async def test_a_failed_backup_still_reaches_a_caller_that_asked(self):
        def refused(leg_kwargs):
            leg_kwargs["transport_failures"].append("backup-model: refused")
            return None

        with pytest.raises(ProviderUnavailable, match="refused"):
            await self._asked(_dual(), refused, raise_on_unavailable=True)


class TestProbeBudgetTimeouts:
    """Probes never count strikes, on any transport."""

    @staticmethod
    async def _hang(*args, **kwargs):
        await asyncio.sleep(5)

    @pytest.mark.asyncio
    async def test_a_probe_that_runs_out_its_budget_does_not_strike(self):
        model = _plain()
        with patch.object(
            model, "_RemoteOpenLike__infer", new=self._hang
        ), patch.object(model, "_note_budget_timeout") as note:
            await _ask(model, raise_http_errors=True, timeout=0.05)
        note.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_call_that_runs_out_its_budget_is_noted(self):
        model = _plain()
        with patch.object(
            model, "_RemoteOpenLike__infer", new=self._hang
        ), patch.object(model, "_note_budget_timeout") as note:
            await _ask(model, timeout=0.05)
        note.assert_called_once()


class TestAnAnswerClearsStrikes:
    def test_timeouts_between_answers_do_not_cool_a_serving_backend(self):
        """Budget-timeout strikes count for ten minutes, so three of them
        between answers cooled a backend that was busy, not hung."""
        model = _OurServer("http://ours.example/v1", "tok", "our-model")
        with patch("fighthealthinsurance.ml.ml_models.time.monotonic") as clock:
            for now in (1000.0, 1090.0, 1200.0):
                clock.return_value = now
                # It answered other requests before each hung one.
                model._note_served(model.api_base, model.model)
                model._note_budget_timeout(model.api_base, model.model, 300.0)
            assert not model._transport_cooling(model.api_base, model.model)


@pytest.mark.usefixtures("_fresh_rate_limiters")
class TestAnAnswerLiftsACreditPause:
    """A provider paused for credit stayed paused until 00:00 UTC, even after
    a top-up and a passing deploy check."""

    @pytest.mark.asyncio
    async def test_a_probe_reaches_a_paused_provider(
        self, monkeypatch, make_fake_model_post
    ):
        spend.pause(spend.ANTHROPIC, reason="test")
        fake_post = make_fake_model_post(200, "{}", json_data=GOOD_JSON)
        await _answered(monkeypatch, _anthropic(), fake_post, raise_http_errors=True)
        assert fake_post.calls == 1

    @pytest.mark.asyncio
    async def test_a_probe_answered_2xx_lifts_the_pause(
        self, monkeypatch, make_fake_model_post
    ):
        spend.pause(spend.ANTHROPIC, reason="test")
        with patch.object(spend, "unpause") as unpause:
            await _answered(
                monkeypatch,
                _anthropic(),
                make_fake_model_post(200, "{}", json_data=GOOD_JSON),
                raise_http_errors=True,
            )
        unpause.assert_called_once_with(spend.ANTHROPIC, reason=ANY)

    @pytest.mark.asyncio
    async def test_after_the_lift_the_provider_is_no_longer_paused(
        self, monkeypatch, make_fake_model_post
    ):
        spend.pause(spend.ANTHROPIC, reason="test")
        await _answered(
            monkeypatch,
            _anthropic(),
            make_fake_model_post(200, "{}", json_data=GOOD_JSON),
            raise_http_errors=True,
        )
        assert not spend.paused(spend.ANTHROPIC, "*")

    @pytest.mark.asyncio
    async def test_a_messages_api_probe_answered_2xx_lifts_the_pause(
        self, monkeypatch, make_fake_model_post
    ):
        spend.pause(spend.AZURE_ANTHROPIC, reason="test")
        with patch.object(spend, "unpause") as unpause:
            await _answered(
                monkeypatch,
                _azure_claude(),
                make_fake_model_post(
                    200, "{}", json_data={"content": [{"type": "text", "text": "OK"}]}
                ),
                raise_http_errors=True,
            )
        unpause.assert_called_once_with(spend.AZURE_ANTHROPIC, reason=ANY)

    @pytest.mark.asyncio
    async def test_an_answer_without_a_pause_lifts_nothing(
        self, monkeypatch, make_fake_model_post
    ):
        with patch.object(spend, "unpause") as unpause:
            await _answered(
                monkeypatch,
                _anthropic(),
                make_fake_model_post(200, "{}", json_data=GOOD_JSON),
            )
        unpause.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_error_answer_lifts_nothing(
        self, monkeypatch, make_fake_model_post
    ):
        spend.pause(spend.ANTHROPIC, reason="test")
        with patch.object(spend, "unpause") as unpause:
            with pytest.raises(aiohttp.ClientResponseError):
                await _answered(
                    monkeypatch,
                    _anthropic(),
                    make_fake_model_post(400, CREDIT_BODY),
                    raise_http_errors=True,
                )
        unpause.assert_not_called()


class TestRetiredOnlyEndpoint:
    def test_a_backend_left_with_only_a_retired_endpoint_raises_retired(
        self, monkeypatch
    ):
        """Its own class, so the deploy check shows it as Retired rather than
        as a backend that failed to construct."""
        monkeypatch.setitem(
            retired_models.RETIRED_MODELS,
            "fhi-test-retired",
            Retirement(datetime.date(2026, 10, 10), "test"),
        )
        with pytest.raises(RetiredEndpointError):
            RemoteFullOpenLike(
                None,
                "tok",
                "fhi-live",
                backup_api_base="http://backup.example/v1",
                backup_model="fhi-test-retired",
            )

    def test_it_is_still_a_value_error(self):
        assert issubclass(RetiredEndpointError, ValueError)


def _cooled(model, detail: str, connect_failed: bool):
    """Cool the pair with three strikes, as the transport does."""
    for _ in range(3):
        model._note_transport_failure(
            model.api_base, model.model, detail, connect_failed=connect_failed
        )
    return model


class TestReachableHostEndsAConnectCooldown:
    """The health sweep's /models request answering says the host is up
    again (_note_reachable); it says nothing about generation or the key."""

    def test_a_connect_failure_cooldown_ends(self):
        model = _cooled(_plain(), "connection refused", connect_failed=True)
        model._note_reachable(model.api_base, model.model)
        assert not model._transport_cooling(model.api_base, model.model)

    def test_the_escalation_resets(self):
        model = _cooled(_plain(), "connection refused", connect_failed=True)
        model._note_reachable(model.api_base, model.model)
        assert (model.api_base, model.model) not in model._transport_recools

    def test_a_5xx_cooldown_stays(self):
        model = _cooled(_plain(), "HTTP 503", connect_failed=False)
        model._note_reachable(model.api_base, model.model)
        assert model._transport_cooling(model.api_base, model.model)

    def test_a_refused_key_stays_refused(self):
        model = _plain()
        model._note_http_refusal(
            model.api_base, model.model, 401, INVALID_KEY_BODY, probe=False
        )
        model._note_reachable(model.api_base, model.model)
        assert model._pair_refused(model.api_base, model.model)
