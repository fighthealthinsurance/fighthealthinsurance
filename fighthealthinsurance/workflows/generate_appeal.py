"""``GenerateAppealWorkflow`` -- durable, queued appeal-draft generation.

The interactive flow streams appeals to a watching user and stays in-process.
This workflow is for the queued shape: a user (or a future product surface)
asks for drafts, the work survives worker restarts, and the drafts land as
``ProposedAppeal`` rows.

Unlike the fax send, generation has no irreversible external side effect, and
the store step dedupes against existing drafts -- so the generation activity
retries freely. Payloads carry opaque identifiers only; no PHI enters
workflow history.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from fighthealthinsurance.workflows.types import GenerateAppealInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import appeal_journey as appeal_activities
    from fighthealthinsurance.appeal_journey_core import SITE_IS_GENERATING

# Bookkeeping steps retry forever with capped backoff (same rationale as the
# fax workflow's DURABLE_RETRY): a bounded retry running out would orphan the
# journey silently.
DURABLE_RETRY = RetryPolicy(maximum_attempts=0, maximum_interval=timedelta(minutes=5))

# Generation is safe to retry (idempotent store), so a real retry policy:
# transient model-backend failures get three attempts with backoff.
GENERATION_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=5),
)


# While the site's appeals page holds the lease, look again after these
# timers. An abandoned page's lease lapses within its TTL (5 minutes).
SITE_RECHECK_INITIAL = timedelta(minutes=5)
SITE_RECHECK_MAX = timedelta(minutes=30)
SITE_WAIT_LIMIT = timedelta(hours=6)


@workflow.defn
class GenerateAppealWorkflow:
    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        if not await self._precheck(journey):
            return 0
        stored = await self._generate(journey)
        # The site's appeals page is writing this case's letters. Wait on
        # durable timers until it lets go: letters it stored end the journey
        # (the precheck sees them); a page left before any were stored is
        # generated for here, so nobody is left without letters.
        delay = SITE_RECHECK_INITIAL
        waited = timedelta(0)
        while stored == SITE_IS_GENERATING:
            if waited >= SITE_WAIT_LIMIT:
                workflow.logger.warning(
                    "the site held the generation lease past the wait limit"
                )
                return 0
            await asyncio.sleep(delay.total_seconds())
            waited += delay
            delay = min(delay * 2, SITE_RECHECK_MAX)
            if not await self._precheck(journey):
                return 0
            stored = await self._generate(journey)
        return int(stored)

    async def _precheck(self, journey: GenerateAppealInput) -> bool:
        status = await workflow.execute_activity(
            appeal_activities.precheck_appeal_journey,
            args=[journey.hashed_email, journey.denial_uuid],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=DURABLE_RETRY,
        )
        if status != "ok":
            # not_found / no_denial_text / already_has_appeals are all terminal
            # and idempotent; the workflow records the status and stops.
            workflow.logger.info(f"Appeal journey ended at precheck: {status}")
            return False
        return True

    async def _generate(self, journey: GenerateAppealInput) -> int:
        stored = await workflow.execute_activity(
            appeal_activities.generate_and_store_appeals,
            args=[journey.hashed_email, journey.denial_uuid],
            # Above the core's GENERATION_BUDGET_SECONDS so the budget, not
            # the timeout, ends a slow attempt; heartbeats catch dead workers.
            start_to_close_timeout=timedelta(minutes=8),
            heartbeat_timeout=timedelta(seconds=120),
            retry_policy=GENERATION_RETRY,
        )
        return int(stored)
