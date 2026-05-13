"""Ray actor that periodically refreshes the IMR / external-appeal corpus.

Pulls the CA DMHC and (optionally) NY DFS CSV exports on the
``IMR_REFRESH_INTERVAL_HOURS`` cadence and upserts rows via the shared
``load_csv_text`` loader. Sources whose URL setting is unset are skipped.

The run-loop / health-check / backoff scaffolding lives in
``BaseRefreshActor`` so this module only owns the IMR-specific work.
"""

from typing import Tuple

import ray
from asgiref.sync import sync_to_async

from fighthealthinsurance.base_refresh_actor import BaseRefreshActor

name = "IMRRefreshActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class IMRRefreshActor(BaseRefreshActor):
    actor_log_name = "IMRRefreshActor"
    settings_interval_key = "IMR_REFRESH_INTERVAL_HOURS"
    default_interval_hours = 168

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

    async def _refresh_due(self, interval_hours: int) -> bool:
        sources = self._configured_sources()
        if not sources:
            self._logger.debug("No IMR_*_CSV_URL settings configured; skipping refresh")
            return False

        any_success = False
        for source, url in sources:
            try:
                created, updated, skipped, failed = await sync_to_async(
                    self._refresh_source, thread_sensitive=False
                )(source, url)
                any_success = True
                log = self._logger.warning if failed else self._logger.info
                log(
                    f"IMR refresh ({source}): {created} created, "
                    f"{updated} updated, {skipped} skipped, {failed} failed"
                )
            except Exception as e:
                self._logger.opt(exception=True).warning(
                    f"IMR refresh failed for source {source}: {e}"
                )

        return any_success

    @staticmethod
    def _refresh_source(source: str, url: str) -> Tuple[int, int, int, int]:
        from fighthealthinsurance.imr_ingest import fetch_csv, load_csv_text

        csv_text = fetch_csv(url)
        return load_csv_text(csv_text, source=source, source_url=url)
