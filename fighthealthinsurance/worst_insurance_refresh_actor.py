"""Ray actor that periodically refreshes the worst-insurance rankings.

Pulls the pipeline repo's published ``rankings.json`` (the
``WORST_INSURANCE_RANKINGS_URL`` setting, typically the raw GitHub URL of
``data/latest.json`` on main) on the ``WORST_INSURANCE_REFRESH_INTERVAL_HOURS``
cadence and idempotently upserts via the shared loader. The URL unset means
the feature is dark: nothing fetches, pages 404.

The run-loop / health-check / backoff scaffolding lives in
``BaseRefreshActor``.
"""

from typing import Tuple

import ray
from asgiref.sync import sync_to_async

from fighthealthinsurance.base_refresh_actor import BaseRefreshActor

name = "WorstInsuranceRefreshActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class WorstInsuranceRefreshActor(BaseRefreshActor):
    actor_log_name = "WorstInsuranceRefreshActor"
    settings_interval_key = "WORST_INSURANCE_REFRESH_INTERVAL_HOURS"
    default_interval_hours = 24

    @staticmethod
    def _configured_url() -> str:
        from django.conf import settings

        return getattr(settings, "WORST_INSURANCE_RANKINGS_URL", "")

    @staticmethod
    def _refresh(url: str) -> Tuple[int, int, int, int]:
        from fighthealthinsurance.worst_insurance_ingest import (
            fetch_json,
            load_report_dict,
        )

        data = fetch_json(url)
        return load_report_dict(data, source_url=url)

    async def _refresh_due(self, interval_hours: int) -> bool:
        url = self._configured_url()
        if not url:
            self._logger.debug(
                "No WORST_INSURANCE_RANKINGS_URL configured; skipping refresh"
            )
            return False
        try:
            created, updated, deleted, match_failures = await sync_to_async(
                self._refresh, thread_sensitive=False
            )(url)
            log = self._logger.warning if match_failures else self._logger.info
            log(
                f"Worst-insurance refresh: {created} created, {updated} updated, "
                f"{deleted} deleted, {match_failures} unmatched issuers"
            )
            return True
        except Exception as e:
            self._logger.opt(exception=True).warning(
                f"Worst-insurance refresh failed: {e}"
            )
            return False
