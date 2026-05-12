"""Ray actor that re-fetches payer prior-authorization requirement lists daily at 1 AM Pacific.

Iterates every ``InsuranceCompany`` row with
``pa_requirement_list_url_is_parseable=True`` once per day and upserts
``PayerPriorAuthRequirement`` rows via
:class:`~fighthealthinsurance.pa_requirement_fetcher.PARequirementFetcher`.
"""

import asyncio
import os
import time

import ray

from fighthealthinsurance.utils import get_env_variable, seconds_until_next_1am_pacific

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
        self._stop_event: asyncio.Event = asyncio.Event()
        self._logger.info("PARequirementRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting PARequirementRefreshActor run")
        self.running = True
        self._stop_event.clear()

        while self.running:
            try:
                await self._refresh()
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in PARequirementRefreshActor refresh cycle"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=ERROR_BACKOFF_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            secs = seconds_until_next_1am_pacific()
            self._logger.info(
                f"PARequirementRefreshActor sleeping {secs:.0f}s until next 1 AM Pacific"
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=secs)
            except asyncio.TimeoutError:
                pass

        self._logger.warning("PARequirementRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()

    # --- Implementation ---------------------------------------------------

    async def _refresh(self) -> None:
        self._logger.info("PA requirement refresh cycle starting")
        from fighthealthinsurance.pa_requirement_fetcher import PARequirementFetcher

        async with PARequirementFetcher() as fetcher:
            stats = await fetcher.ingest_all()

        log = self._logger.warning if stats["failed"] > 0 else self._logger.info
        log(
            f"PA requirement refresh: {stats['fetched']} payers fetched, "
            f"{stats['failed']} failed, {stats['entries']} requirements written"
        )
