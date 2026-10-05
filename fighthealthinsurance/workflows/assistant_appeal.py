"""``AssistantAppealWorkflow``: letters drafted for an AI assistant to collect.

Started when the person agrees on our site. Reads the letter, asks our
questions, waits up to a day for the answers (a signal; the answers go to
Django, never through here), records the form as completed so the intake
journey sends no nudge, and runs ``GenerateAppealWorkflow`` as a child with
the same deterministic id the intake journey uses. Ids only in history.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

from fighthealthinsurance.workflows.types import (
    AssistantAppealInput,
    GenerateAppealInput,
)

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import appeal_journey as appeal_activities
    from fighthealthinsurance.activities import assistant_appeal as draft_activities

ANSWER_WAIT = timedelta(hours=24)
# Inside the draft's own day: past this the letters could not be collected.
RECONCILE_FOR = timedelta(hours=6)
RECONCILE_INITIAL_DELAY = timedelta(seconds=30)
RECONCILE_MAX_DELAY = timedelta(minutes=10)

BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=5, maximum_interval=timedelta(minutes=5)
)
# Reading and asking spend model calls; two attempts each, then on without.
MODEL_STEP_RETRY = RetryPolicy(
    maximum_attempts=2, initial_interval=timedelta(seconds=30)
)


@workflow.defn
class AssistantAppealWorkflow:
    def __init__(self) -> None:
        self._answered = False

    @workflow.signal
    def answers_filed(self) -> None:
        self._answered = True

    @workflow.query
    def draft_state(self) -> dict:
        return {"answered": self._answered}

    @workflow.run
    async def run(self, draft: AssistantAppealInput) -> str:
        args = [draft.hashed_email, draft.denial_uuid]
        try:
            return await self._run(draft, args)
        except ActivityError:
            # A bookkeeping step ran out of attempts: say so on the draft.
            try:
                await self._mark(args, "stopped")
            except ActivityError:
                workflow.logger.warning("could not mark the draft stopped")
            raise

    async def _run(self, draft: AssistantAppealInput, args: list) -> str:
        found = await self._model_step(
            draft_activities.read_letter, args, timedelta(minutes=3), True
        )
        if not found:
            return "not_found"
        asked = await self._model_step(
            draft_activities.ask_questions, args, timedelta(minutes=4), 0
        )
        if asked:
            try:
                await workflow.wait_condition(
                    lambda: self._answered, timeout=ANSWER_WAIT
                )
            except asyncio.TimeoutError:
                await self._mark(args, "expired")
                return "expired"
        started = await workflow.execute_activity(
            draft_activities.start_drafting,
            args=args,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=BOOKKEEPING_RETRY,
        )
        if not started:
            return "not_found"
        if not await self._start_generation(draft):
            await self._reconcile(draft, args)
        return str(
            await workflow.execute_activity(
                draft_activities.finish_drafts,
                args=args,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=BOOKKEEPING_RETRY,
            )
        )

    async def _model_step(self, step, args: list, timeout: timedelta, failed):
        """A model step out of attempts goes on without its result."""
        try:
            return await workflow.execute_activity(
                step,
                args=args,
                start_to_close_timeout=timeout,
                retry_policy=MODEL_STEP_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("model step out of attempts; going on without it")
            return failed

    async def _start_generation(self, draft: AssistantAppealInput) -> bool:
        """Run generation as our child; False when a standalone run holds
        the id (WorkflowAlreadyStartedError), which the caller reconciles."""
        try:
            await workflow.execute_child_workflow(
                "GenerateAppealWorkflow",
                GenerateAppealInput(
                    hashed_email=draft.hashed_email, denial_uuid=draft.denial_uuid
                ),
                id=f"generate-appeal-{draft.denial_uuid}",
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "generation already running for this denial; reconciling"
            )
            return False
        except ChildWorkflowError:
            # Out of attempts; finish_drafts says whether anything landed.
            workflow.logger.warning("generation child failed; collecting what exists")
        return True

    async def _reconcile(self, draft: AssistantAppealInput, args: list) -> None:
        """The intake journey's rule: another run is not drafts. Check the
        durable outcome on a backoff and take generation over if that run
        closes short of it."""
        delay = RECONCILE_INITIAL_DELAY
        until = workflow.now() + RECONCILE_FOR
        while True:
            remaining = until - workflow.now()
            if remaining <= timedelta(0):
                break
            await asyncio.sleep(min(delay, remaining).total_seconds())
            delay = min(delay * 2, RECONCILE_MAX_DELAY)
            if workflow.now() >= until:
                break
            satisfied = await workflow.execute_activity(
                appeal_activities.check_generation_postcondition,
                args=args,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=BOOKKEEPING_RETRY,
            )
            if satisfied or workflow.now() >= until:
                return
            if await self._start_generation(draft):
                return
        workflow.logger.warning(
            "generation postcondition unmet after the reconciliation window"
        )

    async def _mark(self, args: list, status: str) -> None:
        await workflow.execute_activity(
            draft_activities.mark_draft_status,
            args=[*args, status],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=BOOKKEEPING_RETRY,
        )
