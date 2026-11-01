"""Provider responses reach the logs as a size and error labels, never as text.

A provider can echo the request back in an error, and the request holds patient
data. These send a sentinel through each path where a response body was logged.
"""

import aiohttp
import pytest
from unittest.mock import patch

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    error_kind_for_log,
    provider_body_for_log,
)

SENTINEL = "sentinel-patient-text"


def _model():
    return RemoteFullOpenLike("http://fake-backend.example/v1", "tok", "logs-test-model")


class TestProviderBodyForLog:
    def test_plain_text_gives_only_its_size(self):
        assert provider_body_for_log(f"bad request: {SENTINEL}") == f"{len(SENTINEL) + 13} chars"

    def test_json_gives_its_error_labels_not_its_message(self):
        body = '{"error": {"type": "invalid_request_error", "code": 400, "message": "%s"}}' % SENTINEL
        summary = provider_body_for_log(body)
        assert "type=invalid_request_error" in summary
        assert "code=400" in summary
        assert SENTINEL not in summary

    def test_a_label_holding_free_text_is_dropped(self):
        summary = provider_body_for_log({"error": {"type": f"bad input {SENTINEL} here"}})
        assert SENTINEL not in summary
        assert "type=" not in summary

    def test_a_json_list_gives_only_its_size(self):
        assert provider_body_for_log(f'["{SENTINEL}"]').endswith("chars")


    def test_deep_json_and_a_broken_body_fall_back_without_raising(self):
        class Broken:
            def __str__(self):
                raise RuntimeError(SENTINEL)

        assert provider_body_for_log("[" * 5000 + "]" * 5000).endswith("chars")
        assert provider_body_for_log(Broken()) == "unreadable body"


class TestErrorKindForLog:
    def test_a_status_that_raises_gives_the_type(self):
        class Odd(Exception):
            @property
            def status(self):
                raise RuntimeError(SENTINEL)

        assert error_kind_for_log(Odd()) == "Odd"

    def test_a_plain_exception_gives_its_type(self):
        assert error_kind_for_log(ValueError(SENTINEL)) == "ValueError"

    def test_an_http_error_gives_its_status(self):
        err = aiohttp.ClientResponseError(request_info=None, history=(), status=503, message=SENTINEL)
        assert error_kind_for_log(err) == "ClientResponseError (HTTP 503)"


class TestProviderBodiesStayOutOfLogs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,kwargs",
        [
            (500, {"body": f"server echoed: {SENTINEL}"}),
            (429, {"body": f"slow down: {SENTINEL}"}),
            (400, {"body": f"This model's maximum context length is 10 tokens. {SENTINEL}"}),
            (200, {"json_data": {"object": "chat.completion", "choices": [], "echo": SENTINEL}}),
            (200, {"json_data": {"object": "error", "message": SENTINEL}}),
            (404, {"body": f"The model does not exist. {SENTINEL}"}),
            (200, {"json_data": {"object": "error", "message": f"The model does not exist. {SENTINEL}"}}),
        ],
    )
    async def test_no_line_carries_the_response_text(self, status, kwargs, make_fake_model_post, log_capture):
        with patch("aiohttp.ClientSession.post", make_fake_model_post(status, **kwargs)):
            with log_capture() as cap:
                await _model()._infer_no_context(system_prompts=["sys"], prompt="hello", timeout=10.0)
        assert cap.records, "nothing logged, so the check proves nothing"
        for record in cap.records:
            assert SENTINEL not in record["message"], record["message"]
