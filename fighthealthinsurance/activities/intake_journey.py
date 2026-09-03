"""Temporal activities for the intake journey's bookkeeping.

Asyncio activities like the appeal-journey's: short, opaque identifiers
only, sanitized errors. No heartbeats needed -- both are seconds-long.
"""

from loguru import logger

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import intake_journey_core


@activity.defn
async def send_abandonment_nudge(hashed_email: str, denial_uuid: str) -> bool:
    try:
        return await intake_journey_core.send_abandonment_nudge(
            hashed_email, denial_uuid
        )
    except Exception:
        logger.opt(exception=True).error(
            f"Intake nudge failed for denial {denial_uuid}"
        )
        raise ApplicationError(f"nudge failed for denial {denial_uuid}") from None


@activity.defn
async def close_incomplete_journey(hashed_email: str, denial_uuid: str) -> bool:
    try:
        return await intake_journey_core.close_incomplete_journey(
            hashed_email, denial_uuid
        )
    except Exception:
        logger.opt(exception=True).error(
            f"Intake close failed for denial {denial_uuid}"
        )
        raise ApplicationError(f"close failed for denial {denial_uuid}") from None
