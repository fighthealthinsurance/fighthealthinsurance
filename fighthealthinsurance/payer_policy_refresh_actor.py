"""Ray actor that re-fetches payer medical-policy indexes daily at 1 AM Pacific.

Iterates every ``InsuranceCompany`` row with
``medical_policy_url_is_static_index=True`` once per day and replaces its
``PayerPolicyEntry`` rows via
:class:`~fighthealthinsurance.payer_policy_fetcher.PayerPolicyFetcher`.
"""

import asyncio
import os
import time

import ray

from fighthealthinsurance.utils import get_env_variable, seconds_until_next_1am_pacific

name = "PayerPolicyRefreshActor"

ERROR_BACKOFF_SECONDS = 600


@ray.remote(max_restarts=-1, max_task_retries=-1)
class PayerPolicyRefreshActor:
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
        self._logger.info("PayerPolicyRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting PayerPolicyRefreshActor run")
        self.running = True

        while self.running:
            try:
                await self._refresh()
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in PayerPolicyRefreshActor refresh cycle"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                continue

            secs = seconds_until_next_1am_pacific()
            self._logger.info(
                f"PayerPolicyRefreshActor sleeping {secs:.0f}s until next 1 AM Pacific"
            )
            await asyncio.sleep(secs)

        self._logger.warning("PayerPolicyRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False

    # --- Implementation ---------------------------------------------------

    async def _refresh(self) -> None:
        self._logger.info("Payer policy refresh cycle starting")
        from fighthealthinsurance.payer_policy_fetcher import PayerPolicyFetcher

        async with PayerPolicyFetcher() as fetcher:
            stats = await fetcher.ingest_all()

        log = self._logger.warning if stats["failed"] > 0 else self._logger.info
        log(
            f"Payer policy refresh: {stats['fetched']} payers fetched, "
            f"{stats['failed']} failed, {stats['entries']} entries written"
        )
