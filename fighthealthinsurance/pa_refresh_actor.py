"""Ray actor that periodically refreshes carrier PA requirement lists.

Runs ``PaRequirementsFetcher.ingest_all()`` on a
``PA_REFRESH_INTERVAL_HOURS`` cadence (default 168 hours / weekly). Carrier
PA lists publish quarterly at most so weekly is plenty; tighten the env
var if a carrier's policies move faster.

Patterned on ``IMRRefreshActor`` so the launcher and health-check
machinery in ``polling_actor_setup.py`` / ``actor_health_status.py``
treat it identically.
"""

import asyncio
import datetime
import os
import time
from typing import Optional

import ray

from fighthealthinsurance.utils import get_env_variable

name = "PaRefreshActor"


# Backoff between attempts after an unexpected error, mirroring the IMR
# actor so retry pressure on misbehaving carrier endpoints is consistent.
ERROR_BACKOFF_SECONDS = 600


@ray.remote(max_restarts=-1, max_task_retries=-1)
class PaRefreshActor:
    def __init__(self) -> None:
        time.sleep(1)
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )
        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._logger = logger
        self.running = False
        self.last_refresh: Optional[datetime.datetime] = None
        self._logger.info("PaRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting PaRefreshActor run")
        self.running = True

        while self.running:
            try:
                interval_hours = self._interval_hours()
                await self._refresh_due(interval_hours)
                # Re-check hourly so config / parser-registry changes get
                # picked up without a restart.
                await asyncio.sleep(3600)
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in PaRefreshActor refresh cycle"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)

        self._logger.warning("PaRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False

    @staticmethod
    def _interval_hours() -> int:
        from django.conf import settings

        return getattr(settings, "PA_REFRESH_INTERVAL_HOURS", 168)

    async def _refresh_due(self, interval_hours: int) -> None:
        from fighthealthinsurance.pa_requirements_fetcher import (
            PA_PARSERS,
            PaRequirementsFetcher,
        )

        if not PA_PARSERS:
            self._logger.debug(
                "No PA parsers registered; PaRefreshActor cycle is a no-op"
            )
            return

        if self.last_refresh is not None:
            elapsed = datetime.datetime.utcnow() - self.last_refresh
            if elapsed < datetime.timedelta(hours=interval_hours):
                return

        async with PaRequirementsFetcher() as fetcher:
            stats = await fetcher.ingest_all()

        log = self._logger.warning if stats["failed"] else self._logger.info
        log(
            "PA refresh: "
            f"{stats['fetched']} carrier(s) fetched, "
            f"{stats['failed']} failed, "
            f"{stats['entries']} rows upserted, "
            f"{stats['retired']} retired"
        )

        # Only mark the cycle complete if at least one carrier successfully
        # refreshed; otherwise the next loop iteration retries instead of
        # waiting another full PA_REFRESH_INTERVAL_HOURS window.
        if stats["fetched"] > 0:
            self.last_refresh = datetime.datetime.utcnow()
