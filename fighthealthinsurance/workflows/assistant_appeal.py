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
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

from fighthealthinsurance.workflows.types import (
    AssistantAppealInput,
    GenerateAppealInput,
)

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import assistant_appeal as draft_activities

ANSWER_WAIT = timedelta(hours=24)

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
        found = await workflow.execute_activity(
            draft_activities.read_letter,
            args=args,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=MODEL_STEP_RETRY,
        )
        if not found:
            return "not_found"
        asked = await workflow.execute_activity(
            draft_activities.ask_questions,
            args=args,
            start_to_close_timeout=timedelta(minutes=4),
            retry_policy=MODEL_STEP_RETRY,
        )
        if asked:
            try:
                await workflow.wait_condition(
                    lambda: self._answered, timeout=ANSWER_WAIT
                )
            except asyncio.TimeoutError:
                await self._mark(args, "expired")
                return "expired"
        await workflow.execute_activity(
            draft_activities.start_drafting,
            args=args,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=BOOKKEEPING_RETRY,
        )
        try:
            await workflow.execute_child_workflow(
                "GenerateAppealWorkflow",
                GenerateAppealInput(
                    hashed_email=draft.hashed_email, denial_uuid=draft.denial_uuid
                ),
                id=f"generate-appeal-{draft.denial_uuid}",
            )
        except WorkflowAlreadyStartedError:
            # The site is already drafting this case; its page shows them.
            await self._mark(args, "on_site")
            return "on_site"
        except ChildWorkflowError:
            # Out of attempts; finish_drafts says whether anything landed.
            workflow.logger.warning("generation child failed; collecting what exists")
        return str(
            await workflow.execute_activity(
                draft_activities.finish_drafts,
                args=args,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=BOOKKEEPING_RETRY,
            )
        )

    async def _mark(self, args: list, status: str) -> None:
        await workflow.execute_activity(
            draft_activities.mark_draft_status,
            args=[*args, status],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=BOOKKEEPING_RETRY,
        )
