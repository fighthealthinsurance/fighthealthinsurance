from unittest import mock
from django.core import mail
from django.test import TestCase

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


class TestHealthStatus(TestCase):
    """Tests for cached model health endpoint behavior."""

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
