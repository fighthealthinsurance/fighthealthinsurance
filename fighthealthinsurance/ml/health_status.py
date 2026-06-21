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


def _model_key(model: Any) -> str:
    """Stable identifier for a backend instance, shared by the health sweep
    (which records per-model results) and the router (which looks them up).

    Prefers the unique friendly ``name`` the router stamps onto each registered
    model (so two providers exposing the same wire ``model`` id — e.g. the
    direct Anthropic API vs Azure Claude — don't collide), then the wire
    ``model`` id, then the class name.
    """
    name = getattr(model, "name", None)
    if name:
        return str(name)
    model_id = getattr(model, "model", None)
    if model_id:
        return str(model_id)
    return type(model).__name__


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
        # Per-model result of the last sweep, keyed by ``_model_key``. Rebound
        # atomically on each refresh and read lock-free by ``model_ok`` so the
        # router can consult it on the request path without ever blocking on the
        # sweep lock.
        self._health_map: Dict[str, bool] = {}
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

    def model_ok(self, model: Any) -> Optional[bool]:
        """Return the last sweep's result for ``model`` — ``True``/``False`` —
        or ``None`` if it hasn't been checked yet.

        Lock-free and non-blocking: it reads the atomically-rebound health map
        and never triggers a probe, so the router can call it on the request
        path. It does ensure the periodic sweep is running (in the background)
        so the cache gets populated, but returns immediately regardless.
        """
        self.ensure_started()
        return self._health_map.get(_model_key(model))

    def ensure_started(self) -> None:
        """Start the periodic health sweep exactly once, in the background, so
        cached results get populated without any caller blocking on the initial
        probe.

        The common (already-started) path is a lock-free flag read, so callers
        on the request path don't contend on the sweep lock. Shares the
        ``_initialized`` guard with :meth:`get_snapshot`, so whichever runs
        first does the one-time start and only a single refresh timer is ever
        scheduled.
        """
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
        threading.Thread(target=self._refresh, daemon=True, name="health-init").start()

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
        new_health: Dict[str, bool] = {}
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
                    new_health[_model_key(m)] = bool(ok)
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
        if candidates:
            # Only replace the cached per-model health when we actually ran a
            # sweep; on an enumeration failure (no candidates) keep the last
            # known-good map rather than wiping it to "unknown".
            self._health_map = new_health

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
        #
        # The authoritative throttle is a shared DB row so that, with multiple
        # web pods each running their own check, the cluster sends at most one
        # email per window. The in-memory timestamp is a per-pod fallback used
        # only if the DB throttle is unreachable.
        if not self._should_send_alert():
            return

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

    def _should_send_alert(self) -> bool:
        """Decide whether this caller should send the alert email now.

        Prefers a cross-pod DB throttle; on any DB error falls back to the
        per-pod in-memory throttle so a database hiccup degrades to "one email
        per pod per hour" rather than one per refresh.
        """
        try:
            from django.db import close_old_connections
            from fighthealthinsurance.models import ModelHealthAlertState

            # This can run from the long-lived refresh timer thread, which
            # never gets Django's per-request connection cleanup. Drop any
            # stale connection first so we don't fall back to the per-pod
            # throttle (and lose cluster-wide dedup) on a dead socket.
            close_old_connections()

            claimed = ModelHealthAlertState.try_claim(
                "internal_models_dead", ALERT_THROTTLE_SECONDS
            )
            # Keep the in-memory fallback timestamp warm so a later DB outage
            # doesn't immediately re-open the floodgates on this pod.
            if claimed:
                with self._lock:
                    self._last_alert_sent_at = time.monotonic()
            else:
                logger.debug("Suppressing alert email; throttled cluster-wide")
            return claimed
        except Exception:
            logger.opt(exception=True).warning(
                "Cross-pod alert throttle unavailable; using per-pod throttle"
            )
            return self._claim_in_memory_alert_slot()

    def _claim_in_memory_alert_slot(self) -> bool:
        """Per-pod fallback throttle. Returns True if this pod may send now."""
        now = time.monotonic()
        with self._lock:
            last = self._last_alert_sent_at
            if last is not None and now - last < ALERT_THROTTLE_SECONDS:
                logger.debug(
                    f"Suppressing alert email; last sent {now - last:.0f}s ago"
                )
                return False
            # Mark sent before the SMTP call so a concurrent caller doesn't
            # double-send while we're inside send_mail.
            self._last_alert_sent_at = now
            return True

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


def compute_model_health_details(timeout_seconds: int = 8) -> List[Dict[str, Any]]:
    """Run a fresh, uncached health check across every known model backend.

    Unlike :meth:`_HealthStatus.get_snapshot`, which returns a cached summary
    (used by the public ``live_models_status`` endpoint and deliberately keeps
    internal failures out of ``details``), this returns a full per-backend
    breakdown — ``{"name", "ok", "external", "error"}`` — for the staff-only
    system status dashboard. It does not send alerts or mutate the cached
    snapshot.

    Checks run in parallel with a shared deadline; a backend whose check has
    not finished by ``timeout_seconds`` is reported as not-ok with a timeout
    error. Results are sorted problems-first (down before up, internal before
    external, then by name) so on-call sees failures at the top.
    """
    try:
        candidates = list(ml_router_module.ml_router.all_models_by_cost)
    except Exception:
        # Propagate rather than returning [] — an empty list is indistinguishable
        # from "no models registered" and would let the caller (_model_status)
        # report a broken router as a healthy "0 backends" instead of an error.
        logger.opt(exception=True).warning(
            "compute_model_health_details could not enumerate models"
        )
        raise

    results: List[Dict[str, Any]] = []
    if not candidates:
        return results

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates)))
    try:
        future_map = {ex.submit(m.model_is_ok): m for m in candidates}
        concurrent.futures.wait(future_map, timeout=timeout_seconds)
        for future, m in future_map.items():
            name = str(
                getattr(m, "model", None) or getattr(m, "__class__", type(m)).__name__
            )
            is_external = bool(getattr(m, "external", True))
            ok = False
            err: Optional[str] = None
            if future.done():
                try:
                    ok = bool(future.result(timeout=0))
                    if not ok:
                        err = "not ok"
                except Exception as e:
                    err = str(e)
            else:
                err = f"timeout>{timeout_seconds}s"
            results.append(
                {"name": name, "ok": ok, "external": is_external, "error": err}
            )
    finally:
        # Return at the deadline rather than blocking on stragglers. Exiting a
        # `with ThreadPoolExecutor()` calls shutdown(wait=True), which joins
        # every submitted probe — so a single hung/slow model_is_ok() would
        # stall the staff status request well past timeout_seconds, defeating
        # the bounded live check. Cancel queued probes and let any in-flight
        # ones finish in the background instead.
        ex.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda r: (r["ok"], r["external"], r["name"]))
    return results


# Singleton used by views
health_status = _HealthStatus()
