"""Tests for status-aware HTTP error logging in remote ML backends.

A backend that returns an expected operational error -- an exhausted quota
(401), a billing problem (402), a forbidden resource (403), or rate limiting
(429) -- should be logged concisely as a WARNING and degrade to ``None``,
*not* emit an ERROR with a stack trace (which reads like a crash). Real
failures (e.g. HTTP 500) keep an ERROR-level line, but a concise classified
one: the traceback for an HTTP status error is aiohttp internals and buries
the status.
"""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from fighthealthinsurance.ml.ml_models import (
    EXPECTED_HTTP_STATUS_CODES,
    RemoteFullOpenLike,
    _http_status_is_expected,
)


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    """Build a ClientResponseError carrying ``status``.

    A real ``RequestInfo`` is required because the exception's ``__str__``
    (used when loguru renders the traceback) dereferences
    ``request_info.real_url``.
    """
    url = URL("https://api.test/chat/completions")
    request_info = aiohttp.RequestInfo(
        url, "POST", CIMultiDictProxy(CIMultiDict()), url
    )
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        message="Unauthorized" if status == 401 else "Error",
    )


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
    async def test_expected_error_logs_warning_not_error_and_returns_none(
        self, log_capture
    ):
        """A 401 (insufficient_quota) must not produce an ERROR-level log."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=_client_response_error(401),
        ):
            with log_capture() as cap:
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
    async def test_unexpected_http_error_still_logs_error_concisely(self, log_capture):
        """A 500 is a real failure: it keeps an ERROR-level line, but as a
        concise classified message (with the status) rather than a traceback
        of aiohttp internals."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=_client_response_error(500),
        ):
            with log_capture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        errors = [r for r in cap.records if r["level"].name == "ERROR"]
        assert len(errors) == 1
        assert "500" in errors[0]["message"]
        assert errors[0]["exception"] is None


class TestModelStr:
    def test_str_is_readable(self):
        """str() should be human-readable, not the default object repr."""
        model = RemoteFullOpenLike("http://test-api.com", "tok", "test-model")
        text = str(model)
        assert "test-model" in text
        assert "RemoteFullOpenLike" in text
        assert "object at 0x" not in text
