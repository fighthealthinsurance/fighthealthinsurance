import concurrent.futures
import datetime
import os
import threading
import time
from unittest import mock
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.health_status import (
    DOWN_RECHECK_SECONDS,
    PAUSED_FOR_CREDIT,
    REFRESH_INTERVAL_SECONDS,
    _HealthStatus,
    _model_key,
    health_status,
    live_problem,
)


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


class _ListedButRefused:
    """An outside model whose /models still lists it while its key is
    refused for inference."""

    model = "google/gemma-4-26B-A4B-it"
    external = True

    def model_is_ok(self):
        return True

    def unavailable_reason(self):
        return "refused (HTTP 401)"


class _ListedAnthropic:
    """An outside model whose check passes whatever its provider's credit."""

    model = "claude-haiku-4-5"
    external = True
    SPEND_PROVIDER = spend.ANTHROPIC

    def model_is_ok(self):
        return True


class _InternalListedButCooling:
    model = "internal-cooling"
    external = False

    def model_is_ok(self):
        return True

    def unavailable_reason(self):
        return "unreachable (ConnectError)"


class _Flaky:
    """A backend whose /models probe answers as ``healthy`` says."""

    external = False

    def __init__(self, model="flaky", healthy=False):
        self.model = model
        self.healthy = healthy
        self.probes = 0

    def model_is_ok(self):
        self.probes += 1
        return self.healthy


class _FlakyOutside(_Flaky):
    """An outside backend whose probe answers as ``healthy`` says: the public
    snapshot lists it by name while it is down."""

    external = True


class _FlakyRefused(_FlakyOutside):
    """One whose key this pod saw refused, which a passing /models probe
    says nothing about."""

    def unavailable_reason(self):
        return "refused (HTTP 401)"


class _CheckedLive(_Flaky):
    """A backend with its own live signal: routing never reads the sweep for
    it."""

    health_checked_live = True


class _WaitsForAll:
    """A probe that passes only once every other probe has started too."""

    external = False

    def __init__(self, barrier, n):
        self.model = f"backend-{n}"
        self.barrier = barrier

    def model_is_ok(self):
        self.barrier.wait()
        return True


class _NeverRunsExecutor:
    """Queues every probe and runs none, like a pool whose workers are all
    still busy at the deadline."""

    def __init__(self, max_workers=None):
        pass

    def submit(self, fn, *args, **kwargs):
        return concurrent.futures.Future()

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _router(models, chat_outside=None):
    router = mock.MagicMock()
    router.all_models_by_cost = list(models)
    router.context_only_models_by_cost = []
    router.chat_outside_models_by_name = chat_outside or {}
    return router


def _swept(models, status=None, chat_outside=None):
    """A health status (a fresh one unless given) after one sweep over
    ``models`` (and chat's own ``chat_outside`` ones), its snapshot read as
    it stands from then on."""
    status = status or _HealthStatus()
    with mock.patch(
        "fighthealthinsurance.ml.ml_router.ml_router", _router(models, chat_outside)
    ):
        status._refresh_unlocked()
    status._initialized = True
    return status


def _outside_backend():
    """A real outside backend at a host that can never resolve."""
    from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

    return RemoteFullOpenLike("http://outside.example.invalid/v1", "", "m")


def _connect_failures(backend, detail="ConnectError", connect_failed=True):
    """Enough failures in a row to start the pair's transport cooldown."""
    for _ in range(backend.TRANSPORT_STRIKES_TO_COOL):
        backend._note_transport_failure(
            backend.api_base, backend.model, detail, connect_failed=connect_failed
        )


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

    # A /models probe passes for a model whose key is refused or whose
    # provider is paused for credit (listing models is free), so this pod's
    # live signals take the row down and say why.

    def test_a_refused_backend_whose_probe_passes_reads_down(self):
        details = self._by_name(self._router([_ListedButRefused()]))
        assert details["google/gemma-4-26B-A4B-it"]["ok"] is False

    def test_a_refused_backend_whose_probe_passes_shows_the_refusal(self):
        details = self._by_name(self._router([_ListedButRefused()]))
        assert details["google/gemma-4-26B-A4B-it"]["error"] == "refused (HTTP 401)"

    def test_a_credit_paused_backend_whose_probe_passes_shows_the_pause(self):
        spend.pause(spend.ANTHROPIC, reason="test")
        details = self._by_name(self._router([_ListedAnthropic()]))
        assert details["claude-haiku-4-5"]["error"] == PAUSED_FOR_CREDIT

    def test_a_pause_on_another_provider_leaves_the_row_up(self):
        spend.pause(spend.DEEPINFRA, reason="test")
        details = self._by_name(self._router([_ListedAnthropic()]))
        assert details["claude-haiku-4-5"]["ok"] is True

    def test_a_timed_out_probe_shows_the_live_reason_instead(self):
        release = threading.Event()

        class _HungAndRefused(_ListedButRefused):
            def model_is_ok(self):
                release.wait(timeout=10)
                return True

        try:
            from fighthealthinsurance.ml.health_status import (
                compute_model_health_details,
            )

            with mock.patch(
                "fighthealthinsurance.ml.ml_router.ml_router",
                self._router([_HungAndRefused()]),
            ):
                details = compute_model_health_details(timeout_seconds=1)
        finally:
            release.set()
        assert details[0]["error"] == "refused (HTTP 401)"

    def test_every_probe_starts_at_once(self):
        """A probe queued behind eight slow ones used to read as timed out."""
        barrier = threading.Barrier(9, timeout=3)
        models = [_WaitsForAll(barrier, n) for n in range(9)]
        details = self._details(self._router(models))
        assert [d["ok"] for d in details] == [True] * 9


class TestLiveProblem(TestCase):
    """live_problem: why this pod's own signals say a backend cannot answer,
    shared by every staff page and the snapshot. Never raises."""

    def test_a_recorded_reason_is_the_problem(self):
        assert live_problem(_ListedButRefused()) == "refused (HTTP 401)"

    def test_a_credit_pause_on_its_provider_is_the_problem(self):
        spend.pause(spend.ANTHROPIC, reason="test")
        assert live_problem(_ListedAnthropic()) == PAUSED_FOR_CREDIT

    def test_a_pause_on_one_use_only_is_no_problem(self):
        """A spent per-use budget is not a dead provider."""
        spend.pause(spend.ANTHROPIC, spend.CHAT, reason="test")
        assert live_problem(_ListedAnthropic()) is None

    def test_a_reason_accessor_that_raises_reads_as_no_problem(self):
        class _Broken(_ListedAnthropic):
            def unavailable_reason(self):
                raise RuntimeError("flags unreadable")

        assert live_problem(_Broken()) is None

    def test_a_pause_that_cannot_be_read_is_no_problem(self):
        with mock.patch.object(spend, "paused", side_effect=RuntimeError("boom")):
            assert live_problem(_ListedAnthropic()) is None

    def test_a_mock_backend_reads_as_no_problem(self):
        assert live_problem(mock.MagicMock()) is None


class TestSnapshotLiveOverlay(TestCase):
    """The public snapshot reads live problems at each read: a backend whose
    probe passed but that cannot answer now is not counted ready, is listed
    as "not ok" with no reason, and never lands in the routing map."""

    def test_a_credit_paused_backend_is_not_counted_alive(self):
        spend.pause(spend.ANTHROPIC, reason="test")
        status = _swept([_ListedAnthropic()])
        assert status.get_snapshot()["alive_models"] == 0

    def test_a_refused_backend_is_listed_as_not_ok_without_its_reason(self):
        status = _swept([_ListedButRefused()])
        assert status.get_snapshot()["details"] == [
            {"name": "google/gemma-4-26B-A4B-it", "ok": False, "error": "not ok"}
        ]

    def test_a_pause_that_starts_after_the_sweep_shows_at_the_next_read(self):
        status = _swept([_ListedAnthropic()])
        spend.pause(spend.ANTHROPIC, reason="test")
        assert status.get_snapshot()["alive_models"] == 0

    def test_a_pause_that_clears_is_counted_again_at_the_next_read(self):
        spend.pause(spend.ANTHROPIC, reason="test")
        status = _swept([_ListedAnthropic()])
        spend._ledger.reset_for_tests()
        assert status.get_snapshot()["alive_models"] == 1

    def test_a_live_problem_never_reaches_the_routing_map(self):
        """The router reads the map hourly for some backends: a pause or a
        refusal written there would outlast its clearing by up to an hour."""
        spend.pause(spend.ANTHROPIC, reason="test")
        backend = _ListedAnthropic()
        status = _swept([backend])
        assert status.model_ok(backend) is True

    def test_an_internal_live_problem_is_counted_but_not_listed(self):
        status = _swept([_InternalListedButCooling()])
        snapshot = status.get_snapshot()
        assert (snapshot["alive_models"], snapshot["details"]) == (0, [])

    def test_a_chat_only_live_problem_changes_no_count(self):
        status = _HealthStatus()
        router = _router([_InternalGood()], {"gemma": _ListedButRefused()})
        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", router):
            status._refresh_unlocked()
        status._initialized = True
        assert status.get_snapshot()["alive_models"] == 1


class TestSweepPool(TestCase):
    """Every probe gets its own worker, and a probe that never started
    measured nothing, so it keeps last round's result."""

    def test_every_probe_starts_at_once(self):
        """With eight workers the ninth probe waited behind the rest and was
        marked down for the hour."""
        barrier = threading.Barrier(9, timeout=3)
        models = [_WaitsForAll(barrier, n) for n in range(9)]
        status = _swept(models)
        assert [status.model_ok(m) for m in models] == [True] * 9

    def _sweep_that_never_runs(self, status, models):
        with mock.patch.object(
            concurrent.futures, "ThreadPoolExecutor", _NeverRunsExecutor
        ), mock.patch("fighthealthinsurance.ml.health_status.SWEEP_TIMEOUT_SECONDS", 0):
            return _swept(models, status)

    def test_a_probe_that_never_started_keeps_last_rounds_result(self):
        backend = _ExternalGood()
        status = self._sweep_that_never_runs(_swept([backend]), [backend])
        assert status.model_ok(backend) is True

    def test_a_probe_that_never_started_is_not_listed_as_failing(self):
        backend = _ExternalGood()
        status = self._sweep_that_never_runs(_swept([backend]), [backend])
        assert status.get_snapshot()["details"] == []

    def test_a_first_probe_that_never_started_stays_unchecked(self):
        backend = _ExternalGood()
        status = self._sweep_that_never_runs(_HealthStatus(), [backend])
        assert status.model_ok(backend) is None


class TestDownRecheck(TestCase):
    """Between hourly sweeps, the backends the last one marked down are
    probed again every few minutes, so one failed probe does not keep a
    backend out of routing (and the appeal fan-out) for the hour."""

    def setUp(self):
        # tox runs with FHI_HEALTH_FAST=1 (a 5s sweep); these tests time the
        # hourly sweep.
        env = mock.patch.dict(os.environ, {"FHI_HEALTH_FAST": "0"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_recheck_puts_a_recovered_backend_back_in_routing(self):
        backend = _Flaky()
        status = _swept([backend])
        backend.healthy = True
        status._recheck_down()
        assert status.model_ok(backend) is True

    def test_a_recheck_leaves_a_backend_still_down_out(self):
        backend = _Flaky()
        status = _swept([backend])
        status._recheck_down()
        assert status.model_ok(backend) is False

    def test_a_recheck_probes_only_the_backends_marked_down(self):
        healthy = _Flaky("healthy", healthy=True)
        status = _swept([healthy, _Flaky()])
        status._recheck_down()
        assert healthy.probes == 1

    def test_a_backend_with_its_own_live_signal_is_not_rechecked(self):
        """Routing never reads the sweep for it, so a recheck would change
        nothing."""
        live = _CheckedLive()
        status = _swept([live])
        status._recheck_down()
        assert live.probes == 1

    def _recheck_recording(self, status):
        """Run a recheck; what it handed the serving registry."""
        with mock.patch(
            "fighthealthinsurance.ml.serving_registry.record_backends_async"
        ) as record:
            status._recheck_down()
        return record

    def test_a_recheck_records_the_card_of_only_the_backend_it_brings_back(self):
        """The sweep that marked it down left the serving registry a "nothing"
        for it, so unrecorded, every draft it wrote until the next sweep had
        no ServingIdentity."""
        recovering = _Flaky("recovering")
        status = _swept([_Flaky("healthy", healthy=True), recovering, _Flaky()])
        recovering.healthy = True
        record = self._recheck_recording(status)
        record.assert_called_once_with([recovering])

    def test_a_recheck_that_brings_nothing_back_records_nothing(self):
        status = _swept([_Flaky()])
        record = self._recheck_recording(status)
        record.assert_not_called()

    # The public snapshot follows routing: a backend a recheck puts back is
    # counted and no longer listed as failing, not an hour later.

    def test_after_a_recheck_the_snapshot_counts_the_backend_alive(self):
        backend = _FlakyOutside()
        status = _swept([backend])
        backend.healthy = True
        status._recheck_down()
        assert status.get_snapshot()["alive_models"] == 1

    def test_after_a_recheck_the_snapshot_no_longer_lists_it_failing(self):
        backend = _FlakyOutside()
        status = _swept([backend])
        backend.healthy = True
        status._recheck_down()
        assert status.get_snapshot()["details"] == []

    def test_a_recheck_drops_only_the_row_of_the_backend_it_brings_back(self):
        """Two providers can expose the same wire id."""
        recovering = _FlakyOutside("same-id")
        status = _swept([recovering, _FlakyOutside("same-id")])
        recovering.healthy = True
        status._recheck_down()
        assert status.get_snapshot()["details"] == [
            {"name": "same-id", "ok": False, "error": "not ok"}
        ]

    def test_a_recovered_chat_only_backend_is_not_counted_alive(self):
        """It cannot draft, so the sweep would not have counted it either."""
        chat = _FlakyOutside("chat-model")
        status = _swept([_Flaky("drafter", healthy=True)], chat_outside={"c": chat})
        chat.healthy = True
        status._recheck_down()
        assert status.get_snapshot()["alive_models"] == 1

    def test_a_recovered_backend_with_a_live_problem_is_not_counted_alive(self):
        """The live overlay reads it like any backend whose probe passed."""
        backend = _FlakyRefused()
        status = _swept([backend])
        backend.healthy = True
        status._recheck_down()
        assert status.get_snapshot()["alive_models"] == 0

    def test_a_backend_that_stays_down_waits_twice_as_long_next_time(self):
        status = _swept([_Flaky()])
        status._recheck_down()
        assert status._recheck_seconds == 2 * DOWN_RECHECK_SECONDS

    def test_the_wait_between_rechecks_stops_growing_at_the_hour(self):
        status = _swept([_Flaky()])
        for _ in range(10):
            status._recheck_down()
        assert status._recheck_seconds == REFRESH_INTERVAL_SECONDS

    def test_a_full_sweep_starts_the_rechecks_short_again(self):
        backend = _Flaky()
        status = _swept([backend])
        status._recheck_down()
        _swept([backend], status)
        assert status._recheck_seconds == DOWN_RECHECK_SECONDS

    def test_the_next_tick_is_a_recheck_while_a_backend_is_down(self):
        status = _swept([_Flaky()])
        assert status._next_tick_seconds() == DOWN_RECHECK_SECONDS

    def test_the_next_tick_is_the_hourly_sweep_when_nothing_is_down(self):
        status = _swept([_Flaky(healthy=True)])
        assert status._next_tick_seconds() > REFRESH_INTERVAL_SECONDS - 60

    def test_rechecks_do_not_move_the_hourly_sweep(self):
        """Near the hour, the next tick is the sweep, not a full recheck
        interval later."""
        status = _swept([_Flaky()])
        status._last_sweep_at -= REFRESH_INTERVAL_SECONDS - 30
        assert status._next_tick_seconds() <= 30

    def _tick(self, status):
        with mock.patch.object(status, "_refresh") as refresh, mock.patch.object(
            status, "_recheck_down"
        ) as recheck, mock.patch.object(status, "_schedule_refresh"):
            status._tick()
        return refresh.called, recheck.called

    def test_a_tick_before_the_hour_only_rechecks(self):
        status = _swept([_Flaky()])
        assert self._tick(status) == (False, True)

    def test_a_tick_at_the_hour_runs_the_full_sweep(self):
        status = _swept([_Flaky()])
        status._last_sweep_at -= REFRESH_INTERVAL_SECONDS
        assert self._tick(status) == (True, False)

    def test_a_failing_recheck_still_arms_the_next_tick(self):
        status = _swept([_Flaky()])
        with mock.patch.object(
            status, "_recheck_down", side_effect=RuntimeError("boom")
        ), mock.patch.object(status, "_schedule_refresh") as rearm:
            status._tick()
        rearm.assert_called_once()

    @override_settings(ML_HEALTH_BACKGROUND_SWEEP=True)
    def test_the_chain_ticks_into_the_recheck(self):
        status = _swept([_Flaky()])
        with mock.patch(
            "fighthealthinsurance.ml.health_status.threading.Timer"
        ) as timer:
            status._schedule_refresh()
        timer.assert_called_once_with(DOWN_RECHECK_SECONDS, status._tick)


class TestProbeEndsConnectCooldown(TestCase):
    """A /models probe that passes heard the host, so an outside endpoint's
    connect-failure cooldown (escalating up to an hour) ends with it. Only
    reachability: a refusal or a 5xx cooldown is left to run."""

    def test_a_passing_sweep_probe_ends_the_cooldown(self):
        backend = _outside_backend()
        _connect_failures(backend)
        with mock.patch.object(backend, "model_is_ok", return_value=True):
            _swept([backend])
        assert backend.is_available() is True

    def test_a_passing_recheck_ends_the_cooldown(self):
        backend = _outside_backend()
        with mock.patch.object(backend, "model_is_ok", return_value=False):
            status = _swept([backend])
        _connect_failures(backend)
        with mock.patch.object(backend, "model_is_ok", return_value=True):
            status._recheck_down()
        assert backend.is_available() is True

    def test_the_next_outage_starts_from_the_short_cooldown(self):
        backend = _outside_backend()
        _connect_failures(backend)
        with mock.patch.object(backend, "model_is_ok", return_value=True):
            _swept([backend])
        assert backend._transport_recools == {}

    def test_a_failing_probe_leaves_the_cooldown(self):
        backend = _outside_backend()
        _connect_failures(backend)
        with mock.patch.object(backend, "model_is_ok", return_value=False):
            _swept([backend])
        assert backend.is_available() is False

    def test_a_cooldown_from_server_errors_is_left_to_run(self):
        """A /models answer says nothing of the completions route that
        answered 5xx."""
        backend = _outside_backend()
        _connect_failures(backend, detail="HTTP 503", connect_failed=False)
        with mock.patch.object(backend, "model_is_ok", return_value=True):
            _swept([backend])
        assert backend.is_available() is False

    def test_a_refusal_is_left_in_place(self):
        """/models can list a model the key is refused for."""
        backend = _outside_backend()
        backend._note_http_refusal(
            backend.api_base, backend.model, 401, "", probe=False
        )
        with mock.patch.object(backend, "model_is_ok", return_value=True):
            _swept([backend])
        assert backend.unavailable_reason() == "refused (HTTP 401)"


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
