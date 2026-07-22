"""Tests for classifying context-window-overflow 400s from model backends.

A prompt that outruns the model's context window is a per-request,
operational condition (the appeal path retries with shed context): it must
log ONE classified WARNING quoting the server's token counts -- not an ERROR
or a raw-body dump -- must NOT flag the backend as missing/unhealthy, and
must not stop subsequent (smaller) calls from being attempted.
"""

import aiohttp
import pytest

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    _http_error_indicates_context_overflow,
)

VLLM_OVERFLOW_BODY = (
    '{"object":"error","message":"This model\'s maximum context length '
    "is 32768 tokens. However, you requested 33223 tokens (31175 in the "
    "messages, 2048 in the completion). Please reduce the length of the "
    'messages or completion.","type":"BadRequestError","code":400}'
)


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://overflow.example/v1", "test-token", "big-model")


class TestOverflowDetection:
    def test_vllm_openai_maximum_context_length_matches(self):
        assert _http_error_indicates_context_overflow(400, VLLM_OVERFLOW_BODY)

    def test_openai_context_window_phrasing_matches(self):
        assert _http_error_indicates_context_overflow(
            400,
            '{"error":{"message":"Your input exceeds the context window of '
            'this model"}}',
        )

    def test_llama_cpp_context_size_phrasing_matches(self):
        assert _http_error_indicates_context_overflow(
            400, "the request exceeds the available context size"
        )

    def test_anthropic_prompt_too_long_matches(self):
        assert _http_error_indicates_context_overflow(
            400, "prompt is too long: 210145 tokens > 200000 maximum"
        )

    def test_unrelated_400_does_not_match(self):
        assert not _http_error_indicates_context_overflow(
            400, "'messages' is a required property"
        )

    def test_non_400_status_does_not_match(self):
        assert not _http_error_indicates_context_overflow(500, VLLM_OVERFLOW_BODY)

    @pytest.mark.parametrize("body", ["", None])
    def test_empty_body_does_not_match(self, body):
        assert not _http_error_indicates_context_overflow(400, body)


class TestOverflowLogging:
    @pytest.mark.asyncio
    async def test_overflow_logs_single_warning_and_degrades(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        model = _model()
        fake_post = make_fake_model_post(400, VLLM_OVERFLOW_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with log_capture() as cap:
            result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "context window" in warnings[0]
        # The server's token accounting survives into the log line.
        assert "33223" in warnings[0]
        assert cap.messages("ERROR") == []
        assert all(r["exception"] is None for r in cap.records)

    @pytest.mark.asyncio
    async def test_overflow_does_not_disable_the_backend(
        self, monkeypatch, make_fake_model_post
    ):
        """Unlike a missing model, an oversized request must not cool down
        the endpoint: the next (possibly smaller) call still goes out."""
        model = _model()
        fake_post = make_fake_model_post(400, VLLM_OVERFLOW_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        await model._infer(system_prompts=["sys"], prompt="big")
        await model._infer(system_prompts=["sys"], prompt="small")

        assert fake_post.calls == 2
        assert model._missing_models == {}

    @pytest.mark.asyncio
    async def test_probe_path_still_raises_http_status(
        self, monkeypatch, make_fake_model_post
    ):
        model = _model()
        fake_post = make_fake_model_post(400, VLLM_OVERFLOW_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
