"""Ray actor that periodically re-fetches payer prior-authorization requirement lists.

Iterates every ``InsuranceCompany`` row with
``pa_requirement_list_url_is_parseable=True`` on the
``PA_REQUIREMENT_REFRESH_INTERVAL_HOURS`` cadence (default: 168 h / weekly)
and upserts ``PayerPriorAuthRequirement`` rows via
:class:`~fighthealthinsurance.pa_requirement_fetcher.PARequirementFetcher`.

The actor pattern mirrors :class:`IMRRefreshActor`: it loops forever, sleeps
between cycles, and recovers automatically after network failures.
"""

import asyncio
import datetime
import os
import time
from typing import Optional

import ray

from fighthealthinsurance.utils import get_env_variable

name = "PARequirementRefreshActor"

ERROR_BACKOFF_SECONDS = 600


@ray.remote(max_restarts=-1, max_task_retries=-1)
class PARequirementRefreshActor:
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
        self._logger.info("PARequirementRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting PARequirementRefreshActor run")
        self.running = True

        while self.running:
            try:
                interval_hours = self._interval_hours()
                await self._refresh_if_due(interval_hours)
                # Re-check hourly so config changes are picked up without restart.
                await asyncio.sleep(3600)
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in PARequirementRefreshActor refresh cycle"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)

        self._logger.warning("PARequirementRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False

    # --- Implementation ---------------------------------------------------

    @staticmethod
    def _interval_hours() -> int:
        from django.conf import settings

        return getattr(settings, "PA_REQUIREMENT_REFRESH_INTERVAL_HOURS", 168)

    async def _refresh_if_due(self, interval_hours: int) -> None:
        if self.last_refresh is not None:
            elapsed = datetime.datetime.utcnow() - self.last_refresh
            if elapsed < datetime.timedelta(hours=interval_hours):
                return

        self._logger.info("PA requirement refresh cycle starting")
        try:
            from fighthealthinsurance.pa_requirement_fetcher import PARequirementFetcher

            async with PARequirementFetcher() as fetcher:
                stats = await fetcher.ingest_all()

            log = self._logger.warning if stats["failed"] > 0 else self._logger.info
            log(
                f"PA requirement refresh: {stats['fetched']} payers fetched, "
                f"{stats['failed']} failed, {stats['entries']} requirements written"
            )
            # Only advance the timestamp if at least one payer was successfully
            # fetched — a total failure should retry on the next hourly check.
            if stats["fetched"] > 0 or stats["failed"] == 0:
                self.last_refresh = datetime.datetime.utcnow()
        except Exception:
            self._logger.opt(exception=True).error(
                "PA requirement refresh cycle failed"
            )
