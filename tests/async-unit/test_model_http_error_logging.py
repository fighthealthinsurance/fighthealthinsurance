"""Tests for status-aware HTTP error logging in remote ML backends.

A backend that returns an expected operational error -- an exhausted quota
(401), a billing problem (402), a forbidden resource (403), or rate limiting
(429) -- should be logged concisely as a WARNING and degrade to ``None``,
*not* emit an ERROR with a stack trace (which reads like a crash). Real
failures (e.g. HTTP 500) keep the ERROR + traceback.
"""

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from loguru import logger
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from fighthealthinsurance.ml.ml_models import (
    EXPECTED_HTTP_STATUS_CODES,
    RemoteFullOpenLike,
    _http_error_indicates_context_length,
    _http_status_is_expected,
)


def _client_response_error(
    status: int, message: Optional[str] = None
) -> aiohttp.ClientResponseError:
    """Build a ClientResponseError carrying ``status`` (and optional message).

    A real ``RequestInfo`` is required because the exception's ``__str__``
    (used when loguru renders the traceback) dereferences
    ``request_info.real_url``.
    """
    url = URL("https://api.test/chat/completions")
    request_info = aiohttp.RequestInfo(
        url, "POST", CIMultiDictProxy(CIMultiDict()), url
    )
    if message is None:
        message = "Unauthorized" if status == 401 else "Error"
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        message=message,
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

    @property
    def levels(self):
        return [r["level"].name for r in self.records]


class TestExpectedStatusClassification:
    def test_quota_auth_ratelimit_codes_are_expected(self):
        for status in (401, 402, 403, 429):
            assert _http_status_is_expected(status)
            assert status in EXPECTED_HTTP_STATUS_CODES

    def test_server_errors_and_none_are_not_expected(self):
        for status in (400, 404, 500, 502, 503, None):
            assert not _http_status_is_expected(status)


class TestInferHttpErrorLogging:
    def _model(self) -> RemoteFullOpenLike:
        return RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

    @pytest.mark.asyncio
    async def test_expected_error_logs_warning_not_error_and_returns_none(self):
        """A 401 (insufficient_quota) must not produce an ERROR-level log."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=_client_response_error(401),
        ):
            with _LogCapture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert "ERROR" not in cap.levels
        assert "WARNING" in cap.levels
        # The concise warning should hint at the operational cause.
        warning_text = " ".join(
            r["message"] for r in cap.records if r["level"].name == "WARNING"
        )
        assert "401" in warning_text
        assert "quota" in warning_text.lower()

    @pytest.mark.asyncio
    async def test_unexpected_error_still_logs_error_with_traceback(self):
        """A 500 is a real failure and keeps the ERROR + traceback."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=_client_response_error(500),
        ):
            with _LogCapture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert "ERROR" in cap.levels


class TestContextLengthErrorDetection:
    def test_context_length_phrasings_match(self):
        for msg in (
            "This model's maximum context length is 8192 tokens",
            "Please reduce the length of the messages",
            "input is too long for the requested model",
            "Requested 40000 tokens, too many tokens for this model",
            "context window exceeded",
        ):
            assert _http_error_indicates_context_length(400, msg)
        # 413 Payload Too Large is also a valid context-length signal.
        assert _http_error_indicates_context_length(413, "prompt is too long")

    def test_non_context_400s_do_not_match(self):
        assert not _http_error_indicates_context_length(400, "invalid api key")
        assert not _http_error_indicates_context_length(400, "")
        # A context-length phrasing on a non-400/413 status is not treated as one.
        assert not _http_error_indicates_context_length(500, "context length")
        assert not _http_error_indicates_context_length(429, "too many tokens")


class _CtxLenFakeResponse:
    """Minimal aiohttp response stand-in with a canned status + body text.
    ``raise_for_status`` mirrors aiohttp: the ClientResponseError carries the
    HTTP *reason phrase* as ``message`` (NOT the body) -- which is exactly why
    context-length must be detected from ``text()``, not from ``e.message``."""

    def __init__(self, status=200, text="", json_data=None):
        self.status = status
        self._text = text
        self._json = json_data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=self.status,
                message="Bad Request",  # reason phrase, not the body
            )


class _CtxLenFakeSession:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        return self._response


class TestInferContextLengthLogging:
    """__infer detects context-length overflow from the response BODY (the
    reason phrase never carries the provider explanation), logs a concise
    model-tagged WARNING, and returns None -- not a scary ERROR + traceback."""

    def _model(self) -> RemoteFullOpenLike:
        return RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

    @pytest.mark.asyncio
    async def test_context_length_body_warns_and_returns_none(self):
        model = self._model()
        response = _CtxLenFakeResponse(
            status=400,
            text=(
                '{"error": {"message": "This model\'s maximum context length '
                'is 8192 tokens, however you requested 90000 tokens.", '
                '"code": "context_length_exceeded"}}'
            ),
        )
        session = _CtxLenFakeSession(response)
        with patch.object(aiohttp, "ClientSession", return_value=session):
            with _LogCapture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert "ERROR" not in cap.levels
        warning_text = " ".join(
            r["message"] for r in cap.records if r["level"].name == "WARNING"
        )
        assert "context-length exceeded" in warning_text.lower()

    @pytest.mark.asyncio
    async def test_generic_400_body_still_logs_error(self):
        """A 400 that is NOT a context-length error still surfaces as ERROR."""
        model = self._model()
        response = _CtxLenFakeResponse(
            status=400, text='{"error": "malformed request body"}'
        )
        session = _CtxLenFakeSession(response)
        with patch.object(aiohttp, "ClientSession", return_value=session):
            with _LogCapture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        assert "ERROR" in cap.levels


class TestModelStr:
    def test_str_is_readable(self):
        """str() should be human-readable, not the default object repr."""
        model = RemoteFullOpenLike("http://test-api.com", "tok", "test-model")
        text = str(model)
        assert "test-model" in text
        assert "RemoteFullOpenLike" in text
        assert "object at 0x" not in text
