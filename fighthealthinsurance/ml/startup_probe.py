"""One-shot, deployment-time probe of all model backends.

On deploy we want to confirm every configured model backend can actually
answer (valid credentials, available quota, reachable endpoint) and get a
report of any that can't -- so on-call can investigate before users hit a
silently degraded backend.

This is intentionally *not* the per-request liveness check (which stays
config-only and free) and *not* something every web worker should run. It is
meant to be invoked once per deployment from a single container -- the same
one that launches the polling actors -- via ``run_startup_model_probe``.

Each probe makes a tiny "Hello" inference, so it costs a little; that's the
price of an end-to-end check. Disable with ``FHI_STARTUP_MODEL_PROBE=0``.
"""

import os
from typing import List, Optional, Tuple

from asgiref.sync import async_to_sync
from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router

# Where the dead-models report is sent. Matches the on-call alias used by the
# model liveness alert in ``health_status``.
SUPPORT_EMAIL = "support42@fighthealthinsurance.com"

ProbeResult = Tuple[str, bool, Optional[str]]


def _probe_enabled() -> bool:
    """Whether the deployment probe should run in this process.

    Skipped under the test configurations (which set ``TESTING``) so unit
    tests and the actor-launch command tests don't make network calls or send
    mail, and skippable in any environment via ``FHI_STARTUP_MODEL_PROBE=0``.
    """
    if os.getenv("TESTING") == "True":
        return False
    if os.getenv("FHI_STARTUP_MODEL_PROBE", "1") != "1":
        return False
    return True


def _send_dead_models_report(
    dead: List[Tuple[str, Optional[str]]],
    recipients: Optional[List[str]] = None,
) -> None:
    """Email a report of unreachable model backends to the support alias."""
    recipients = recipients or [SUPPORT_EMAIL]
    detail = "\n".join(f"- {name}: {err or 'no response'}" for name, err in dead)
    subject = f"[FHI] Model probe found {len(dead)} unreachable model backend(s)"
    message = (
        "The deployment model probe could not get a response from the "
        f"following model backend(s):\n\n{detail}\n\n"
        "Each backend was probed with a single 'Hello' inference. Likely "
        "causes are an invalid/disabled API key, an exhausted quota or "
        "billing problem, or an unreachable endpoint. Please investigate."
    )
    try:
        from django.conf import settings
        from django.core.mail import send_mail

        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
        logger.info(
            f"Emailed dead-models report ({len(dead)} backend(s)) to {recipients}"
        )
    except Exception:
        logger.opt(exception=True).error("Failed to send dead-models report email")


def run_startup_model_probe(
    recipients: Optional[List[str]] = None,
) -> Optional[List[ProbeResult]]:
    """Probe every backend once and email a report of any dead ones.

    Returns the full list of ``(name, ok, error)`` results, or ``None`` if the
    probe was skipped (disabled or test environment). Never raises.
    """
    if not _probe_enabled():
        logger.debug("Startup model probe skipped (disabled or test environment)")
        return None

    try:
        results: List[ProbeResult] = async_to_sync(ml_router.probe_all_models)()
    except Exception:
        logger.opt(exception=True).warning("Startup model probe failed to run")
        return None

    dead = [(name, err) for name, ok, err in results if not ok]
    if dead:
        _send_dead_models_report(dead, recipients=recipients)
    else:
        logger.info(
            f"Startup model probe: all {len(results)} backend(s) responded; "
            "no report sent"
        )
    return results
