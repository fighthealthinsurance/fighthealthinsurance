"""Prometheus metrics + Sentry reliability events for the ML layer.

The APPEAL path got per-attempt DB rows in July 2026; this adds the missing
aggregate view (call counts/latency/failure reasons per backend, chat turn
outcomes) and Sentry messages for the total-failure cases.
"""

import asyncio
from unittest.mock import MagicMock, patch

import aiohttp

import pytest
from prometheus_client import REGISTRY

from fighthealthinsurance.ml.ml_metrics import (
    record_chat_turn,
    record_ml_call,
    record_ml_failure,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.reliability_events import capture_reliability_event


def _counter_value(name, **labels):
    # Every fhi_ml_* series carries the primary/backup leg label.
    if name.startswith("fhi_ml_"):
        labels.setdefault("leg", "primary")
    val = REGISTRY.get_sample_value(name, labels)
    return val or 0.0


class TestMetricHelpers:
    def test_record_ml_call_increments_counter_and_histogram(self):
        before = _counter_value(
            "fhi_ml_calls_total", model="metrics-model", outcome="ok"
        )
        record_ml_call("metrics-model", "ok", 1.5)
        after = _counter_value(
            "fhi_ml_calls_total", model="metrics-model", outcome="ok"
        )
        assert after == before + 1
        assert (
            REGISTRY.get_sample_value(
                "fhi_ml_call_seconds_count",
                {"model": "metrics-model", "leg": "primary"},
            )
            >= 1
        )

    def test_record_ml_failure_increments(self):
        before = _counter_value(
            "fhi_ml_call_failures_total",
            model="metrics-model",
            reason="transport_error",
        )
        record_ml_failure("metrics-model", "transport_error")
        after = _counter_value(
            "fhi_ml_call_failures_total",
            model="metrics-model",
            reason="transport_error",
        )
        assert after == before + 1

    def test_record_chat_turn_increments(self):
        before = _counter_value("fhi_chat_turns_total", outcome="ok")
        record_chat_turn("ok")
        after = _counter_value("fhi_chat_turns_total", outcome="ok")
        assert after == before + 1

    def test_helpers_never_raise_on_garbage(self):
        record_ml_call(None, "ok", -1)
        record_ml_failure(object(), "reason")
        record_chat_turn("weird")

    def test_executor_gauges_exported(self):
        assert (
            REGISTRY.get_sample_value(
                "fhi_executor_queued_tasks", {"pool": "interactive"}
            )
            is not None
        )
        assert (
            REGISTRY.get_sample_value("fhi_executor_threads", {"pool": "background"})
            is not None
        )


class TestTransportCallsAreCounted:
    @pytest.mark.asyncio
    async def test_failed_call_lands_in_metrics(self):
        model_name = "obs-dead-model"
        m = RemoteFullOpenLike("http://127.0.0.1:1/v1", "tok", model_name)
        calls_before = sum(
            _counter_value("fhi_ml_calls_total", model=model_name, outcome=o)
            for o in ("ok", "none", "timeout")
        )
        failures_before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="transport_error"
        )
        result = await m._infer_no_context(
            system_prompts=["sys"], prompt="hello", timeout=10.0
        )
        assert result is None
        calls_after = sum(
            _counter_value("fhi_ml_calls_total", model=model_name, outcome=o)
            for o in ("ok", "none", "timeout")
        )
        failures_after = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="transport_error"
        )
        assert calls_after > calls_before
        assert failures_after > failures_before


class TestBadBodyWarning:
    @pytest.mark.asyncio
    async def test_200_with_no_choices_warns_and_counts(
        self, make_fake_model_post, log_capture
    ):
        model_name = "obs-badbody-model"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        fake_post = make_fake_model_post(
            200, json_data={"object": "chat.completion", "choices": []}
        )
        before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="bad_body"
        )
        with patch("aiohttp.ClientSession.post", fake_post):
            with log_capture() as cap:
                result = await m._infer_no_context(
                    system_prompts=["sys"], prompt="hello", timeout=10.0
                )
        assert result is None
        warnings = cap.messages("WARNING")
        assert any("no choices" in w for w in warnings), warnings
        after = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="bad_body"
        )
        assert after > before


class TestReliabilityEvents:
    def test_captures_when_sentry_active(self):
        fake_client = MagicMock()
        fake_client.is_active.return_value = True
        with patch("sentry_sdk.get_client", return_value=fake_client), patch(
            "sentry_sdk.capture_message"
        ) as capture, patch("sentry_sdk.new_scope") as new_scope:
            new_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
            new_scope.return_value.__exit__ = MagicMock(return_value=False)
            capture_reliability_event("test_event", denial_id=42)
        capture.assert_called_once()
        assert "test_event" in capture.call_args.args[0]

    def test_noop_when_sentry_inactive(self):
        fake_client = MagicMock()
        fake_client.is_active.return_value = False
        with patch("sentry_sdk.get_client", return_value=fake_client), patch(
            "sentry_sdk.capture_message"
        ) as capture:
            capture_reliability_event("test_event", denial_id=42)
        capture.assert_not_called()

    def test_never_raises_when_sentry_missing(self):
        with patch("sentry_sdk.get_client", side_effect=RuntimeError("no sdk")):
            capture_reliability_event("test_event")


class TestEveryCallIsCountedOnce:
    """Every call lands in fhi_ml_calls_total exactly once, whatever happened
    to it, so failures / calls is a rate. HTTP errors used to be missing from
    the call counter entirely (only their reason was recorded), and two
    classified failures had no reason at all."""

    @pytest.mark.asyncio
    async def test_http_500_is_an_error_call_with_an_http_error_reason(
        self, make_fake_model_post
    ):
        model_name = "obs-http500-model"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        calls_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="error"
        )
        reasons_before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="http_error"
        )
        with patch("aiohttp.ClientSession.post", make_fake_model_post(500, body="boom")):
            try:
                result = await m._infer_no_context(
                    system_prompts=["sys"], prompt="hello", timeout=10.0
                )
            except aiohttp.ClientResponseError:
                result = None
        assert result is None
        assert (
            _counter_value("fhi_ml_calls_total", model=model_name, outcome="error")
            == calls_before + 1
        )
        assert (
            _counter_value(
                "fhi_ml_call_failures_total", model=model_name, reason="http_error"
            )
            == reasons_before + 1
        )

    @pytest.mark.asyncio
    async def test_context_overflow_has_its_own_reason(self, make_fake_model_post):
        model_name = "obs-overflow-model"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        body = (
            "This model's maximum context length is 8192 tokens. However, you "
            "requested 9000 tokens."
        )
        reason_before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="context_overflow"
        )
        none_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="none"
        )
        with patch("aiohttp.ClientSession.post", make_fake_model_post(400, body=body)):
            result = await m._infer_no_context(
                system_prompts=["sys"], prompt="hello", timeout=10.0
            )
        assert result is None
        assert (
            _counter_value(
                "fhi_ml_call_failures_total",
                model=model_name,
                reason="context_overflow",
            )
            == reason_before + 1
        )
        assert (
            _counter_value("fhi_ml_calls_total", model=model_name, outcome="none")
            == none_before + 1
        )

    @pytest.mark.asyncio
    async def test_missing_model_has_its_own_reason(self, make_fake_model_post):
        model_name = "obs-missing-model"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        body = '{"detail": "The model `obs-missing-model` does not exist."}'
        before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="missing_model"
        )
        with patch("aiohttp.ClientSession.post", make_fake_model_post(404, body=body)):
            result = await m._infer_no_context(
                system_prompts=["sys"], prompt="hello", timeout=10.0
            )
        assert result is None
        assert (
            _counter_value(
                "fhi_ml_call_failures_total", model=model_name, reason="missing_model"
            )
            == before + 1
        )

    @pytest.mark.asyncio
    async def test_cancelled_call_is_counted(self):
        """The losing leg of a dual-mode race is cancelled; it was a real
        request and used to vanish from every series."""
        m = RemoteFullOpenLike("http://h1:8000/v1", "tok", "obs-cancel-wire")
        m.name = "obs-cancel-registry"

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(30)

        with patch.object(m, "_RemoteOpenLike__infer", side_effect=hang):
            task = asyncio.ensure_future(
                m._RemoteOpenLike__timeout_infer(
                    system_prompt="s",
                    prompt="p",
                    patient_context=None,
                    plan_context=None,
                    temperature=0.5,
                    model="obs-cancel-wire",
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (
            _counter_value(
                "fhi_ml_calls_total", model="obs-cancel-registry", outcome="cancelled"
            )
            == 1
        )


class TestMetricIdentity:
    """The series are keyed by the registry name every other attribution
    source uses, plus a primary/backup leg -- not by the provider's wire id,
    which collapsed distinct registry models and split single ones."""

    def test_registry_name_wins_over_the_wire_id_once_stamped(self):
        m = RemoteFullOpenLike("http://h1:8000/v1", "tok", "wire-model")
        assert m._metric_identity(None) == ("wire-model", "primary")
        m.name = "fhi-2025"
        assert m._metric_identity(None) == ("fhi-2025", "primary")

    def test_backup_leg_is_told_apart_from_primary(self):
        m = RemoteFullOpenLike(
            "http://h1:8000/v1", "tok", "wire", backup_api_base="http://h2:9000/v1"
        )
        m.name = "fhi-2025"
        assert m._metric_identity("http://h2:9000/v1") == ("fhi-2025", "backup")
        assert m._metric_identity("http://h1:8000/v1") == ("fhi-2025", "primary")

    @pytest.mark.asyncio
    async def test_stamped_instance_records_under_the_registry_name(self):
        m = RemoteFullOpenLike("http://127.0.0.1:1/v1", "tok", "obs-wire-x")
        m.name = "obs-registry-x"
        before = _counter_value(
            "fhi_ml_call_failures_total", model="obs-registry-x", reason="transport_error"
        )
        result = await m._infer_no_context(
            system_prompts=["sys"], prompt="hello", timeout=10.0
        )
        assert result is None
        assert (
            _counter_value(
                "fhi_ml_call_failures_total",
                model="obs-registry-x",
                reason="transport_error",
            )
            == before + 1
        )
        assert (
            _counter_value(
                "fhi_ml_call_failures_total", model="obs-wire-x", reason="transport_error"
            )
            == 0
        )
