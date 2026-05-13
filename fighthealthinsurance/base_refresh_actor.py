"""Shared run-loop scaffolding for periodic Ray refresh actors.

A refresh actor in this codebase has the same shape every time:

  1. Django + WSGI bootstrap in ``__init__``.
  2. ``run()`` loops forever, calling ``_refresh_due()`` on a configurable
     cadence and sleeping ~1 hour between checks so config changes are
     picked up without a restart.
  3. Errors during the refresh trigger an ``ERROR_BACKOFF_SECONDS`` sleep
     before the next attempt.
  4. ``health_check()`` reports whether the run loop is active.

Concrete subclasses supply ``_settings_interval_key`` (the env/settings
attribute holding the cadence in hours) and override ``_refresh_due()``
with the actual work. The ``@ray.remote`` decoration must be applied to
the subclass, not this base class — Ray inheritance works when the
parent is a plain Python class.

The :func:`bootstrap_django_for_actor` helper is exported separately so
non-refresh actors (polling actors, prefetch actors) can share the same
Django bootstrap without inheriting the refresh-loop scaffolding.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from typing import Optional

from fighthealthinsurance.utils import get_env_variable

# How long to sleep after an unhandled exception in the run loop. Matches
# the value used in every existing refresh actor so retry pressure on
# misbehaving upstream sources stays consistent across actors.
ERROR_BACKOFF_SECONDS = 600


def _now_utc() -> datetime.datetime:
    """Timezone-aware "now" in UTC.

    ``datetime.datetime.utcnow()`` is deprecated in Python 3.12+; refresh
    actors only care about a monotonically-advancing comparison value so
    a UTC-aware ``now()`` is the drop-in replacement.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def bootstrap_django_for_actor(settle_seconds: float = 1.0) -> None:
    """Initialize Django + WSGI inside a Ray actor process.

    Ray spawns each actor in its own process; Django settings have not been
    loaded yet there. The short pre-sleep mirrors what every existing actor
    does — it gives Ray a moment to finish setting up the actor handle
    before we start importing Django. Pass ``settle_seconds=0`` from
    contexts that don't need it (e.g. unit tests).
    """
    if settle_seconds:
        time.sleep(settle_seconds)
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
    )
    from configurations.wsgi import get_wsgi_application

    get_wsgi_application()


class BaseRefreshActor:
    """Plain-Python base for ``@ray.remote``-decorated refresh actors.

    Subclasses set:

    * ``actor_log_name``: human label used in log lines.
    * ``settings_interval_key``: name of the settings attribute holding
      the cadence in hours (e.g. ``"PA_REFRESH_INTERVAL_HOURS"``).
    * ``default_interval_hours``: fallback when the settings key is unset.

    Subclasses override:

    * ``_refresh_due()``: do the actual refresh work; return truthy if at
      least one source successfully refreshed (used to advance
      ``last_refresh``).
    """

    actor_log_name: str = "BaseRefreshActor"
    settings_interval_key: str = ""
    default_interval_hours: int = 168
    error_backoff_seconds: int = ERROR_BACKOFF_SECONDS

    def __init__(self) -> None:
        bootstrap_django_for_actor()
        from loguru import logger

        self._logger = logger
        self.running = False
        self.last_refresh: Optional[datetime.datetime] = None
        self._logger.info(f"{self.actor_log_name} initialized")

    async def health_check(self) -> bool:
        return self.running

    async def run(self) -> None:
        self._logger.info(f"Starting {self.actor_log_name} run")
        self.running = True

        while self.running:
            try:
                interval_hours = self._interval_hours()
                await self._maybe_refresh(interval_hours)
                # Re-check hourly so configuration changes get picked up
                # without restarting the actor.
                await asyncio.sleep(3600)
            except Exception:
                self._logger.opt(exception=True).error(
                    f"Error in {self.actor_log_name} refresh cycle"
                )
                await asyncio.sleep(self.error_backoff_seconds)

        self._logger.warning(f"{self.actor_log_name} stopped running")

    def stop(self) -> None:
        self.running = False

    def _interval_hours(self) -> int:
        from django.conf import settings

        return getattr(
            settings, self.settings_interval_key, self.default_interval_hours
        )

    async def _maybe_refresh(self, interval_hours: int) -> None:
        """Gate the refresh on the configured interval and advance the clock."""
        if self.last_refresh is not None:
            elapsed = _now_utc() - self.last_refresh
            if elapsed < datetime.timedelta(hours=interval_hours):
                return

        any_progress = await self._refresh_due(interval_hours)
        # Only mark the cycle complete if something actually refreshed —
        # otherwise the next loop iteration retries instead of waiting
        # another full interval window.
        if any_progress:
            self.last_refresh = _now_utc()

    async def _refresh_due(self, interval_hours: int) -> bool:
        """Subclass entry point. Return truthy iff at least one source refreshed."""
        raise NotImplementedError
