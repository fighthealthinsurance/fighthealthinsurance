"""
Lightweight, cached health snapshot for model backends.

What & why:
- Computes how many model backends are currently reachable/healthy.
- Runs at startup and caches results; refreshes periodically (hourly) in background.
- Avoids heavy checks per request; endpoint simply returns the cached snapshot.

Trade-offs:
- Uses a simple ping inference with short timeout; does not validate output quality.
- Background refresh via threading.Timer keeps dependencies minimal (no Celery needed).
"""

import concurrent.futures
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.ml import ml_router as ml_router_module

REFRESH_INTERVAL_SECONDS = 60 * 60  # hourly
ALERT_THROTTLE_SECONDS = 60 * 60  # at most one alert email per hour


@dataclass
class BackendHealthDetail:
    name: str
    ok: bool
    error: Optional[str] = None


@dataclass
class HealthSnapshot:
    alive_models: int = 0
    last_checked: float = field(default_factory=lambda: time.time())
    details: List[BackendHealthDetail] = field(default_factory=list)


class _HealthStatus:
    def __init__(self):
        self._snapshot: HealthSnapshot = HealthSnapshot()
        self._timer: Optional[threading.Timer] = None
        self._initialized = False
        # Use RLock to allow reentrant locking and reduce deadlock chances
        self._lock = threading.RLock()
        # Fast mode for tests to avoid network stalls
        self._fast_mode = os.getenv("FHI_HEALTH_FAST", "0") == "1"
        # Monotonic timestamp of last alert email, used to throttle
        # outgoing alerts to at most one per ALERT_THROTTLE_SECONDS.
        self._last_alert_sent_at: Optional[float] = None

    def get_snapshot(self) -> Dict[str, Any]:
        # Use lock to prevent race condition on first access
        pending_alert: Optional[
            Tuple[int, int, List[BackendHealthDetail], Optional[str]]
        ] = None
        with self._lock:
            if not self._initialized:
                pending_alert = self._refresh_unlocked()
                self._schedule_refresh()
                self._initialized = True

        if pending_alert is not None:
            self._alert_if_all_internal_dead(*pending_alert)

        return {
            "alive_models": self._snapshot.alive_models,
            "last_checked": self._snapshot.last_checked,
            "details": [
                {"name": d.name, "ok": d.ok, "error": d.error}
                for d in self._snapshot.details
            ],
        }

    def _schedule_refresh(self):
        """Schedule the next refresh."""
        try:
            interval = 5 if self._fast_mode else REFRESH_INTERVAL_SECONDS
            self._timer = threading.Timer(interval, self._refresh)
            self._timer.daemon = True
            self._timer.start()
        except Exception as e:
            logger.warning(f"Failed to schedule health refresh: {e}")

    def _refresh_unlocked(
        self,
    ) -> Tuple[int, int, List[BackendHealthDetail], Optional[str]]:
        """
        Recalculate health snapshot using cheap checks and cache it.

        This method does NOT acquire the lock - caller must hold it if needed.
        Used during initialization when we already hold the lock.

        Returns ``(internal_total, internal_alive, internal_failures,
        enumeration_error)`` so the caller can fire the alert email outside
        the lock — ``send_mail`` can block on SMTP and we don't want any
        concurrent ``get_snapshot()`` reader stuck behind it.
        """
        alive_count = 0
        internal_total = 0
        internal_alive = 0
        internal_failures: List[BackendHealthDetail] = []

        # Choose a small, representative set of backends
        candidates = []
        enumeration_error: Optional[str] = None
        try:
            logger.debug("Starting to look up the models")
            candidates = ml_router_module.ml_router.all_models_by_cost
            logger.debug(f"Considering candidates {candidates}")
        except Exception as e:
            enumeration_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Could not get all_models_by_cost: {e}")
            candidates = []
        for m in candidates:
            if not getattr(m, "external", True):
                internal_total += 1
        # Run health checks in parallel with a shared deadline. We wait up to
        # timeout_seconds for the checks to finish, then classify every backend
        # from its *final* state. Doing the classification in a single pass (as
        # opposed to splitting it across an as_completed loop plus a "not done"
        # sweep) means a check that completes right at the deadline is never
        # dropped from both buckets, which could otherwise fire a false "all
        # internal models are dead" page for a slow-but-healthy backend.
        details: List[BackendHealthDetail] = []
        timeout_seconds = 10
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(candidates))
            ) as ex:
                future_map = {ex.submit(m.model_is_ok): m for m in candidates}
                # Block until all checks finish or the deadline elapses.
                concurrent.futures.wait(future_map, timeout=timeout_seconds)

                for future, m in future_map.items():
                    name = str(
                        getattr(m, "model", None)
                        or getattr(m, "__class__", type(m)).__name__
                    )
                    is_internal = not getattr(m, "external", True)
                    ok = False
                    err: Optional[str] = None
                    if future.done():
                        try:
                            ok = future.result(timeout=0)
                        except Exception as e:
                            err = str(e)
                            logger.debug(f"Health check error for {name}: {e}")
                    else:
                        err = f"timeout>{timeout_seconds}s"
                        details.append(
                            BackendHealthDetail(name=name, ok=False, error=err)
                        )
                    if ok:
                        alive_count += 1
                        if is_internal:
                            internal_alive += 1
                    elif is_internal:
                        internal_failures.append(
                            BackendHealthDetail(
                                name=name, ok=False, error=err or "not ok"
                            )
                        )

        snapshot = HealthSnapshot(
            alive_models=alive_count,
            last_checked=time.time(),
            details=details,
        )

        self._snapshot = snapshot

        return internal_total, internal_alive, internal_failures, enumeration_error

    def _alert_if_all_internal_dead(
        self,
        internal_total: int,
        internal_alive: int,
        internal_failures: List[BackendHealthDetail],
        enumeration_error: Optional[str] = None,
    ) -> None:
        """Log an error and email support when zero internal models are alive.

        If ``enumeration_error`` is set, the router itself failed and we have
        no real measurement; surface that distinctly so on-call doesn't chase
        a backend outage that's actually a router/init bug.
        """
        if internal_alive > 0:
            return
        if enumeration_error is not None:
            subject = "[FHI] Could not enumerate model backends"
            message = (
                "Model liveliness check could not enumerate backends "
                f"(ml_router.all_models_by_cost raised: {enumeration_error}). "
                "Internal model health is unknown."
            )
        else:
            detail = (
                ", ".join(f"{f.name}: {f.error or 'not ok'}" for f in internal_failures)
                or "no internal backends registered"
            )
            subject = "[FHI] All internal models are dead"
            message = (
                f"All internal models are dead "
                f"(internal_total={internal_total}, internal_alive=0). "
                f"Failures: {detail}"
            )
        logger.error(message)

        # Throttle outgoing emails to at most one per ALERT_THROTTLE_SECONDS,
        # shared across alert subjects (the recipient is the same on-call
        # address either way, and a duplicate page during an active incident
        # is more noise than signal). The error log above still fires every
        # refresh so log-based monitoring isn't suppressed.
        now = time.monotonic()
        with self._lock:
            last = self._last_alert_sent_at
            if last is not None and now - last < ALERT_THROTTLE_SECONDS:
                logger.debug(
                    f"Suppressing alert email; last sent {now - last:.0f}s ago"
                )
                return
            # Mark sent before the SMTP call so a concurrent caller doesn't
            # double-send while we're inside send_mail. If SMTP fails we still
            # keep the timestamp — retrying every refresh against a broken
            # mail server defeats the throttle's purpose.
            self._last_alert_sent_at = now

        try:
            from django.conf import settings
            from django.core.mail import send_mail

            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                ["support42@fighthealthinsurance.com"],
                fail_silently=False,
            )
        except Exception:
            logger.opt(exception=True).error(
                "Failed to send internal-models-dead alert email"
            )

    def _refresh(self):
        """
        Recalculate health snapshot (called from background timer).

        Acquires lock, performs refresh, then schedules next refresh outside lock.
        """
        with self._lock:
            pending_alert = self._refresh_unlocked()

        self._alert_if_all_internal_dead(*pending_alert)

        # Since only one timer no need to worry about lock.
        self._schedule_refresh()


# Singleton used by views
health_status = _HealthStatus()
