import datetime
import time
from unittest import mock
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.health_status import health_status


class _InternalGood:
    model = "internal-good"
    external = False

    def model_is_ok(self):
        return True


class _InternalBad:
    model = "internal-bad"
    external = False

    def model_is_ok(self):
        return False


class _ExternalGood:
    model = "external-good"
    external = True

    def model_is_ok(self):
        return True


class _ExternalBad:
    model = "external-bad"
    external = True

    def model_is_ok(self):
        return False


class _SlowInternalGood:
    """Healthy internal backend whose check finishes after a short delay.

    Exercises the path where a check completes while still inside the wait
    window — it must be classified from its final state (alive), not dropped.
    """

    model = "slow-internal-good"
    external = False

    def model_is_ok(self):
        time.sleep(0.2)
        return True


class TestHealthStatus(TestCase):
    """Tests for cached model health endpoint behavior."""

    def setUp(self):
        # health_status is a module-level singleton, so its throttle
        # timestamp leaks across tests. Reset it so each test sees a
        # clean "no email sent yet" state.
        health_status._last_alert_sent_at = None

    def test_snapshot_shape(self):
        print("Getting router...")
        ml_router
        print("Getting snap...")
        snap = health_status.get_snapshot()
        print(f"Got {snap}")
        assert set(snap.keys()) == {"alive_models", "last_checked", "details"} | set(
            snap.keys()
        )  # basic shape

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_all_down_returns_zero(self, fake_router):
        """Simulate all backends failing their model list lookup."""

        class DownBackend:
            model = "down"

            @classmethod
            def models(cls):  # always raises
                raise Exception("unreachable")

            def model_is_ok(self):
                return False

        fake_router.all_models_by_cost = [DownBackend(), DownBackend()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        snap = health_status.get_snapshot()
        assert snap["alive_models"] == 0

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_some_up_counts(self, fake_router):
        """One healthy backend, one failing backend yields count==1."""

        from fighthealthinsurance.ml.ml_models import ModelDescription

        class GoodBackend:
            model = "good"

            @classmethod
            def models(cls):
                return [ModelDescription(cost=1, name="x", internal_name="good")]

            def model_is_ok(self):
                return True

        class BadBackend:
            model = "bad"

            @classmethod
            def models(cls):
                raise Exception("down")

            def model_is_ok(self):
                return False

        fake_router.all_models_by_cost = [GoodBackend(), BadBackend()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        snap = health_status.get_snapshot()
        assert snap["alive_models"] == 1

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_model_ok_true_for_healthy_backend(self, fake_router):
        """model_ok() returns True for a backend the last sweep found healthy."""
        good = _ExternalGood()
        fake_router.all_models_by_cost = [good, _ExternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        # Patch ensure_started so reading the cache doesn't spawn a real sweep.
        with mock.patch.object(health_status, "ensure_started"):
            assert health_status.model_ok(good) is True

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_model_ok_false_for_unhealthy_backend(self, fake_router):
        """model_ok() returns False for a backend the last sweep found down."""
        bad = _ExternalBad()
        fake_router.all_models_by_cost = [_ExternalGood(), bad]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        with mock.patch.object(health_status, "ensure_started"):
            assert health_status.model_ok(bad) is False

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_model_ok_none_for_unswept_backend(self, fake_router):
        """model_ok() returns None for a backend the sweep never saw, so the
        router fails open rather than excluding an unknown backend."""
        fake_router.all_models_by_cost = [_ExternalGood(), _ExternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        with mock.patch.object(health_status, "ensure_started"):
            assert health_status.model_ok(_InternalGood()) is None

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_all_internal_dead_sends_alert(self, fake_router):
        """All internal backends failing triggers email + error log."""
        fake_router.all_models_by_cost = [_InternalBad(), _ExternalGood()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 1
        assert alerts[0].to == ["support42@fighthealthinsurance.com"]
        assert "internal-bad" in alerts[0].body
        assert "internal_alive=0" in alerts[0].body

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_some_internal_alive_no_alert(self, fake_router):
        """At least one internal backend alive => no alert."""
        fake_router.all_models_by_cost = [_InternalGood(), _InternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert alerts == []

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_no_internal_models_sends_alert(self, fake_router):
        """Zero internal backends discovered also fires the alert."""
        fake_router.all_models_by_cost = [_ExternalGood(), _ExternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 1
        assert "internal_total=0" in alerts[0].body
        assert "no internal backends registered" in alerts[0].body

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_all_internal_alive_no_alert(self, fake_router):
        """All internal backends alive => no alert even if externals fail."""
        fake_router.all_models_by_cost = [_InternalGood(), _ExternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert alerts == []

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_slow_but_healthy_internal_counts_alive_no_alert(self, fake_router):
        """A slow-but-healthy internal backend is counted alive, not paged on."""
        fake_router.all_models_by_cost = [_SlowInternalGood()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        snap = health_status.get_snapshot()

        assert snap["alive_models"] == 1
        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert alerts == []

    def test_router_enumeration_failure_sends_distinct_alert(self):
        """Router enumeration failure must not masquerade as 'all internal dead'."""

        class BrokenRouter:
            @property
            def all_models_by_cost(self):
                raise RuntimeError("router not ready")

        from fighthealthinsurance.ml.health_status import _HealthStatus

        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", BrokenRouter()):
            _HealthStatus._refresh(health_status)

        enum_alerts = [
            m for m in mail.outbox if "could not enumerate" in m.subject.lower()
        ]
        dead_alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(enum_alerts) == 1
        assert dead_alerts == []
        assert "router not ready" in enum_alerts[0].body
        assert "RuntimeError" in enum_alerts[0].body

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_throttle_suppresses_email_within_window(self, fake_router):
        """Two consecutive refreshes inside the throttle window => one email."""
        fake_router.all_models_by_cost = [_InternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 1

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_throttle_releases_after_window(self, fake_router):
        """A refresh after the throttle window elapses sends a fresh email."""
        fake_router.all_models_by_cost = [_InternalBad()]
        from fighthealthinsurance.ml.health_status import (
            _HealthStatus,
            ALERT_THROTTLE_SECONDS,
        )
        from fighthealthinsurance.models import ModelHealthAlertState

        _HealthStatus._refresh(health_status)
        # Simulate the throttle window elapsing by backdating the stored row.
        backdated = timezone.now() - datetime.timedelta(
            seconds=ALERT_THROTTLE_SECONDS + 1
        )
        ModelHealthAlertState.objects.filter(key="internal_models_dead").update(
            last_alert_sent=backdated
        )
        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 2

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_throttle_is_shared_across_alert_subjects(self, fake_router):
        """All-dead and enumeration-failure share one throttle slot."""
        fake_router.all_models_by_cost = [_InternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)  # all-dead alert

        class BrokenRouter:
            @property
            def all_models_by_cost(self):
                raise RuntimeError("router not ready")

        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", BrokenRouter()):
            _HealthStatus._refresh(health_status)  # would normally enumerate-fail alert

        assert len(mail.outbox) == 1
        assert "internal models are dead" in mail.outbox[0].subject.lower()

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_db_throttle_dedupes_across_pods(self, fake_router):
        """Two independent instances (simulating two pods) sharing the DB send
        only one alert between them, even though their in-memory throttles are
        separate."""
        fake_router.all_models_by_cost = [_InternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        pod_a = _HealthStatus()
        pod_b = _HealthStatus()
        _HealthStatus._refresh(pod_a)
        _HealthStatus._refresh(pod_b)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 1

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_alert_falls_back_to_in_memory_when_db_unavailable(self, fake_router):
        """If the DB throttle errors, the per-pod in-memory throttle still
        caps a single pod at one email per window."""
        fake_router.all_models_by_cost = [_InternalBad()]
        from fighthealthinsurance.ml.health_status import _HealthStatus
        from fighthealthinsurance.models import ModelHealthAlertState

        with mock.patch.object(
            ModelHealthAlertState, "try_claim", side_effect=Exception("db down")
        ):
            _HealthStatus._refresh(health_status)  # in-memory claim -> sends
            _HealthStatus._refresh(health_status)  # in-memory throttle -> suppressed

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert len(alerts) == 1
