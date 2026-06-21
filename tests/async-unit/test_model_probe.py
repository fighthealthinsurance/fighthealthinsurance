"""Tests for the startup "Hello" model probe.

The probe is a one-off, end-to-end reachability check used at startup to
surface misconfigured/over-quota backends. It is intentionally separate from
the free per-request liveness check (``model_is_ok``).
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.ml.ml_router import MLRouter


class _FakeModel:
    """Minimal stand-in exposing the async ``probe`` contract."""

    def __init__(self, name, ok=True, err=None, exc=None):
        self.model = name
        self._ok = ok
        self._err = err
        self._exc = exc

    async def probe(self, timeout: float = 20.0):
        if self._exc is not None:
            raise self._exc
        return (self._ok, self._err)

    def __str__(self):
        return f"FakeModel({self.model})"


class TestRemoteModelProbe:
    def _model(self) -> RemoteFullOpenLike:
        return RemoteFullOpenLike("http://test-api.com", "tok", "test-model")

    @pytest.mark.asyncio
    async def test_probe_ok_on_nonempty_response(self):
        model = self._model()
        with patch.object(
            model,
            "_infer_no_context",
            new_callable=AsyncMock,
            return_value="Hello there!",
        ):
            ok, err = await model.probe(timeout=5)
        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_probe_fails_on_empty_response(self):
        model = self._model()
        for value in (None, "", "   "):
            with patch.object(
                model,
                "_infer_no_context",
                new_callable=AsyncMock,
                return_value=value,
            ):
                ok, err = await model.probe(timeout=5)
            assert ok is False
            assert err == "empty or no response"

    @pytest.mark.asyncio
    async def test_probe_reports_exception_without_raising(self):
        model = self._model()
        with patch.object(
            model,
            "_infer_no_context",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            ok, err = await model.probe(timeout=5)
        assert ok is False
        assert "boom" in err

    @pytest.mark.asyncio
    async def test_probe_times_out(self):
        model = self._model()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(1)
            return "hi"

        # Patch with a plain coroutine function (not AsyncMock) so wait_for
        # awaits the real sleeping coroutine and the timeout actually fires.
        with patch.object(model, "_infer_no_context", _slow):
            ok, err = await model.probe(timeout=0.01)
        assert ok is False
        assert "timeout" in err


class TestProbeAllModels:
    @pytest.mark.asyncio
    async def test_aggregates_results_across_pools_and_dedups(self):
        router = MLRouter()
        good = _FakeModel("good", ok=True)
        bad = _FakeModel("bad", ok=False, err="empty or no response")
        ctx = _FakeModel("ctx-only", ok=False, err="HTTP 401")
        router.all_models_by_cost = [good, bad, ctx]
        # ctx also appears in the context-only pool; it must be probed once.
        router.context_only_models_by_cost = [ctx]

        results = await router.probe_all_models(per_model_timeout=1.0)

        assert len(results) == 3  # deduped by identity
        ok_map = {name: ok for name, ok, _ in results}
        assert ok_map["FakeModel(good)"] is True
        assert ok_map["FakeModel(bad)"] is False
        assert ok_map["FakeModel(ctx-only)"] is False

    @pytest.mark.asyncio
    async def test_failing_probe_does_not_break_others(self):
        router = MLRouter()
        good = _FakeModel("good", ok=True)
        explodes = _FakeModel("explodes", exc=RuntimeError("kaboom"))
        router.all_models_by_cost = [good, explodes]
        router.context_only_models_by_cost = []

        results = await router.probe_all_models(per_model_timeout=1.0)

        ok_map = {name: ok for name, ok, _ in results}
        assert ok_map["FakeModel(good)"] is True
        assert ok_map["FakeModel(explodes)"] is False
        err_map = {name: err for name, _, err in results}
        assert "kaboom" in err_map["FakeModel(explodes)"]

    @pytest.mark.asyncio
    async def test_no_models_returns_empty(self):
        router = MLRouter()
        router.all_models_by_cost = []
        router.context_only_models_by_cost = []
        results = await router.probe_all_models()
        assert results == []
