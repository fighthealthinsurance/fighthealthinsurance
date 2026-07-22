"""Tests for concise, classified logging of model transport failures.

A backend that times out, refuses connections, or drops mid-request is an
expected operational condition: it must produce a single classified WARNING
line (so the cause -- timeout vs DNS vs refusal -- is immediately greppable)
and degrade to ``None``, NOT a multi-page stack trace of aiohttp/asyncio
internals. One backend failing must also never break the other backends'
results (see TestAppealStreamIsolation).
"""

import asyncio
import socket
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from fighthealthinsurance.exec import executor
from fighthealthinsurance.generate_appeal import (
    GeneratedAppeal,
    _generated_to_appeals_text,
)
from fighthealthinsurance.ml.ml_models import (
    MODEL_TRANSPORT_ERRORS,
    RemoteFullOpenLike,
    describe_model_error,
)
from fighthealthinsurance.utils import as_available_nested


def _connector_error(os_error: OSError) -> aiohttp.ClientConnectorError:
    """Build a ClientConnectorError around ``os_error``.

    The connection key only needs the attributes the exception's properties
    read, so a SimpleNamespace avoids depending on aiohttp's ConnectionKey
    layout.
    """
    key = SimpleNamespace(host="10.0.0.1", port=8000, is_ssl=False, ssl=None)
    return aiohttp.ClientConnectorError(key, os_error)


class TestDescribeModelError:
    def test_connection_timeout_names_the_cause(self):
        err = aiohttp.ConnectionTimeoutError(
            "Connection timeout to host http://10.0.0.1:8000/v1/chat/completions"
        )
        assert "connection timeout" in describe_model_error(err)

    def test_plain_asyncio_timeout(self):
        assert describe_model_error(asyncio.TimeoutError()) == "timed out"

    def test_server_disconnected(self):
        assert "disconnected" in describe_model_error(aiohttp.ServerDisconnectedError())

    def test_connection_refused(self):
        err = _connector_error(ConnectionRefusedError(111, "Connection refused"))
        assert describe_model_error(err) == "connection refused"

    def test_dns_failure(self):
        err = _connector_error(socket.gaierror(-2, "Name or service not known"))
        assert "DNS lookup failed" in describe_model_error(err)

    def test_http_status_error(self):
        err = aiohttp.ClientResponseError(
            request_info=None, history=(), status=503, message="Service Unavailable"
        )
        assert describe_model_error(err) == "HTTP 503 Service Unavailable"

    def test_content_type_error_mentions_missing_model(self):
        err = aiohttp.ContentTypeError(request_info=None, history=())
        assert "content type" in describe_model_error(err)
        assert "missing model" in describe_model_error(err)

    def test_unknown_exception_falls_back_to_type_and_message(self):
        assert describe_model_error(ValueError("boom")) == "ValueError: boom"

    def test_transport_error_tuple_covers_the_classified_types(self):
        for exc in (
            aiohttp.ConnectionTimeoutError(),
            aiohttp.ServerDisconnectedError(),
            _connector_error(ConnectionRefusedError(111, "refused")),
            asyncio.TimeoutError(),
            ConnectionResetError(104, "reset"),
        ):
            assert isinstance(exc, MODEL_TRANSPORT_ERRORS)


class TestTransportErrorLogging:
    def _model(self) -> RemoteFullOpenLike:
        return RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

    @pytest.mark.asyncio
    async def test_connection_timeout_logs_single_concise_warning(self, log_capture):
        """The pasted-log scenario: an unreachable backend must yield exactly
        one classified WARNING line with no traceback attached, not a
        multi-page aiohttp stack dump."""
        model = self._model()
        err = aiohttp.ConnectionTimeoutError(
            "Connection timeout to host http://test-api.com/v1/chat/completions"
        )
        with patch.object(aiohttp.ClientSession, "post", side_effect=err):
            with log_capture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        warnings = [r for r in cap.records if r["level"].name == "WARNING"]
        assert len(warnings) == 1
        assert "connection timeout" in warnings[0]["message"]
        assert warnings[0]["exception"] is None
        assert "ERROR" not in cap.levels

    @pytest.mark.asyncio
    async def test_transport_error_outside_infer_logs_concise_warning(
        self, log_capture
    ):
        """Transport errors surfacing at the _infer level (outside __infer's
        own handling) get the same one-line classified treatment."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=aiohttp.ServerDisconnectedError(),
        ):
            with log_capture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        warnings = [r for r in cap.records if r["level"].name == "WARNING"]
        assert len(warnings) == 1
        assert "disconnected" in warnings[0]["message"]
        assert warnings[0]["exception"] is None
        assert "ERROR" not in cap.levels

    @pytest.mark.asyncio
    async def test_unexpected_error_keeps_traceback(self, log_capture):
        """Genuine bugs (non-transport exceptions) must keep the full
        traceback so they remain debuggable."""
        model = self._model()
        with patch.object(
            model,
            "_RemoteOpenLike__timeout_infer",
            new_callable=AsyncMock,
            side_effect=TypeError("bug in our code"),
        ):
            with log_capture() as cap:
                result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        errors = [r for r in cap.records if r["level"].name == "ERROR"]
        assert len(errors) == 1
        assert errors[0]["exception"] is not None


class _StubTemplateGenerator:
    def generate(self, reason):
        return f"templated: {reason}"


class TestAppealStreamIsolation:
    """One failed backend future must not abort the whole appeal stream."""

    def test_failed_future_yields_nothing_and_logs_concisely(self, log_capture):
        failed: Future = Future()
        failed.set_exception(
            aiohttp.ConnectionTimeoutError("Connection timeout to host X")
        )
        with log_capture() as cap:
            out = list(
                _generated_to_appeals_text("m1", failed, _StubTemplateGenerator())
            )
        assert out == []
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "connection timeout" in warnings[0]
        assert all(r["exception"] is None for r in cap.records)

    def test_unexpected_failure_keeps_traceback(self, log_capture):
        """A non-transport exception (a code bug in the pipeline) must keep
        its traceback so an all-models-empty run stays diagnosable."""
        failed: Future = Future()
        failed.set_exception(TypeError("bug in the cleaning code"))
        with log_capture() as cap:
            out = list(
                _generated_to_appeals_text("m1", failed, _StubTemplateGenerator())
            )
        assert out == []
        warnings = [r for r in cap.records if r["level"].name == "WARNING"]
        assert len(warnings) == 1
        assert warnings[0]["exception"] is not None

    def test_full_result_yields_generated_appeal(self):
        ok: Future = Future()
        ok.set_result([("full", "A perfectly good appeal")])
        out = list(_generated_to_appeals_text("m2", ok, _StubTemplateGenerator()))
        assert [a.text for a in out] == ["A perfectly good appeal"]
        assert out[0].model_name == "m2"

    def test_reason_result_is_templated(self):
        ok: Future = Future()
        ok.set_result([("medically_necessary", "because reasons")])
        out = list(_generated_to_appeals_text("m3", ok, _StubTemplateGenerator()))
        assert [a.text for a in out] == ["templated: because reasons"]

    def test_one_failing_backend_does_not_kill_the_stream(self):
        """End-to-end through as_available_nested (which re-raises future
        exceptions mid-iteration): the failing backend is contained and the
        healthy backend's appeal still comes through."""
        gen = _StubTemplateGenerator()
        failed: Future = Future()
        failed.set_exception(aiohttp.ServerDisconnectedError())
        ok: Future = Future()
        ok.set_result([("full", "Surviving appeal")])
        futures = [
            executor.submit(_generated_to_appeals_text, "bad", failed, gen),
            executor.submit(_generated_to_appeals_text, "good", ok, gen),
        ]
        texts = [a.text for a in as_available_nested(futures)]
        assert texts == ["Surviving appeal"]
