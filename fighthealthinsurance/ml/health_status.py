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
from typing import Any, Dict, List, Optional

from loguru import logger

from fighthealthinsurance.ml import ml_router as ml_router_module

REFRESH_INTERVAL_SECONDS = 60 * 60  # hourly


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

    def get_snapshot(self) -> Dict[str, Any]:
        # Use lock to prevent race condition on first access
        with self._lock:
            if not self._initialized:
                self._refresh_unlocked()
                self._schedule_refresh()
                self._initialized = True

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

    def _refresh_unlocked(self):
        """
        Recalculate health snapshot using cheap checks and cache it.

        This method does NOT acquire the lock - caller must hold it if needed.
        Used during initialization when we already hold the lock.
        """
        alive_count = 0

        # Choose a small, representative set of backends
        candidates = []
        try:
            logger.debug("Starting to look up the models")
            candidates = ml_router_module.ml_router.all_models_by_cost
            logger.debug(f"Considering candidates {candidates}")
        except Exception as e:
            logger.warning(f"Could not get all_models_by_cost: {e}")
            candidates = []
        # Run health checks in parallel with per-model timeouts
        details: List[BackendHealthDetail] = []
        timeout_seconds = 10
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(candidates))
            ) as ex:
                future_map = {ex.submit(m.model_is_ok): m for m in candidates}
                try:
                    for future in concurrent.futures.as_completed(
                        future_map, timeout=timeout_seconds
                    ):
                        m = future_map[future]
                        name = (
                            getattr(m, "model", None)
                            or getattr(m, "__class__", type(m)).__name__
                        )
                        name = str(name)
                        ok = False
                        err: Optional[str] = None
                        try:
                            # This should already be ready.
                            ok = future.result(timeout=0)
                        except Exception as e:
                            err = str(e)
                            logger.debug(f"Health check error for {name}: {e}")
                        if ok:
                            alive_count += 1
                except concurrent.futures.TimeoutError:
                    logger.debug(
                        "Timed out checking backends, remaining marked as dead."
                    )

                # Handle any futures that didn't complete within the as_completed window
                for future, m in future_map.items():
                    if not future.done():
                        name = (
                            getattr(m, "model", None)
                            or getattr(m, "__class__", type(m)).__name__
                        )
                        name = str(name)
                        details.append(
                            BackendHealthDetail(
                                name=name, ok=False, error=f"timeout>{timeout_seconds}s"
                            )
                        )

        snapshot = HealthSnapshot(
            alive_models=alive_count,
            last_checked=time.time(),
            details=details,
        )

        self._snapshot = snapshot

    def _refresh(self):
        """
        Recalculate health snapshot (called from background timer).

        Acquires lock, performs refresh, then schedules next refresh outside lock.
        """
        with self._lock:
            self._refresh_unlocked()

        # Since only one timer no need to worry about lock.
        self._schedule_refresh()


# Singleton used by views
health_status = _HealthStatus()
