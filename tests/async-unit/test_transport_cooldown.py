"""Failure-driven transport cooldown between health sweeps.

Three transport-classified failures (refused / DNS / timeout) inside a minute
put the (api_base, model) pair on a short cooldown: later calls skip it
instantly instead of burning their whole timeout, the router stops selecting
the instance via is_available(), and the pair is probed live again once the
cooldown expires. The startup-probe path bypasses the skip entirely.
"""

import time
from unittest.mock import patch

import pytest

from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://cooldown.test/v1", "tok", "cool-model")


class TestStrikeAccounting:
    def test_three_strikes_start_a_cooldown(self):
        m = _model()
        for _ in range(3):
            m._note_transport_failure(m.api_base, m.model, "connection refused")
        assert m._transport_cooling(m.api_base, m.model) is True

    def test_two_strikes_do_not_cool(self):
        m = _model()
        for _ in range(2):
            m._note_transport_failure(m.api_base, m.model, "connection refused")
        assert m._transport_cooling(m.api_base, m.model) is False

    def test_stale_strikes_age_out_of_the_window(self):
        m = _model()
        with patch("fighthealthinsurance.ml.ml_models.time.monotonic") as clock:
            clock.return_value = 1000.0
            m._note_transport_failure(m.api_base, m.model, "refused")
            m._note_transport_failure(m.api_base, m.model, "refused")
            # Third failure arrives after the 60s window: the first two no
            # longer count.
            clock.return_value = 1100.0
            m._note_transport_failure(m.api_base, m.model, "refused")
            assert m._transport_cooling(m.api_base, m.model) is False

    def test_cooldown_expires_and_probes_again(self):
        m = _model()
        with patch("fighthealthinsurance.ml.ml_models.time.monotonic") as clock:
            clock.return_value = 1000.0
            for _ in range(3):
                m._note_transport_failure(m.api_base, m.model, "refused")
            assert m._transport_cooling(m.api_base, m.model) is True
            clock.return_value = 1000.0 + 121.0  # past the 120s default
            assert m._transport_cooling(m.api_base, m.model) is False

    def test_strikes_are_keyed_per_endpoint(self):
        m = _model()
        for _ in range(3):
            m._note_transport_failure("http://other.host/v1", "other", "refused")
        assert m._transport_cooling(m.api_base, m.model) is False
        assert m._transport_cooling("http://other.host/v1", "other") is True


class TestRouterVisibility:
    def test_is_available_false_while_all_endpoints_cool(self):
        m = _model()
        for _ in range(3):
            m._note_transport_failure(m.api_base, m.model, "refused")
        assert m.is_available() is False

    def test_is_available_true_when_backup_endpoint_is_healthy(self):
        m = RemoteFullOpenLike(
            "http://primary.test/v1",
            "tok",
            "cool-model",
            backup_api_base="http://backup.test/v1",
        )
        for _ in range(3):
            m._note_transport_failure("http://primary.test/v1", m.model, "refused")
        assert m.is_available() is True

    def test_is_available_recovers_after_cooldown(self):
        m = _model()
        with patch("fighthealthinsurance.ml.ml_models.time.monotonic") as clock:
            clock.return_value = 1000.0
            for _ in range(3):
                m._note_transport_failure(m.api_base, m.model, "refused")
            assert m.is_available() is False
            clock.return_value = 1000.0 + 200.0
            assert m.is_available() is True


class TestInferSkipGate:
    @pytest.mark.asyncio
    async def test_cooling_pair_is_skipped_without_network(self):
        """While cooling, _infer returns None fast with zero network calls."""
        m = _model()
        for _ in range(3):
            m._note_transport_failure(m.api_base, m.model, "refused")

        with patch("aiohttp.ClientSession") as session_cls:
            start = time.monotonic()
            result = await m._infer_no_context(
                system_prompts=["sys"], prompt="hello", timeout=30.0
            )
            elapsed = time.monotonic() - start
        assert result is None
        session_cls.assert_not_called()
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_probe_path_bypasses_the_cooldown(self):
        """raise_http_errors=True (startup probe) must always hit the wire so
        it reports current reality. (The injected constructor error is
        swallowed by __infer's generic handler; what matters is that the
        session was attempted at all despite the active cooldown.)"""
        m = _model()
        for _ in range(3):
            m._note_transport_failure(m.api_base, m.model, "refused")

        with patch("aiohttp.ClientSession") as session_cls:
            session_cls.side_effect = RuntimeError("wire was attempted")
            await m._infer_no_context(
                system_prompts=["sys"],
                prompt="hello",
                raise_http_errors=True,
                timeout=5.0,
            )
        session_cls.assert_called()

    @pytest.mark.asyncio
    async def test_repeated_refused_connections_trip_the_cooldown(self):
        """End-to-end: three real transport failures against a dead endpoint
        put the pair on cooldown so the fourth call never opens a session."""
        m = RemoteFullOpenLike("http://127.0.0.1:1/v1", "tok", "dead-model")
        for _ in range(3):
            result = await m._infer_no_context(
                system_prompts=["sys"], prompt="hello", timeout=10.0
            )
            assert result is None
        assert m._transport_cooling("http://127.0.0.1:1/v1", "dead-model") is True
        with patch("aiohttp.ClientSession") as session_cls:
            result = await m._infer_no_context(
                system_prompts=["sys"], prompt="hello", timeout=10.0
            )
        assert result is None
        session_cls.assert_not_called()
