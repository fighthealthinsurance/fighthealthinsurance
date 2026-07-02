"""
Health checks for the outbound fax backends.

This module reports which fax sending backends are currently registered on the
global ``flexible_fax_magic`` router and whether the Sonic fax backend can
authenticate. It is consumed by the staff system-status dashboard so on-call
can tell at a glance whether faxing (and Sonic in particular) is working.

The Sonic check makes a network login round-trip, so it is run under a timeout;
other backends are reported as configured without an active probe (they do not
expose a cheap liveness check).
"""

import concurrent.futures
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class FaxBackendHealthDetail:
    name: str
    ok: bool
    professional: bool = False
    probed: bool = False
    error: Optional[str] = None


# A ``check_health`` probe can make several sequential HTTP round-trips
# (SonicFax: login GET + login POST + members-page GET), each budgeted
# ``timeout`` individually. The outer deadline must cover their sum, or a
# backend answering every request comfortably within its per-request budget
# (i.e. working, just slow) would be reported as a false "timeout" failure.
_MAX_SEQUENTIAL_PROBE_REQUESTS = 3


def _probe_backend(backend: Any, timeout: float) -> tuple[bool, Optional[str]]:
    """Run ``backend.check_health()`` under a timeout.

    ``timeout`` is the per-HTTP-request budget passed to the backend; the
    probe as a whole is capped at ``timeout * _MAX_SEQUENTIAL_PROBE_REQUESTS``
    so the sum of individually-within-budget requests can't trip the outer
    deadline.

    Returns ``(ok, error)``. A timeout or any raised exception is reported as
    not-ok with a descriptive error rather than propagating, so one slow/broken
    backend never takes down the whole status page.
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    outer_timeout = timeout * _MAX_SEQUENTIAL_PROBE_REQUESTS
    try:
        # Pass the probe budget through so a backend's own per-request default
        # (e.g. SonicFax's 3s) doesn't report it down under latency well within
        # `timeout`.
        future = ex.submit(backend.check_health, timeout=timeout)
        ok = bool(future.result(timeout=outer_timeout))
        return (ok, None if ok else "health check returned False")
    except concurrent.futures.TimeoutError:
        return (False, f"timeout>{outer_timeout}s")
    except Exception as e:
        return (False, str(e))
    finally:
        # Don't let exiting the executor block on a hung probe (a `with` block
        # would shutdown(wait=True) and join it); make `timeout` a real cap.
        ex.shutdown(wait=False, cancel_futures=True)


def check_fax_backends_health(probe_timeout: float = 10.0) -> Dict[str, Any]:
    """Check the health of all registered fax backends.

    Returns a dict with:
    - ``backends``: list of per-backend details (name, ok, professional,
      probed, error).
    - ``total_backends``: number of registered backends.
    - ``sonic``: summary of the Sonic backend specifically
      (``configured``, ``active``, ``ok``, ``error``) since it is the backend
      most worth surfacing on the status page.
    """
    from fighthealthinsurance.fax_utils import SonicFax, flexible_fax_magic

    details: List[FaxBackendHealthDetail] = []
    sonic_active = False
    sonic_ok = False
    sonic_error: Optional[str] = None

    for backend in flexible_fax_magic.backends:
        name = type(backend).__name__
        professional = bool(getattr(backend, "professional", False))
        is_sonic = isinstance(backend, SonicFax)
        ok = True
        error: Optional[str] = None
        probed = False

        if is_sonic:
            sonic_active = True
            probed = True
            ok, error = _probe_backend(backend, probe_timeout)
            sonic_ok = ok
            sonic_error = error

        details.append(
            FaxBackendHealthDetail(
                name=name,
                ok=ok,
                professional=professional,
                probed=probed,
                error=error,
            )
        )

    # If Sonic isn't registered, figure out whether that's because it's not
    # configured (missing env vars) so the dashboard can say why.
    sonic_configured = sonic_active
    if not sonic_active:
        try:
            SonicFax()
            # Constructed fine but wasn't registered — unusual, surface it.
            sonic_configured = True
            sonic_error = "configured but not registered as a backend"
        except Exception as e:
            sonic_configured = False
            sonic_error = str(e)

    return {
        "backends": [
            {
                "name": d.name,
                "ok": d.ok,
                "professional": d.professional,
                "probed": d.probed,
                "error": d.error,
            }
            for d in details
        ],
        "total_backends": len(details),
        "sonic": {
            "configured": sonic_configured,
            "active": sonic_active,
            "ok": sonic_ok,
            "error": sonic_error,
        },
    }
