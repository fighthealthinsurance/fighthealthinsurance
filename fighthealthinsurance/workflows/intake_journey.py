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
            await workflow.execute_activity(
                intake_activities.send_abandonment_nudge,
                args=[journey.hashed_email, journey.denial_uuid],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=BOOKKEEPING_RETRY,
            )
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
        await workflow.execute_child_workflow(
            "GenerateAppealWorkflow",
            GenerateAppealInput(
                hashed_email=journey.hashed_email,
                denial_uuid=journey.denial_uuid,
            ),
            id=f"generate-appeal-{journey.denial_uuid}",
        )
        return "completed"
