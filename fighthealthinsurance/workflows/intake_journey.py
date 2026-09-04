"""``IntakeJourneyWorkflow`` -- the durable journey from the first screen.

Starts when a person begins submitting denial information (the first
substantive step creates the Denial row, so the journey keys on its uuid)
and durably tracks their progress via signals. Design and product
decisions: ``docs/appeal-intake-journey-design.md``.

- One signal per meaningful step (opaque ids and a step LABEL only; the
  actual form data goes to Django exactly as before -- this workflow
  tracks state, never content).
- Abandonment: a single email nudge at 24h, only when the user opted into
  stored contact; the journey closes at 30 days regardless.
- On form completion it runs ``GenerateAppealWorkflow`` as a CHILD
  workflow -- intent to generate is therefore held durably from screen
  one, which is what deletes the dispatch-durability gap.
- A query reports the current step so the frontend can offer durable
  resume-where-you-left-off.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from fighthealthinsurance.workflows.types import GenerateAppealInput, IntakeJourneyInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import intake_journey as intake_activities

NUDGE_AFTER = timedelta(hours=24)
CLOSE_AFTER = timedelta(days=30)

# Bookkeeping activities retry with a bound: a nudge or close that cannot
# land after several tries should fail visibly, not spin forever on a
# journey that is already best-effort.
BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=5, maximum_interval=timedelta(minutes=5)
)

# The fax rule applied to email: SMTP can accept a message and the
# acknowledgment still be lost, so retrying an ambiguous send can deliver
# up to five nudges to one person. One attempt; a failed nudge just means
# no nudge (external review).
NUDGE_RETRY = RetryPolicy(maximum_attempts=1)

# After a child-start collision (a standalone generation already owns
# generate-appeal-{uuid}), the journey verifies the durable postcondition on
# a backoff instead of declaring success, and retakes ownership if that run
# dies short of the target. Bounded: past RECONCILE_FOR it closes as
# "deferred" rather than waiting forever (external review).
RECONCILE_FOR = timedelta(hours=24)
RECONCILE_INITIAL_DELAY = timedelta(seconds=30)
RECONCILE_MAX_DELAY = timedelta(minutes=10)

STEP_STARTED = "started"
STEP_COMPLETED = "completed"


@workflow.defn
class IntakeJourneyWorkflow:
    def __init__(self) -> None:
        self._step: str = STEP_STARTED
        self._completed: bool = False
        self._contact_opt_in: bool = False

    @workflow.signal
    def step_reached(self, step: str) -> None:
        """The user advanced to ``step`` (an opaque label, no content)."""
        if not self._completed:
            self._step = step

    @workflow.signal
    def contact_opt_in(self, opted_in: bool) -> None:
        self._contact_opt_in = opted_in

    @workflow.signal
    def form_completed(self) -> None:
        self._completed = True

    @workflow.query
    def journey_state(self) -> dict:
        return {"step": self._step, "completed": self._completed}

    @workflow.run
    async def run(self, journey: IntakeJourneyInput) -> str:
        self._contact_opt_in = journey.contact_opt_in

        # Phase 1: wait for completion, nudging once at the 24h mark.
        try:
            await workflow.wait_condition(lambda: self._completed, timeout=NUDGE_AFTER)
        except asyncio.TimeoutError:
            pass
        if not self._completed and self._contact_opt_in:
            try:
                await workflow.execute_activity(
                    intake_activities.send_abandonment_nudge,
                    args=[journey.hashed_email, journey.denial_uuid],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=NUDGE_RETRY,
                )
            except Exception:
                # A failed or ambiguous nudge never fails the journey.
                workflow.logger.warning("abandonment nudge failed; not retried")
        if not self._completed:
            try:
                await workflow.wait_condition(
                    lambda: self._completed, timeout=CLOSE_AFTER - NUDGE_AFTER
                )
            except asyncio.TimeoutError:
                pass

        if not self._completed:
            # 30 days without completion: close and hand the uuid to the
            # incomplete-form hygiene hook (a stub in v1; the deletion
            # policy is its own follow-up).
            await workflow.execute_activity(
                intake_activities.close_incomplete_journey,
                args=[journey.hashed_email, journey.denial_uuid],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=BOOKKEEPING_RETRY,
            )
            return "abandoned"

        self._step = STEP_COMPLETED
        # Generation as a CHILD workflow, on its own task queue with its own
        # retry policy; the deterministic child id keeps duplicate journeys
        # idempotent, and precheck no-ops if drafts already exist (e.g. the
        # interactive flow delivered them first).
        if await self._start_generation(journey):
            return "completed"
        # A standalone generation already owns this denial's workflow id. A
        # child cannot attach to it, and "another run exists" is not
        # "drafts exist": verify the durable postcondition on a backoff and
        # retake ownership if that run closes short of the target.
        delay = RECONCILE_INITIAL_DELAY
        reconcile_until = workflow.now() + RECONCILE_FOR
        while workflow.now() < reconcile_until:
            await asyncio.sleep(delay.total_seconds())
            delay = min(delay * 2, RECONCILE_MAX_DELAY)
            satisfied = await workflow.execute_activity(
                intake_activities.check_generation_postcondition,
                args=[journey.hashed_email, journey.denial_uuid],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=BOOKKEEPING_RETRY,
            )
            if satisfied:
                return "completed"
            if await self._start_generation(journey):
                return "completed"
        workflow.logger.warning(
            "generation postcondition unmet after the reconciliation window"
        )
        return "deferred"

    async def _start_generation(self, journey: IntakeJourneyInput) -> bool:
        """Run generation as our child; False when a standalone run holds
        the id (WorkflowAlreadyStartedError), which the caller reconciles."""
        try:
            await workflow.execute_child_workflow(
                "GenerateAppealWorkflow",
                GenerateAppealInput(
                    hashed_email=journey.hashed_email,
                    denial_uuid=journey.denial_uuid,
                ),
                id=f"generate-appeal-{journey.denial_uuid}",
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "generation already running for this denial; reconciling"
            )
            return False
        return True
