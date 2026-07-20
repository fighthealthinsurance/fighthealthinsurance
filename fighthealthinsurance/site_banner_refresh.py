"""Per-worker background refresher that keeps the site-banner cache warm.

Why: the site banner is read on every page render (see
``context_processors.site_banner_context``). Rather than let request threads
hit the DB whenever the cache lapses, a single daemon thread per worker process
re-queries the active-banner list on a fixed interval and writes it to the
cache. Request threads then only ever read the warm cache, so banner reads cost
the DB at most one query per worker per interval -- and never on the request
path.

Follows the same lazy-start / ``threading.Timer`` pattern as
``ml/health_status.py`` so it needs no Celery or ``AppConfig`` wiring: the first
call to ``SiteBanner.get_active_banners`` (i.e. the first page render on a
worker) starts the timer chain, and it self-perpetuates from there.
"""

import threading
from typing import Optional

from django.conf import settings
from loguru import logger

# How often each worker re-queries the banner list in the background. Kept
# comfortably below ``SiteBanner.CACHE_TTL_SECONDS`` (10 min) so the cached
# entry is refreshed before it can expire -- under normal operation the request
# path never sees a cache miss. This also bounds how long an admin banner change
# takes to appear on workers *other* than the one that handled the edit (the
# editing worker's cache is cleared immediately by the save/delete signal).
REFRESH_INTERVAL_SECONDS = 5 * 60  # 5 minutes


class _SiteBannerRefresher:
    """Lazily-started, self-rescheduling daemon timer, one instance per process."""

    def __init__(self) -> None:
        self._timer: Optional[threading.Timer] = None
        self._started = False
        # Short lock held only while flipping ``_started``; never held during a
        # refresh, so request-path callers of ensure_started() never contend.
        self._start_lock = threading.Lock()

    def _enabled(self) -> bool:
        """Whether the background thread should run in this configuration.

        Disabled in the ``Test*`` configs: background DB access from a separate
        thread would break ``TestCase`` transaction isolation, and those configs
        use ``DummyCache`` so there is nothing to keep warm anyway.
        """
        return bool(getattr(settings, "SITE_BANNER_BACKGROUND_REFRESH", True))

    def ensure_started(self) -> None:
        """Start the recurring refresh once per process.

        Idempotent, cheap, and non-blocking -- safe to call on every request.
        Uses a lock-free fast path on the ``_started`` flag; the short lock is
        only taken on the first call.
        """
        if not self._enabled() or self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self._started = True
        self._schedule_next()

    def _schedule_next(self) -> None:
        try:
            timer = threading.Timer(REFRESH_INTERVAL_SECONDS, self._run)
            timer.daemon = True
            timer.start()
            self._timer = timer
        except Exception:
            logger.opt(exception=True).warning("Could not schedule site banner refresh")

    def _run(self) -> None:
        try:
            # This runs on a long-lived thread outside Django's request
            # lifecycle, which never gets the per-request connection cleanup.
            # Drop any stale thread-local connection before querying so we don't
            # trip over a dead socket (mirrors ml/health_status.py).
            from django.db import close_old_connections

            from fighthealthinsurance.models import SiteBanner

            close_old_connections()
            SiteBanner.refresh_cache()
        except Exception:
            logger.opt(exception=True).warning("Site banner refresh failed")
        finally:
            # Always reschedule so a single failed refresh never kills the timer
            # chain. If refreshes keep failing the cached entry eventually lapses
            # (TTL) and the request path repopulates it synchronously.
            self._schedule_next()


# Singleton used by SiteBanner.get_active_banners.
site_banner_refresher = _SiteBannerRefresher()
