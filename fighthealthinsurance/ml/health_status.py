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

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
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
        self._lock = threading.Lock()
        self._snapshot: HealthSnapshot = HealthSnapshot()
        self._timer: Optional[threading.Timer] = None
        # Compute immediately at startup
        self._refresh()
        # Schedule background refresh
        self._schedule_refresh()

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "alive_models": self._snapshot.alive_models,
                "last_checked": self._snapshot.last_checked,
                "details": [
                    {"name": d.name, "ok": d.ok, "error": d.error}
                    for d in self._snapshot.details
                ],
            }

    def _schedule_refresh(self):
        try:
            self._timer = threading.Timer(REFRESH_INTERVAL_SECONDS, self._refresh)
            self._timer.daemon = True
            self._timer.start()
        except Exception as e:
            logger.warning(f"Failed to schedule health refresh: {e}")

    def _refresh(self):
        """Recalculate health snapshot using cheap checks and cache it."""
        details: List[BackendHealthDetail] = []
        alive_count = 0

        # Choose a small, representative set of backends
        try:
            candidates = ml_router_module.ml_router.all_models_by_cost
        except Exception as e:
            logger.warning(f"Could not get all_models_by_cost: {e}")
            candidates = []

        for m in candidates:
            name = (
                getattr(m, "model", None) or getattr(m, "__class__", type(m)).__name__
            )
            name = str(name)
            ok = False
            err: Optional[str] = None
            try:
                ok = m.model_is_ok()
            except Exception as e:
                logger.debug(f"Error checking on model {m}")
            if ok:
                alive_count += 1
            # details.append(BackendHealthDetail(name=name, ok=ok, error=err))

        snapshot = HealthSnapshot(
            alive_models=alive_count,
            last_checked=time.time(),
            details=details,
        )

        with self._lock:
            self._snapshot = snapshot

        # Re-schedule next refresh
        self._schedule_refresh()


# Singleton used by views
health_status = _HealthStatus()
