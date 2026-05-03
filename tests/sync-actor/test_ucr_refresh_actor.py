"""Tests for UCRRefreshActor under the sync-actor tox env.

The Ray-based smoke test mirrors the fax_polling_actor pattern: spin the actor
up, kick run(), and confirm it transitions into a healthy looping state. The
remaining tests exercise the per-denial helpers without Ray (they're plain
async coroutines on the actor class).
"""

import asyncio
import datetime
import os
import time

import pytest
import ray
from django.test import TestCase

from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import UCRAreaKind, UCRSource
from fighthealthinsurance.ucr_refresh_actor import UCRRefreshActor


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
        max_wait = 30
        start = time.time()
        running = False
        loop_executed = False
        while time.time() - start < max_wait:
            running = ray.get(actor.health_check.remote())
            if running:
                if (
                    ray.get(actor.actor_error_count.remote()) > 0
                    or ray.get(actor.count.remote()) > 0
                ):
                    loop_executed = True
                    break
            time.sleep(0.5)

        self.assertTrue(running, "run() should mark the actor as running")
        self.assertTrue(loop_executed, "run() loop body should have executed")


class _ActorUnderTest(UCRRefreshActor):
    """Bypass the @ray.remote decorator so we can call coroutines directly.

    Avoids spinning Ray for tests that just exercise the loop-body math.
    """

    def __init__(self):  # noqa: D401  (deliberately skip parent init)
        # We don't want to bootstrap a fresh Django app inside an existing
        # TestCase, so skip the heavy __init__ entirely.
        from loguru import logger

        self._logger = logger
        self.running = True
        self._actor_error_count = 0
        self._loop_count = 0


class TestUCRRefreshActorDenialBatch(TestCase):
    """Exercise the denial-refresh loop body without Ray."""

    def setUp(self):
        self.area = UCRGeographicArea.objects.create(
            kind=UCRAreaKind.ZIP3, code="941"
        )
        for percentile, amount in [(50, 14763), (80, 19684), (90, 24605)]:
            UCRRate.objects.create(
                procedure_code="99213",
                geographic_area=self.area,
                percentile=percentile,
                amount_cents=amount,
                source=UCRSource.MEDICARE_PFS,
                effective_date=datetime.date(2026, 1, 1),
            )
        self.actor = _ActorUnderTest()

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
        denial = self._make_denial()  # ucr_refreshed_at is NULL -> stale
        processed, failed = asyncio.run(self.actor._refresh_denials_once())
        self.assertEqual(processed, 1)
        self.assertEqual(failed, 0)
        denial.refresh_from_db()
        self.assertIsNotNone(denial.ucr_refreshed_at)
        self.assertIsNotNone(denial.latest_ucr_lookup)

    def test_skips_finalized_denials(self):
        denial = self._make_denial()
        denial.appeal_result = "submitted"
        denial.save(update_fields=["appeal_result"])
        processed, failed = asyncio.run(self.actor._refresh_denials_once())
        self.assertEqual(processed, 0)

    def test_per_denial_failures_are_logged_not_raised(self):
        # A denial with no resolvable area returns None from maybe_enrich
        # (a skip, not a raise). Use a real failure path: corrupt
        # `procedure_code` to something that forces a DB error in the helper.
        denial = self._make_denial()

        # Patch maybe_enrich to raise for one denial, succeed for another.
        from fighthealthinsurance import ucr_helper

        original = ucr_helper.UCREnrichmentHelper.maybe_enrich
        boom_target = denial.id
        call_state = {"raised": False, "ok": False}

        def maybe_enrich_with_one_failure(d, **kwargs):
            if d.id == boom_target and not call_state["raised"]:
                call_state["raised"] = True
                raise RuntimeError("synthetic failure")
            call_state["ok"] = True
            return original(d, **kwargs)

        good = self._make_denial()  # second denial that should still process
        ucr_helper.UCREnrichmentHelper.maybe_enrich = (  # type: ignore[assignment]
            maybe_enrich_with_one_failure
        )
        try:
            processed, failed = asyncio.run(self.actor._refresh_denials_once())
        finally:
            ucr_helper.UCREnrichmentHelper.maybe_enrich = original  # type: ignore[assignment]

        self.assertEqual(processed, 1)
        self.assertEqual(failed, 1)
        self.assertTrue(call_state["raised"])
        self.assertTrue(call_state["ok"])
        # The good denial got refreshed despite the bad one raising.
        good.refresh_from_db()
        self.assertIsNotNone(good.ucr_refreshed_at)
