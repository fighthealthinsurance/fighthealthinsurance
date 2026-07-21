"""Per-worker background refresher that keeps the site-banner cache warm.

Why: the site banner is read on every page render (see
``context_processors.site_banner_context``). Rather than let request threads
hit the DB whenever the cache lapses, a single long-lived daemon thread per
worker process re-queries the active-banner list on a fixed interval and
writes it to the cache. Request threads then only ever read the warm cache, so
banner reads cost the DB at most one short-lived connection per worker per
interval -- and never on the request path.

Connection hygiene: this thread lives outside Django's request cycle, so it
never gets the per-request connection cleanup -- exactly the pattern that has
leaked connections from executor threads before (see the PG_USE_POOL comment
in settings.Prod.DATABASES). Each refresh cycle therefore closes its
connections in a ``finally``, holding a connection only for the milliseconds
of the query rather than pinning one (or leaking one per cycle) for the
thread's lifetime.

Lazy-started like ``ml/health_status.py`` so it needs no Celery or
``AppConfig`` wiring: the first call to ``SiteBanner.get_active_banners``
(i.e. the first page render on a worker) starts the thread, and it loops from
there.
"""

import threading
import time
from typing import Optional

from django.conf import settings
from django.db import close_old_connections, connections

from loguru import logger

# How often each worker re-queries the banner list in the background. Kept
# comfortably below ``SiteBanner.CACHE_TTL_SECONDS`` (10 min) so the cached
# entry is refreshed before it can expire -- under normal operation the request
# path never sees a cache miss. This also bounds how long an admin banner change
# takes to appear on workers *other* than the one that handled the edit (the
# editing worker's cache is cleared immediately by the save/delete signal).
REFRESH_INTERVAL_SECONDS = 5 * 60  # 5 minutes


class _SiteBannerRefresher:
    """Lazily-started refresh loop on one daemon thread per process."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
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
        """Start the refresh thread once per process.

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
        self._start_thread()

    def _start_thread(self) -> None:
        try:
            thread = threading.Thread(
                target=self._loop, daemon=True, name="site-banner-refresh"
            )
            thread.start()
            self._thread = thread
        except Exception:
            # Let a later request retry the start; until then the request-path
            # cold-miss fallback in get_active_banners keeps banners working.
            self._started = False
            logger.opt(exception=True).warning(
                "Could not start site banner refresh thread"
            )

    def _loop(self) -> None:
        # The interval comes first: ensure_started() is called from a request
        # whose own cold-miss lookup is already warming the cache, so an
        # immediate refresh would just duplicate that query.
        while True:
            time.sleep(REFRESH_INTERVAL_SECONDS)
            self._refresh_once()

    def _refresh_once(self) -> None:
        try:
            from fighthealthinsurance.models import SiteBanner

            # Defense against a broken connection surviving a failed cleanup
            # below: drop anything stale before querying.
            close_old_connections()
            SiteBanner.refresh_cache()
        except Exception:
            logger.opt(exception=True).warning("Site banner refresh failed")
        finally:
            # Always return this thread's connection between cycles. This
            # thread never gets per-request cleanup, and an unreturned
            # connection would pin a server slot for the thread's lifetime
            # (or leak a pool checkout under PG_USE_POOL) -- the exact
            # executor-thread leak called out in settings.Prod.DATABASES.
            try:
                connections.close_all()
            except Exception:
                logger.opt(exception=True).warning(
                    "Could not close site banner refresh connections"
                )


# Singleton used by SiteBanner.get_active_banners.
site_banner_refresher = _SiteBannerRefresher()
