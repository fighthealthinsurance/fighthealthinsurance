"""Tests for UCRRefreshActor under the sync-actor tox env.

The Ray-based smoke test mirrors the fax_polling_actor pattern: spin the
actor up, kick run(), and confirm it transitions into a healthy looping
state. The remaining tests exercise UCRRefreshController directly — it's
the non-Ray companion class that holds the loop logic, which means we can
unit-test it without spinning Ray.
"""

import asyncio
import datetime
import os
import time

import pytest
import ray
from django.test import TestCase
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import UCRAreaKind, UCRSource
from fighthealthinsurance.ucr_refresh_actor import (
    UCRRefreshActor,
    UCRRefreshController,
)


@pytest.mark.django_db
class TestUCRRefreshActorRayLifecycle(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        if not ray.is_initialized():
            environ = dict(os.environ)
            environ["DJANGO_CONFIGURATION"] = "Test"
            ray.init(
                namespace="fhi-test",
                ignore_reinit_error=True,
                runtime_env={"env_vars": environ},
                num_cpus=1,
            )

    def tearDown(self):
        if ray.is_initialized():
            ray.shutdown()

    def test_run_method_starts_and_loops(self):
        actor = UCRRefreshActor.remote()

        self.assertEqual("Hi", ray.get(actor.hello.remote()))
        actor.run.remote()

        time.sleep(0.5)
        # Generous deadline because the actor bootstraps Django inside the
        # Ray worker (load settings, urlconf, app registry) before its first
        # sleep+iteration; cold-start in CI can take 20+s.
        max_wait = 60
        start = time.time()
        running = False
        loop_executed = False
        while time.time() - start < max_wait:
            running = ray.get(actor.health_check.remote())
            if running and ray.get(actor.count.remote()) > 0:
                # Only count successful loop iterations; an immediate crash
                # bumps actor_error_count without proving the body ran.
                loop_executed = True
                break
            time.sleep(0.5)

        self.assertTrue(running, "run() should mark the actor as running")
        self.assertTrue(loop_executed, "run() loop body should have executed")
        self.assertEqual(
            ray.get(actor.actor_error_count.remote()),
            0,
            "actor should not have errored during the smoke run",
        )


class TestUCRRefreshControllerDenialBatch(TestCase):
    """Exercise the denial-refresh loop body via UCRRefreshController.

    Direct call avoids the Ray bootstrap; we just want to test the math.
    """

    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        for percentile, amount in [(50, 14763), (80, 19684), (90, 24605)]:
            UCRRate.objects.create(
                procedure_code="99213",
                geographic_area=self.area,
                percentile=percentile,
                amount_cents=amount,
                source=UCRSource.MEDICARE_PFS,
                effective_date=datetime.date(2026, 1, 1),
            )
        self.controller = UCRRefreshController(logger)

    def _make_denial(self) -> Denial:
        denial = Denial.objects.create(
            hashed_email="hash:test",
            procedure_code="99213",
            service_zip="94110",
            your_state="CA",
        )
        denial.set_billed_cents(25000)
        denial.set_allowed_cents(8000)
        denial.set_paid_cents(6400)
        denial.save()
        return denial

    def test_processes_stale_denials(self):
        denial = self._make_denial()
        processed, failed = asyncio.run(self.controller.refresh_denials_once())
        self.assertEqual(processed, 1)
        self.assertEqual(failed, 0)
        denial.refresh_from_db()
        self.assertIsNotNone(denial.ucr_refreshed_at)
        self.assertIsNotNone(denial.latest_ucr_lookup)

    def test_skips_finalized_denials(self):
        denial = self._make_denial()
        denial.appeal_result = "submitted"
        denial.save(update_fields=["appeal_result"])
        processed, failed = asyncio.run(self.controller.refresh_denials_once())
        self.assertEqual(processed, 0)

    def test_per_denial_failures_are_logged_not_raised(self):
        denial = self._make_denial()

        from fighthealthinsurance import ucr_helper

        original = ucr_helper.UCREnrichmentHelper.maybe_enrich
        boom_target = denial.pk
        call_state = {"raised": False, "ok": False}

        def maybe_enrich_with_one_failure(d, **kwargs):
            if d.pk == boom_target and not call_state["raised"]:
                call_state["raised"] = True
                raise RuntimeError("synthetic failure")
            call_state["ok"] = True
            return original(d, **kwargs)

        good = self._make_denial()
        ucr_helper.UCREnrichmentHelper.maybe_enrich = (  # type: ignore[assignment]
            maybe_enrich_with_one_failure
        )
        try:
            processed, failed = asyncio.run(self.controller.refresh_denials_once())
        finally:
            ucr_helper.UCREnrichmentHelper.maybe_enrich = original  # type: ignore[assignment]

        self.assertEqual(processed, 1)
        self.assertEqual(failed, 1)
        self.assertTrue(call_state["raised"])
        self.assertTrue(call_state["ok"])
        good.refresh_from_db()
        self.assertIsNotNone(good.ucr_refreshed_at)
