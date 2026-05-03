"""UCR refresh polling actor.

See UCR-OON-Reimbursement-Plan.md §10.4. Two independent loops run
concurrently inside one Ray actor so a slow upstream source does not stall
per-denial refreshes.

The Ray-decorated `UCRRefreshActor` is a thin wrapper around
`UCRRefreshController`. The controller holds the loop logic and is plain
(non-decorated) Python so it can be unit-tested directly without spinning a
Ray runtime — see tests/sync-actor/test_ucr_refresh_actor.py.
"""

import asyncio
import datetime
import os
import random
import time

import ray
from asgiref.sync import sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "UCRRefreshActor"


class UCRRefreshController:
    """Non-Ray loop logic, shared between the @ray.remote actor and tests."""

    def __init__(self, logger):
        self._logger = logger
        self.running = False
        self._actor_error_count = 0
        self._loop_count = 0
        self._last_seen_effective: dict[str, datetime.date] = {}

    # ------------------------------------------------------------- entrypoints

    async def refresh_denial(self, denial_id: int) -> bool:
        """Out-of-cycle trigger for a single denial — called from REST (§7).

        Returns True iff the helper produced a fresh comparison.
        """
        from fighthealthinsurance.models import Denial
        from fighthealthinsurance.ucr_helper import UCREnrichmentHelper

        try:
            denial = await sync_to_async(Denial.objects.get)(pk=denial_id)
        except Denial.DoesNotExist:
            self._logger.warning("UCR refresh_denial: denial {} not found", denial_id)
            return False

        result = await sync_to_async(UCREnrichmentHelper.maybe_enrich)(
            denial, force=True
        )
        return result is not None

    async def source_refresh_loop(self) -> None:
        from django.conf import settings

        while self.running:
            await asyncio.sleep(1)
            try:
                await self.refresh_sources_once()
                self._loop_count += 1
                self._actor_error_count = 0
            except Exception:
                self._actor_error_count += 1
                self._logger.opt(exception=True).error(
                    "UCR source refresh failed (#{})", self._actor_error_count
                )
            base = settings.UCR_SOURCE_REFRESH_INTERVAL_HOURS * 3600
            await asyncio.sleep(random.uniform(base * 0.85, base * 1.15))

    async def denial_refresh_loop(self) -> None:
        from django.conf import settings

        while self.running:
            await asyncio.sleep(1)
            try:
                processed, failed = await self.refresh_denials_once()
                self._loop_count += 1
                if processed or failed:
                    self._logger.info(
                        "UCR denial refresh: {} processed, {} failed",
                        processed,
                        failed,
                    )
                self._actor_error_count = 0
            except Exception:
                self._actor_error_count += 1
                self._logger.opt(exception=True).error(
                    "UCR denial refresh loop crashed (#{})",
                    self._actor_error_count,
                )
            base = settings.UCR_DENIAL_REFRESH_INTERVAL_MINUTES * 60
            await asyncio.sleep(random.uniform(base * 0.75, base * 1.25))

    # --------------------------------------------------------- source helpers

    async def refresh_sources_once(self) -> list[str]:
        advanced: list[str] = []
        for source, predicate in self._source_predicates():
            try:
                if await predicate():
                    advanced.append(source)
            except Exception:
                self._logger.opt(exception=True).error(
                    "UCR source predicate failed for {}", source
                )

        for source in advanced:
            try:
                await self.mark_denials_using_source_stale(source)
            except Exception:
                self._logger.opt(exception=True).error(
                    "Failed to mark denials stale for source {}", source
                )
        return advanced

    def _source_predicates(self):
        return [
            ("medicare_pfs", self._refresh_medicare_pfs_if_due),
            ("fair_health", self._refresh_fair_health_if_due),
            ("fhi_aggregate", self._refresh_fhi_aggregate_if_due),
        ]

    async def _refresh_medicare_pfs_if_due(self) -> bool:
        from fighthealthinsurance.models import UCRRate
        from fighthealthinsurance.ucr_constants import UCRSource

        latest = await sync_to_async(
            lambda: UCRRate.objects.filter(source=UCRSource.MEDICARE_PFS)
            .order_by("-effective_date")
            .values_list("effective_date", flat=True)
            .first()
        )()
        if latest is None:
            return False
        return self._note_source_advance("medicare_pfs", latest)

    async def _refresh_fair_health_if_due(self) -> bool:
        return False  # phase 2 stub

    async def _refresh_fhi_aggregate_if_due(self) -> bool:
        return False  # phase 3 stub

    def _note_source_advance(
        self, source: str, latest_effective_date: datetime.date
    ) -> bool:
        prior = self._last_seen_effective.get(source)
        self._last_seen_effective[source] = latest_effective_date
        return prior is not None and latest_effective_date > prior

    async def mark_denials_using_source_stale(self, source: str) -> int:
        # Filter via Denial.latest_ucr_lookup so we only mark denials whose
        # *currently surfaced* snapshot uses this source. Querying UCRLookup
        # directly would match any historical audit row and re-stale denials
        # that have already moved to a different source.
        from fighthealthinsurance.models import Denial

        return await sync_to_async(
            lambda: Denial.objects.filter(
                appeal_result__isnull=True,
                latest_ucr_lookup__rates_snapshot__contains=[{"source": source}],
            ).update(ucr_refreshed_at=None)
        )()

    # -------------------------------------------------------- denial helpers

    async def refresh_denials_once(self) -> tuple[int, int]:
        from django.conf import settings
        from django.db.models import Q
        from django.utils import timezone

        from fighthealthinsurance.models import Denial
        from fighthealthinsurance.ucr_helper import UCREnrichmentHelper

        ttl = settings.UCR_DENIAL_STALE_TTL_DAYS
        batch_size = settings.UCR_DENIAL_REFRESH_BATCH_SIZE

        cutoff = timezone.now() - datetime.timedelta(days=ttl)
        stale = await sync_to_async(
            lambda: list(
                Denial.objects.filter(appeal_result__isnull=True)
                .filter(
                    Q(ucr_refreshed_at__isnull=True) | Q(ucr_refreshed_at__lt=cutoff)
                )
                .order_by("pk")[:batch_size]
            )
        )()

        if not stale:
            return 0, 0

        rate_cache = await sync_to_async(UCREnrichmentHelper.bulk_load_rates)(stale)

        # return_exceptions=True so one bad denial doesn't cancel the batch
        # and we get per-denial visibility into failures (§10.4).
        # No force=True: we want the helper's hash short-circuit so the actor
        # only writes when something actually changed. ucr_refreshed_at is
        # bumped by the helper either way, so the stale-batch query advances.
        results = await asyncio.gather(
            *(
                sync_to_async(UCREnrichmentHelper.maybe_enrich)(d, rates=rate_cache)
                for d in stale
            ),
            return_exceptions=True,
        )
        failed = 0
        for denial, result in zip(stale, results):
            if isinstance(result, Exception):
                failed += 1
                self._logger.opt(exception=result).error(
                    "UCR enrich failed for denial_id={}", denial.pk
                )
        return len(stale) - failed, failed


@ray.remote(max_restarts=-1, max_task_retries=-1)
class UCRRefreshActor:
    def __init__(self):
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._controller = UCRRefreshController(logger)
        logger.info("UCRRefreshActor initialized")

    async def hello(self) -> str:
        return "Hi"

    async def count(self) -> int:
        return self._controller._loop_count

    async def actor_error_count(self) -> int:
        return self._controller._actor_error_count

    async def health_check(self) -> bool:
        return self._controller.running

    async def run(self) -> None:
        self._controller._logger.info("Starting UCRRefreshActor run")
        self._controller.running = True
        try:
            await asyncio.gather(
                self._controller.source_refresh_loop(),
                self._controller.denial_refresh_loop(),
            )
        finally:
            self._controller.running = False
            self._controller._logger.warning("UCRRefreshActor stopped running")

    async def refresh_denial(self, denial_id: int) -> bool:
        return await self._controller.refresh_denial(denial_id)
