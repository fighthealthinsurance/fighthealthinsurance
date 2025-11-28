from unittest import mock
from django.test import TestCase

from fighthealthinsurance.ml.health_status import health_status


class TestHealthStatus(TestCase):
    """Tests for cached model health endpoint behavior."""

    def test_snapshot_shape(self):
        snap = health_status.get_snapshot()
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

        fake_router.all_models_by_cost = [DownBackend(), DownBackend()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        snap = health_status.get_snapshot()
        assert snap["alive_models"] == 0
        assert len(snap["details"]) == 2
        assert all(not d["ok"] for d in snap["details"])  # all marked not ok

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_some_up_counts(self, fake_router):
        """One healthy backend, one failing backend yields count==1."""

        from fighthealthinsurance.ml.ml_models import ModelDescription

        class GoodBackend:
            model = "good"

            @classmethod
            def models(cls):
                return [ModelDescription(cost=1, name="x", internal_name="good")]

        class BadBackend:
            model = "bad"

            @classmethod
            def models(cls):
                raise Exception("down")

        fake_router.all_models_by_cost = [GoodBackend(), BadBackend()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        snap = health_status.get_snapshot()
        assert snap["alive_models"] == 1
        assert len(snap["details"]) == 2
        # Ensure one ok and one not ok
        oks = [d["ok"] for d in snap["details"]]
        assert sorted(oks) == [False, True]
