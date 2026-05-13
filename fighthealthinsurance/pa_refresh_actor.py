"""Ray actor that periodically refreshes carrier PA requirement lists.

Runs ``PaRequirementsFetcher.ingest_all()`` on a
``PA_REFRESH_INTERVAL_HOURS`` cadence (default 168 hours / weekly).
Carrier PA lists publish quarterly at most so weekly is plenty; tighten
the env var if a carrier's policies move faster.

The bulk of the run-loop / health-check / backoff scaffolding lives in
``BaseRefreshActor`` so each new refresh actor stays minimal.
"""

import ray

from fighthealthinsurance.base_refresh_actor import BaseRefreshActor

name = "PaRefreshActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class PaRefreshActor(BaseRefreshActor):
    actor_log_name = "PaRefreshActor"
    settings_interval_key = "PA_REFRESH_INTERVAL_HOURS"
    # Carrier PA lists publish quarterly at most; weekly catches the
    # most-frequent refresh cycles with plenty of headroom.
    default_interval_hours = 168

    async def _refresh_due(self, interval_hours: int) -> bool:
        from fighthealthinsurance.pa_requirements_fetcher import (
            PaRequirementsFetcher,
        )

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
        return stats["fetched"] > 0
