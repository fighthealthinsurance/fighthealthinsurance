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

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from fighthealthinsurance.workflows.types import GenerateAppealInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import appeal_journey as appeal_activities

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


@workflow.defn
class GenerateAppealWorkflow:
    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
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
            return 0

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
