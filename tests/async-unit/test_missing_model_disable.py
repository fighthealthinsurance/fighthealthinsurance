"""Tests for detecting and cooling down "model not served here" endpoints.

A local OpenAI-compatible endpoint (vLLM, llama.cpp, Ollama) that is up but
doesn't serve the requested model answers 404 "The model `X` does not exist"
on every call. That must produce ONE warning and flag the (api_base, model)
pair for a cooldown during which calls skip the endpoint entirely -- not a
log line (or an HTTP round-trip) per inference. The startup probe bypasses
the cooldown and still surfaces the real HTTP status.

Shared fakes (log_capture, make_fake_model_post) live in conftest.py.
"""

import time

import aiohttp
import pytest

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    _error_text_indicates_missing_model,
)

VLLM_404_BODY = (
    '{"object":"error","message":"The model `nope-model` does not exist.",'
    '"type":"NotFoundError","code":404}'
)


def _model(api_base: str) -> RemoteFullOpenLike:
    return RemoteFullOpenLike(api_base, "test-token", "nope-model")


@pytest.fixture
def missing_model_200_post(make_fake_model_post):
    """ClientSession.post stand-in for a server that reports a missing model
    inside a 200 {"object": "error"} body (shared by the flag/probe tests)."""
    return make_fake_model_post(
        200,
        VLLM_404_BODY,
        json_data={
            "object": "error",
            "message": "The model `nope-model` does not exist.",
            "type": "NotFoundError",
        },
    )


class TestMissingModelDetection:
    def test_vllm_not_found_body_matches(self):
        assert _error_text_indicates_missing_model(VLLM_404_BODY)

    def test_ollama_style_message_matches(self):
        assert _error_text_indicates_missing_model(
            '{"error":{"message":"model \'x\' not found, try pulling it first"}}'
        )

    def test_openai_model_not_found_code_matches(self):
        assert _error_text_indicates_missing_model(
            '{"error":{"code":"model_not_found"}}'
        )

    def test_wrong_url_404_does_not_match(self):
        # vLLM's response for a bad path: no mention of a model.
        assert not _error_text_indicates_missing_model('{"detail":"Not Found"}')

    def test_empty_and_unrelated_bodies_do_not_match(self):
        assert not _error_text_indicates_missing_model("")
        assert not _error_text_indicates_missing_model(None)
        assert not _error_text_indicates_missing_model("internal server error")


class TestMissingModelCooldown:
    @pytest.mark.asyncio
    async def test_404_flags_pair_logs_once_and_skips_next_call(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        model = _model("http://missing.example/v1")
        fake_post = make_fake_model_post(404, VLLM_404_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with log_capture() as cap:
            first = await model._infer(system_prompts=["sys"], prompt="hi")
            second = await model._infer(system_prompts=["sys"], prompt="hi")

        assert first is None and second is None
        # One HTTP round-trip total: the second call skipped the endpoint.
        assert fake_post.calls == 1
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "not served" in warnings[0]
        assert "nope-model" in warnings[0]
        # No ERROR-level noise for this operational condition.
        assert cap.messages("ERROR") == []
        # The skip is visible at DEBUG for traceability.
        assert any("flagged as not served" in m for m in cap.messages("DEBUG"))

    @pytest.mark.asyncio
    async def test_cooldown_expiry_probes_endpoint_again(
        self, monkeypatch, make_fake_model_post
    ):
        model = _model("http://missing.example/v1")
        fake_post = make_fake_model_post(404, VLLM_404_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        await model._infer(system_prompts=["sys"], prompt="hi")
        assert fake_post.calls == 1
        # Force the cooldown to lapse; the endpoint must be probed live again.
        key = ("http://missing.example/v1", "nope-model")
        model._missing_models[key] = time.monotonic() - 1
        await model._infer(system_prompts=["sys"], prompt="hi")
        assert fake_post.calls == 2

    @pytest.mark.asyncio
    async def test_probe_path_still_raises_http_status(
        self, monkeypatch, make_fake_model_post
    ):
        """raise_http_errors (the startup probe) must bypass the cooldown and
        surface the raw 404 so the probe reports the real cause."""
        model = _model("http://missing.example/v1")
        fake_post = make_fake_model_post(404, VLLM_404_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        # Still flagged for the regular request path.
        assert model._model_marked_missing("http://missing.example/v1", "nope-model")
        # And the probe is never gated by an existing flag: a second probe
        # still makes a live call.
        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        assert fake_post.calls == 2

    @pytest.mark.asyncio
    async def test_error_object_in_200_body_flags_pair(
        self, monkeypatch, missing_model_200_post, log_capture
    ):
        """Servers that report missing models inside a 200 {"object":"error"}
        body get the same cooldown treatment as an HTTP 404."""
        model = _model("http://missing.example/v1")
        monkeypatch.setattr(aiohttp.ClientSession, "post", missing_model_200_post)

        with log_capture() as cap:
            result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert model._model_marked_missing("http://missing.example/v1", "nope-model")
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "not served" in warnings[0]

    @pytest.mark.asyncio
    async def test_error_object_in_200_body_still_raises_for_probe(
        self, monkeypatch, missing_model_200_post
    ):
        """The startup probe must see the missing-model cause even when the
        transport said 200: a status-bearing error is raised (as in the 404
        branch) instead of degrading to "empty or no response"."""
        model = _model("http://missing.example/v1")
        monkeypatch.setattr(aiohttp.ClientSession, "post", missing_model_200_post)

        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        assert "does not exist" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_rate_limited_provider_probe_raises_http_status(
        self, monkeypatch, make_fake_model_post
    ):
        """raise_http_errors must survive RateLimitedRemoteOpenLike's
        _do_infer delegation: a paid-provider probe against a missing model
        reports the real 404, and is not silently absorbed into the
        cooldown."""
        from fighthealthinsurance.ml.ml_models import RateLimitedRemoteOpenLike

        model = RateLimitedRemoteOpenLike(
            "http://missing.example/v1", "test-token", "nope-model"
        )
        RateLimitedRemoteOpenLike._ensure_rate_limiter("nope-model")
        fake_post = make_fake_model_post(404, VLLM_404_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        # A second probe still hits the network despite the cooldown flag.
        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        assert fake_post.calls == 2
