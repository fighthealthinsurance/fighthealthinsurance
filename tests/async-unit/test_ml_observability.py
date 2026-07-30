"""Prometheus metrics + Sentry reliability events for the ML layer.

The APPEAL path got per-attempt DB rows in July 2026; this adds the missing
aggregate view (call counts/latency/failure reasons per backend, chat turn
outcomes) and Sentry messages for the total-failure cases.
"""

from unittest.mock import MagicMock, patch

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
                "fhi_ml_call_seconds_count", {"model": "metrics-model"}
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
