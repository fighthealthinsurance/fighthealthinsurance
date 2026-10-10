import datetime
import time
from unittest import mock
from django.core import mail
from django.test import TestCase, override_settings
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


class _ContextOnlyGood:
    """A citations backend: in the router's context-only pool, never in a
    generation pool."""

    model = "sonar"
    external = True
    context_only = True

    def model_is_ok(self):
        return True


class _InternalBadWithReason(_InternalBad):
    """An internal backend that recorded why it is unusable on this pod."""

    def unavailable_reason(self):
        return "unreachable (connection refused)"


class _ExternalOutOfCredit(_ExternalBad):
    """An external backend whose recorded reason names our billing state."""

    def unavailable_reason(self):
        return "out of credit (HTTP 402)"


class _ExternalReasonRaises(_ExternalBad):
    def unavailable_reason(self):
        raise RuntimeError("flags unreadable")


class _ChatOnlyBad:
    """One of chat's own outside models: in no generation pool."""

    model = "zai-org/GLM-5.3-Flash"
    external = True

    def model_is_ok(self):
        return False


class TestHealthStatus(TestCase):
    """Tests for cached model health endpoint behavior."""

    def setUp(self):
        # health_status is a module-level singleton, so its throttle
        # timestamp leaks across tests. Reset it so each test sees a
        # clean "no email sent yet" state.
        health_status._last_alert_sent_at = None

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_context_only_backends_are_swept_too(self, fake_router):
        """They are in no generation pool, so nothing checked them between
        deploys and model_ok answered None for them forever."""
        citations = _ContextOnlyGood()
        fake_router.all_models_by_cost = [_InternalGood()]
        fake_router.context_only_models_by_cost = [citations]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        assert health_status.model_ok(citations) is True

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_a_context_only_backend_never_counts_as_alive(self, fake_router):
        """alive_models tells the public status widget a model is ready to
        write an appeal. A citations backend can't write one, so with every
        generation backend down the count is zero even while it is healthy."""
        fake_router.all_models_by_cost = [_InternalBad()]
        fake_router.context_only_models_by_cost = [_ContextOnlyGood()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        assert health_status.get_snapshot()["alive_models"] == 0

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_a_chat_only_backend_never_counts_as_alive(self, fake_router):
        """Chat's own outside models sit in no generation pool either, so a
        healthy one says nothing about whether an appeal can be written."""
        fake_router.all_models_by_cost = [_InternalBad()]
        fake_router.context_only_models_by_cost = []
        fake_router.chat_outside_models_by_name = {"chat-only": _ExternalGood()}
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        assert health_status.get_snapshot()["alive_models"] == 0

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_the_alert_names_the_reason_an_internal_backend_recorded(self, fake_router):
        """The staff alert says why the backend is down, not a bare
        "not ok"."""
        fake_router.all_models_by_cost = [_InternalBadWithReason()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        alerts = [
            m for m in mail.outbox if "internal models are dead" in m.subject.lower()
        ]
        assert "internal-bad: unreachable (connection refused)" in alerts[0].body

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_public_details_never_carry_the_recorded_reason(self, fake_router):
        """The public snapshot must not reveal our billing or key state."""
        fake_router.all_models_by_cost = [_InternalGood(), _ExternalOutOfCredit()]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)

        assert health_status.get_snapshot()["details"] == [
            {"name": "external-bad", "ok": False, "error": "not ok"}
        ]

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router")
    def test_details_list_failing_externals_and_no_internal_failures(
        self, fake_router
    ):
        """The public snapshot names failing external providers, timed out or
        not; internal failures stay out of it (their names are internal wire
        paths) and drive the alert instead."""
        fake_router.all_models_by_cost = [
            _InternalBad(),
            _ExternalBad(),
            _ExternalGood(),
        ]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        _HealthStatus._refresh(health_status)
        details = health_status.get_snapshot()["details"]

        assert [d["name"] for d in details] == ["external-bad"]
        assert details[0]["ok"] is False

    def test_a_failing_sweep_still_arms_the_next_one(self):
        """An exception in the sweep used to end the timer chain for the life
        of the process, freezing the cached map every routing decision reads."""
        from fighthealthinsurance.ml.health_status import _HealthStatus

        with mock.patch.object(
            _HealthStatus, "_refresh_unlocked", side_effect=RuntimeError("boom")
        ), mock.patch.object(_HealthStatus, "_schedule_refresh") as rearm:
            _HealthStatus._refresh(health_status)

        rearm.assert_called_once()

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
    def test_last_sweep_result_pairs_verdict_with_sweep_time(self, fake_router):
        """last_sweep_result() reports a backend's verdict and when that sweep
        ran, for the staff status page."""
        bad = _ExternalBad()
        fake_router.all_models_by_cost = [_InternalGood(), bad]
        from fighthealthinsurance.ml.health_status import _HealthStatus

        before = time.time()
        _HealthStatus._refresh(health_status)

        ok, checked_at = health_status.last_sweep_result(bad)
        assert ok is False and before <= checked_at <= time.time()

    def test_last_sweep_result_does_not_start_the_sweep(self):
        """Reading the cache for the status page must not kick off a sweep."""
        with mock.patch.object(health_status, "ensure_started") as ensure_started:
            health_status.last_sweep_result(_InternalGood())
        ensure_started.assert_not_called()

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


class TestComputeModelHealthDetails(TestCase):
    """The staff System Status breakdown: why a backend is down, and chat's
    own outside models listed but never counted as able to draft."""

    def _router(self, models, chat_outside=None):
        router = mock.MagicMock()
        router.all_models_by_cost = models
        router.context_only_models_by_cost = []
        router.chat_outside_models_by_name = chat_outside or {}
        return router

    def _details(self, router):
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", router):
            return compute_model_health_details(timeout_seconds=2)

    def _by_name(self, router):
        return {d["name"]: d for d in self._details(router)}

    def test_a_down_backend_shows_the_reason_it_recorded(self):
        details = self._by_name(self._router([_ExternalOutOfCredit()]))
        assert details["external-bad"]["error"] == "out of credit (HTTP 402)"

    def test_a_down_backend_without_a_reason_reads_not_ok(self):
        details = self._by_name(self._router([_ExternalBad()]))
        assert details["external-bad"]["error"] == "not ok"

    def test_a_reason_accessor_that_raises_reads_not_ok(self):
        details = self._by_name(self._router([_ExternalReasonRaises()]))
        assert details["external-bad"]["error"] == "not ok"

    def test_a_chat_only_backend_is_listed_as_chat_only(self):
        """So staff see a dead DeepInfra chat model."""
        router = self._router([_InternalGood()], {"glm": _ChatOnlyBad()})
        details = self._by_name(router)
        assert {name: d["chat_only"] for name, d in details.items()} == {
            "internal-good": False,
            "zai-org/GLM-5.3-Flash": True,
        }

    def test_a_chat_only_row_is_left_out_of_drafting_counts(self):
        """Callers count what can draft by leaving context_only rows out."""
        router = self._router([_InternalGood()], {"glm": _ChatOnlyBad()})
        details = self._by_name(router)
        assert details["zai-org/GLM-5.3-Flash"]["context_only"] is True

    def test_a_chat_model_already_in_a_generation_pool_is_listed_once(self):
        shared = _ExternalGood()
        details = self._details(self._router([shared], {"shared": shared}))
        assert [(d["name"], d["chat_only"]) for d in details] == [
            ("external-good", False)
        ]


class TestBackgroundSweepGate(TestCase):
    """``ML_HEALTH_BACKGROUND_SWEEP`` gates the self-re-arming timer chain.

    The sweep writes the cross-pod alert-throttle row on every pass, so left
    running under the test configs it lands DB writes in the middle of
    unrelated tests and can lock the table against the teardown flush.
    """

    def _make_status(self):
        from fighthealthinsurance.ml.health_status import _HealthStatus

        return _HealthStatus()

    @override_settings(ML_HEALTH_BACKGROUND_SWEEP=False)
    def test_ensure_started_is_noop_when_disabled(self):
        status = self._make_status()
        with mock.patch(
            "fighthealthinsurance.ml.health_status.threading.Thread"
        ) as thread:
            status.ensure_started()
        thread.assert_not_called()
        assert status._sweep_started is False

    @override_settings(ML_HEALTH_BACKGROUND_SWEEP=False)
    def test_schedule_refresh_arms_no_timer_when_disabled(self):
        """A direct ``_refresh`` (what the alert tests drive) must not leave a
        timer behind that fires during some later test."""
        status = self._make_status()
        with mock.patch(
            "fighthealthinsurance.ml.health_status.threading.Timer"
        ) as timer:
            status._schedule_refresh()
        timer.assert_not_called()
        assert status._timer is None

    @override_settings(ML_HEALTH_BACKGROUND_SWEEP=True)
    def test_ensure_started_starts_the_sweep_exactly_once_when_enabled(self):
        status = self._make_status()
        with mock.patch(
            "fighthealthinsurance.ml.health_status.threading.Thread"
        ) as thread:
            status.ensure_started()
            status.ensure_started()  # idempotent
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
