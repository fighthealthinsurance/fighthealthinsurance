"""Ray actor that periodically refreshes the IMR / external-appeal corpus.

Pulls the CA DMHC and (optionally) NY DFS CSV exports on the
``IMR_REFRESH_INTERVAL_HOURS`` cadence and upserts rows via the shared
``load_csv_text`` loader. Sources whose URL setting is unset are skipped.
"""

import asyncio
import datetime
import os
import time
from typing import Optional, Tuple

import ray
from asgiref.sync import sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "IMRRefreshActor"


# Backoff between attempts after an unexpected error.
ERROR_BACKOFF_SECONDS = 600


@ray.remote(max_restarts=-1, max_task_retries=-1)
class IMRRefreshActor:
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
        self._logger.info("IMRRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting IMRRefreshActor run")
        self.running = True

        while self.running:
            try:
                interval_hours = self._interval_hours()
                await self._refresh_due_sources(interval_hours)
                # Re-check hourly so config changes are picked up without restart.
                await asyncio.sleep(3600)
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in IMRRefreshActor refresh cycle"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)

        self._logger.warning("IMRRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False

    # --- Implementation -----------------------------------------------------

    @staticmethod
    def _interval_hours() -> int:
        from django.conf import settings

        return getattr(settings, "IMR_REFRESH_INTERVAL_HOURS", 168)

    @staticmethod
    def _configured_sources() -> list[Tuple[str, str]]:
        from django.conf import settings

        from fighthealthinsurance.models import IMRDecision

        candidates = [
            (IMRDecision.SOURCE_CA_DMHC, getattr(settings, "IMR_DMHC_CSV_URL", "")),
            (IMRDecision.SOURCE_NY_DFS, getattr(settings, "IMR_DFS_CSV_URL", "")),
        ]
        return [(src, url) for src, url in candidates if url]

    async def _refresh_due_sources(self, interval_hours: int) -> None:
        sources = self._configured_sources()
        if not sources:
            self._logger.debug("No IMR_*_CSV_URL settings configured; skipping refresh")
            return

        if self.last_refresh is not None:
            elapsed = datetime.datetime.utcnow() - self.last_refresh
            if elapsed < datetime.timedelta(hours=interval_hours):
                return

        any_success = False
        for source, url in sources:
            try:
                created, updated, skipped = await sync_to_async(
                    self._refresh_source, thread_sensitive=False
                )(source, url)
                any_success = True
                self._logger.info(
                    f"IMR refresh ({source}): {created} created, "
                    f"{updated} updated, {skipped} skipped"
                )
            except Exception as e:
                self._logger.opt(exception=True).warning(
                    f"IMR refresh failed for source {source}: {e}"
                )

        # Only mark the cycle as complete if at least one source actually
        # refreshed; otherwise the next loop iteration will retry instead of
        # waiting another full IMR_REFRESH_INTERVAL_HOURS window.
        if any_success:
            self.last_refresh = datetime.datetime.utcnow()

    @staticmethod
    def _refresh_source(source: str, url: str) -> Tuple[int, int, int]:
        from fighthealthinsurance.imr_ingest import fetch_csv, load_csv_text

        csv_text = fetch_csv(url)
        return load_csv_text(csv_text, source=source, source_url=url)
