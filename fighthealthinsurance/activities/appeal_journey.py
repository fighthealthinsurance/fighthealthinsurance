"""Temporal activities for the queued appeal-generation journey.

Thin synchronous wrappers around :mod:`fighthealthinsurance.appeal_journey_core`,
following the fax activities' conventions: ``close_old_connections`` at entry,
opaque identifiers only, sanitized exceptions, and heartbeats around the slow
generation call (via the shared helper in :mod:`.fax`) so a dead worker is
detected in seconds rather than at the start-to-close timeout.
"""

from django.db import close_old_connections

from loguru import logger

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import appeal_journey_core
from fighthealthinsurance.activities.fax import _call_with_heartbeats


@activity.defn
def precheck_appeal_journey(hashed_email: str, denial_uuid: str) -> str:
    close_old_connections()
    denial = appeal_journey_core.load_denial(hashed_email, denial_uuid)
    if denial is None:
        return appeal_journey_core.STATUS_NOT_FOUND
    return appeal_journey_core.precheck_appeal_journey(denial)


@activity.defn
def generate_and_store_appeals(hashed_email: str, denial_uuid: str) -> int:
    """Generate + persist drafts; returns the number stored this attempt."""
    close_old_connections()
    denial = appeal_journey_core.load_denial(hashed_email, denial_uuid)
    if denial is None:
        return 0
    try:
        return int(
            _call_with_heartbeats(
                appeal_journey_core.generate_and_store_appeals, denial
            )
        )
    except Exception:
        # Keep detail in worker logs; history gets only the opaque uuid.
        logger.opt(exception=True).error(
            f"Appeal generation failed for denial {denial_uuid}"
        )
        raise ApplicationError(
            f"appeal generation failed for denial {denial_uuid}"
        ) from None
