"""Prometheus metrics + Sentry reliability events for the ML layer.

The APPEAL path got per-attempt DB rows in July 2026; this adds the missing
aggregate view (call counts/latency/failure reasons per backend, chat turn
outcomes) and Sentry messages for the total-failure cases.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import pytest
from prometheus_client import REGISTRY

from fighthealthinsurance.ml.ml_metrics import (
    ML_CALL_PURPOSE,
    ml_call_purpose,
    record_chat_turn,
    record_ml_call,
    record_ml_failure,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.reliability_events import capture_reliability_event


def _counter_value(name, **labels):
    # Every fhi_ml_call* series carries the primary/backup leg and the
    # purpose of the call (a bare helper call has no purpose in scope).
    if name.startswith("fhi_ml_call"):
        labels.setdefault("leg", "primary")
        labels.setdefault("purpose", "other")
    val = REGISTRY.get_sample_value(name, labels)
    return val or 0.0


def _completion(text):
    return {"object": "chat.completion", "choices": [{"message": {"content": text}}]}


def _checked_infer_kwargs(infer_type="medically_necessary", **overrides):
    kwargs = dict(
        prompt="denial text",
        patient_context=None,
        plan_context=None,
        infer_type=infer_type,
        pubmed_context=None,
        system_prompt="sys",
        temperature=0.5,
    )
    kwargs.update(overrides)
    return kwargs


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
                {"model": "metrics-model", "leg": "primary", "purpose": "other"},
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

class TestCallPurpose:
    """Every fhi_ml_call* series says why the call was made. Appeal generation
    shares its backend instances with chat, the health probes, entity
    extraction and summaries, so without the label an appeal-only outage was
    diluted by the traffic that kept succeeding, and the appeal latency
    quantile by the probes' "Hello"s."""

    def test_default_is_other_and_unknown_purposes_collapse_to_it(self):
        assert ML_CALL_PURPOSE.get() == "other"
        with ml_call_purpose("appeal"):
            assert ML_CALL_PURPOSE.get() == "appeal"
            with ml_call_purpose("brand-new-purpose"):
                assert ML_CALL_PURPOSE.get() == "other"
            assert ML_CALL_PURPOSE.get() == "appeal"
        assert ML_CALL_PURPOSE.get() == "other"

    def test_recording_helpers_read_the_purpose_in_scope(self):
        model_name = "obs-purpose-helper"
        calls_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="chat"
        )
        failures_before = _counter_value(
            "fhi_ml_call_failures_total",
            model=model_name,
            reason="http_error",
            purpose="chat",
        )
        with ml_call_purpose("chat"):
            record_ml_call(model_name, "ok", 0.1)
            record_ml_failure(model_name, "http_error")
        assert (
            _counter_value(
                "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="chat"
            )
            == calls_before + 1
        )
        assert (
            _counter_value(
                "fhi_ml_call_failures_total",
                model=model_name,
                reason="http_error",
                purpose="chat",
            )
            == failures_before + 1
        )
        assert (
            REGISTRY.get_sample_value(
                "fhi_ml_call_seconds_count",
                {"model": model_name, "leg": "primary", "purpose": "chat"},
            )
            >= 1
        )

    @pytest.mark.asyncio
    async def test_checked_infer_calls_land_in_the_appeal_series(
        self, make_fake_model_post
    ):
        model_name = "obs-purpose-appeal"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        appeal_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="appeal"
        )
        other_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="ok"
        )
        fake_post = make_fake_model_post(
            200,
            json_data=_completion(
                "The denied service is medically necessary for this patient."
            ),
        )
        with patch("aiohttp.ClientSession.post", fake_post):
            result = await m._checked_infer(**_checked_infer_kwargs())
        assert result and result[0][0] == "medically_necessary"
        assert (
            _counter_value(
                "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="appeal"
            )
            == appeal_before + 1
        )
        assert (
            _counter_value("fhi_ml_calls_total", model=model_name, outcome="ok")
            == other_before
        )

    @pytest.mark.asyncio
    async def test_probe_calls_land_in_the_probe_series(self, make_fake_model_post):
        model_name = "obs-purpose-probe"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="probe"
        )
        fake_post = make_fake_model_post(200, json_data=_completion("Hello there."))
        with patch("aiohttp.ClientSession.post", fake_post):
            ok, error = await m.probe(timeout=10.0)
        assert ok, error
        assert (
            _counter_value(
                "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="probe"
            )
            == before + 1
        )

    @pytest.mark.asyncio
    async def test_chat_turns_run_under_the_chat_purpose(self):
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", "obs-chat")
        seen = []

        async def fake_infer(*args, **kwargs):
            seen.append(ML_CALL_PURPOSE.get())
            return ("Happy to help with that.🐼Greeting.", None)

        m._infer = fake_infer  # type: ignore[method-assign]
        reply = await m.generate_chat_response("hello there")
        assert reply
        assert seen and set(seen) == {"chat"}
        assert ML_CALL_PURPOSE.get() == "other"


class TestCheckedResultsAreCounted:
    """fhi_ml_calls_total's ok means the transport returned a body. Whether the
    appeal path could USE the body was decided later, in _checked_infer, and
    exported nowhere: a backend that was up but answered every prompt with a
    refusal read as 100% ok while every draft it produced was rejected and
    filed as a no_output attempt. fhi_ml_results_total records that
    decision, once per invocation."""

    @pytest.mark.asyncio
    async def test_a_usable_completion_is_accepted(self, make_fake_model_post):
        model_name = "obs-result-accepted"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        before = _counter_value(
            "fhi_ml_results_total",
            model=model_name,
            infer_type="medically_necessary",
            result="accepted",
        )
        fake_post = make_fake_model_post(
            200,
            json_data=_completion(
                "The denied service is medically necessary for this patient."
            ),
        )
        with patch("aiohttp.ClientSession.post", fake_post):
            result = await m._checked_infer(**_checked_infer_kwargs())
        assert result
        assert (
            _counter_value(
                "fhi_ml_results_total",
                model=model_name,
                infer_type="medically_necessary",
                result="accepted",
            )
            == before + 1
        )

    @pytest.mark.asyncio
    async def test_a_refusal_is_rejected_while_its_calls_read_ok(
        self, make_fake_model_post
    ):
        model_name = "obs-result-refusal"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        ok_before = _counter_value(
            "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="appeal"
        )
        rejected_before = _counter_value(
            "fhi_ml_results_total",
            model=model_name,
            infer_type="medically_necessary",
            result="rejected_bad_result",
        )
        fake_post = make_fake_model_post(
            200,
            json_data=_completion(
                "I cannot directly create an appeal letter for you."
            ),
        )
        with patch("aiohttp.ClientSession.post", fake_post):
            result = await m._checked_infer(**_checked_infer_kwargs())
        assert result == []
        # The call and its one retry both answered: two ok calls ...
        assert (
            _counter_value(
                "fhi_ml_calls_total", model=model_name, outcome="ok", purpose="appeal"
            )
            == ok_before + 2
        )
        # ... and one rejected result, which is what the alert needs.
        assert (
            _counter_value(
                "fhi_ml_results_total",
                model=model_name,
                infer_type="medically_necessary",
                result="rejected_bad_result",
            )
            == rejected_before + 1
        )

    @pytest.mark.asyncio
    async def test_a_passed_deadline_is_a_skip_and_makes_no_call(self):
        model_name = "obs-result-deadline"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value="unused"
        )
        before = _counter_value(
            "fhi_ml_results_total",
            model=model_name,
            infer_type="full",
            result="skipped_deadline",
        )
        result = await m._checked_infer(
            **_checked_infer_kwargs(infer_type="full", deadline=time.monotonic() - 1)
        )
        assert result == []
        m._infer_no_context.assert_not_called()
        assert (
            _counter_value(
                "fhi_ml_results_total",
                model=model_name,
                infer_type="full",
                result="skipped_deadline",
            )
            == before + 1
        )

    @pytest.mark.asyncio
    async def test_a_completion_the_cleaners_empty_is_rejected_for_repetition(self):
        model_name = "obs-result-repetition"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value="The denied service is medically necessary for this patient."
        )
        before = _counter_value(
            "fhi_ml_results_total",
            model=model_name,
            infer_type="medically_necessary",
            result="rejected_repetition",
        )
        with patch(
            "fighthealthinsurance.ml.ml_models.remove_repeated_sentences",
            return_value=None,
        ):
            result = await m._checked_infer(**_checked_infer_kwargs())
        assert result == []
        assert (
            _counter_value(
                "fhi_ml_results_total",
                model=model_name,
                infer_type="medically_necessary",
                result="rejected_repetition",
            )
            == before + 1
        )


class TestClassifiedReasonsSurviveTheProbeRaise:
    """The status handler classifies a context overflow or a missing model,
    but for a raise_http_errors caller (the probes) it re-raised before
    filing the reason, and the outer handler filed the call as a bare
    http_error: the specific series never moved for exactly the callers
    that exist to notice."""

    @pytest.mark.asyncio
    async def test_probe_context_overflow_keeps_its_reason(self, make_fake_model_post):
        model_name = "obs-probe-overflow"
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", model_name)
        body = (
            "This model's maximum context length is 8192 tokens. However, you "
            "requested 9000 tokens."
        )
        overflow_before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="context_overflow"
        )
        http_before = _counter_value(
            "fhi_ml_call_failures_total", model=model_name, reason="http_error"
        )
        with patch("aiohttp.ClientSession.post", make_fake_model_post(400, body=body)):
            with pytest.raises(aiohttp.ClientResponseError):
                await m._infer_no_context(
                    system_prompts=["sys"],
                    prompt="hello",
                    timeout=10.0,
                    raise_http_errors=True,
                )
        assert (
            _counter_value(
                "fhi_ml_call_failures_total", model=model_name, reason="context_overflow"
            )
            == overflow_before + 1
        )
        # Filed once, under its reason, not again as a bare http_error.
        assert (
            _counter_value(
                "fhi_ml_call_failures_total", model=model_name, reason="http_error"
            )
            == http_before
        )
