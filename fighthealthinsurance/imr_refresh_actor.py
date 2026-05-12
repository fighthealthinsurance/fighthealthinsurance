"""Ray actor that refreshes the IMR / external-appeal corpus daily at 1 AM Pacific.

Pulls the CA DMHC and (optionally) NY DFS CSV exports once per day and
upserts rows via the shared ``load_csv_text`` loader. Sources whose URL
setting is unset are skipped.
"""

import asyncio
import os
import time
from typing import Tuple

import ray
from asgiref.sync import sync_to_async

from fighthealthinsurance.utils import get_env_variable, seconds_until_next_1am_pacific

name = "IMRRefreshActor"

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
        self._logger.info("IMRRefreshActor initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info("Starting IMRRefreshActor run")
        self.running = True

        while self.running:
            try:
                await self._refresh_sources()
            except Exception:
                self._logger.opt(exception=True).error(
                    "Error in IMRRefreshActor refresh cycle"
                )
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                continue

            secs = seconds_until_next_1am_pacific()
            self._logger.info(f"IMRRefreshActor sleeping {secs:.0f}s until next 1 AM Pacific")
            await asyncio.sleep(secs)

        self._logger.warning("IMRRefreshActor stopped running")

    def stop(self) -> None:
        self.running = False

    # --- Implementation -----------------------------------------------------

    @staticmethod
    def _configured_sources() -> list[Tuple[str, str]]:
        from django.conf import settings

        from fighthealthinsurance.models import IMRDecision

        candidates = [
            (IMRDecision.SOURCE_CA_DMHC, getattr(settings, "IMR_DMHC_CSV_URL", "")),
            (IMRDecision.SOURCE_NY_DFS, getattr(settings, "IMR_DFS_CSV_URL", "")),
        ]
        return [(src, url) for src, url in candidates if url]

    async def _refresh_sources(self) -> None:
        sources = self._configured_sources()
        if not sources:
            self._logger.debug("No IMR_*_CSV_URL settings configured; skipping refresh")
            return

        for source, url in sources:
            try:
                created, updated, skipped, failed = await sync_to_async(
                    self._refresh_source, thread_sensitive=False
                )(source, url)
                log = self._logger.warning if failed else self._logger.info
                log(
                    f"IMR refresh ({source}): {created} created, "
                    f"{updated} updated, {skipped} skipped, {failed} failed"
                )
            except Exception as e:
                self._logger.opt(exception=True).warning(
                    f"IMR refresh failed for source {source}: {e}"
                )

    @staticmethod
    def _refresh_source(source: str, url: str) -> Tuple[int, int, int, int]:
        from fighthealthinsurance.imr_ingest import fetch_csv, load_csv_text

        csv_text = fetch_csv(url)
        return load_csv_text(csv_text, source=source, source_url=url)
