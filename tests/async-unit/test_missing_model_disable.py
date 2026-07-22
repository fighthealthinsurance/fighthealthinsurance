"""Tests for detecting and cooling down "model not served here" endpoints.

A local OpenAI-compatible endpoint (vLLM, llama.cpp, Ollama) that is up but
doesn't serve the requested model answers 404 "The model `X` does not exist"
on every call. That must produce ONE warning and flag the (api_base, model)
pair for a cooldown during which calls skip the endpoint entirely -- not a
log line (or an HTTP round-trip) per inference. The startup probe bypasses
the cooldown and still surfaces the real HTTP status.
"""

import time

import aiohttp
import pytest
from loguru import logger
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    _error_text_indicates_missing_model,
)

VLLM_404_BODY = (
    '{"object":"error","message":"The model `nope-model` does not exist.",'
    '"type":"NotFoundError","code":404}'
)


class _LogCapture:
    """Context manager capturing loguru records at DEBUG and above."""

    def __enter__(self):
        self.records = []
        self._sink_id = logger.add(
            lambda msg: self.records.append(msg.record), level="DEBUG"
        )
        return self

    def __exit__(self, *exc):
        logger.remove(self._sink_id)

    def messages(self, level):
        return [r["message"] for r in self.records if r["level"].name == level]


class _FakeResponse:
    def __init__(self, status: int, body: str, json_data=None):
        self.status = status
        self._body = body
        self._json = json_data

    async def text(self):
        return self._body

    async def json(self):
        return self._json

    def raise_for_status(self):
        if self.status >= 400:
            # A real RequestInfo so any str()/repr() of the error (loguru,
            # pytest tracebacks) can dereference request_info.real_url.
            url = URL("http://missing.example/v1/chat/completions")
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url, "POST", CIMultiDictProxy(CIMultiDict()), url
                ),
                history=(),
                status=self.status,
                message="Not Found" if self.status == 404 else "Error",
            )


class _FakePost:
    """Stands in for ClientSession.post: returns an async CM yielding the
    canned response, counting calls so tests can assert an endpoint was NOT
    re-hit during cooldown."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


def _model(api_base: str) -> RemoteFullOpenLike:
    return RemoteFullOpenLike(api_base, "test-token", "nope-model")


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
    async def test_404_flags_pair_logs_once_and_skips_next_call(self, monkeypatch):
        model = _model("http://missing.example/v1")
        fake_post = _FakePost(_FakeResponse(404, VLLM_404_BODY))
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with _LogCapture() as cap:
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
    async def test_cooldown_expiry_probes_endpoint_again(self, monkeypatch):
        model = _model("http://missing.example/v1")
        fake_post = _FakePost(_FakeResponse(404, VLLM_404_BODY))
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        await model._infer(system_prompts=["sys"], prompt="hi")
        assert fake_post.calls == 1
        # Force the cooldown to lapse; the endpoint must be probed live again.
        key = ("http://missing.example/v1", "nope-model")
        model._missing_models[key] = time.monotonic() - 1
        await model._infer(system_prompts=["sys"], prompt="hi")
        assert fake_post.calls == 2

    @pytest.mark.asyncio
    async def test_probe_path_still_raises_http_status(self, monkeypatch):
        """raise_http_errors (the startup probe) must bypass the cooldown and
        surface the raw 404 so the probe reports the real cause."""
        model = _model("http://missing.example/v1")
        fake_post = _FakePost(_FakeResponse(404, VLLM_404_BODY))
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
    async def test_error_object_in_200_body_flags_pair(self, monkeypatch):
        """Servers that report missing models inside a 200 {"object":"error"}
        body get the same cooldown treatment as an HTTP 404."""
        model = _model("http://missing.example/v1")
        fake_post = _FakePost(
            _FakeResponse(
                200,
                VLLM_404_BODY,
                json_data={
                    "object": "error",
                    "message": "The model `nope-model` does not exist.",
                    "type": "NotFoundError",
                },
            )
        )
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with _LogCapture() as cap:
            result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert model._model_marked_missing("http://missing.example/v1", "nope-model")
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "not served" in warnings[0]
