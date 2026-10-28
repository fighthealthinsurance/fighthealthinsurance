"""``PlanDocumentsSweepWorkflow`` -- the hourly plan documents sweep.

The ``plan-documents-sweep`` Temporal Schedule (see
``temporal_client.ensure_plan_documents_schedule``) starts one run an hour.
A run does two things, and a failure in the first never stops the second:

1. Sends the share page's reminders that are due, one activity per
   reminder, each run once and never retried. The activity claims the
   reminder by deleting its row and reads the address from it, so the
   address never reaches history, and a reminder goes at most once.
2. Starts a reading (SharedPlanDocumentsWorkflow) for each share still
   waiting a while after it was saved: its start from the request was lost,
   or a run failed, or staff asked for files to be read again. The readings
   are children the sweep does not wait for (parent close policy ABANDON).
   A share whose reading is already running is left to it; one whose
   earlier reading has closed gets a new run under the same id.

History holds the run's numbers, the reminders' row ids, true or false for
each, the shares' uuids and two counts.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError

from fighthealthinsurance.workflows.shared_plan_documents import (
    shared_plan_documents_workflow_id,
)
from fighthealthinsurance.workflows.types import (
    PlanDocumentsSweepInput,
    SharedPlanDocumentsInput,
)

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import (
        plan_documents_sweep as sweep_activities,
    )

# Lookups: a few quick tries; the next hourly run is the real retry.
LOOKUP_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
)
# The mail server can accept a message and the answer still be lost, so a
# send is never retried: a failed reminder is not sent again.
SEND_RETRY = RetryPolicy(maximum_attempts=1)
# The same pace the Ray email loop keeps between sends.
SEND_PACING = timedelta(seconds=2)


@workflow.defn
class PlanDocumentsSweepWorkflow:
    @workflow.run
    async def run(self, request: PlanDocumentsSweepInput) -> dict[str, int]:
        sent = await self._send_reminders(request.max_reminders)
        started = await self._start_readings(
            request.received_minutes, request.max_batches
        )
        return {"reminders_sent": sent, "readings_started": started}

    async def _send_reminders(self, limit: int) -> int:
        try:
            due = await workflow.execute_activity(
                sweep_activities.find_due_plan_documents_reminders,
                limit,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=LOOKUP_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("due reminders not looked up; next run tries")
            return 0
        sent = 0
        for reminder_id in due:
            try:
                delivered = await workflow.execute_activity(
                    sweep_activities.send_plan_documents_reminder,
                    reminder_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=SEND_RETRY,
                )
            except ActivityError:
                workflow.logger.warning("a plan documents reminder failed; not retried")
                continue
            if delivered:
                sent += 1
                await workflow.sleep(SEND_PACING)
        return sent

    async def _start_readings(self, received_minutes: int, limit: int) -> int:
        batches = await workflow.execute_activity(
            sweep_activities.find_waiting_share_batches,
            args=[received_minutes, limit],
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=LOOKUP_RETRY,
        )
        started = 0
        for share_batch in batches:
            try:
                await workflow.start_child_workflow(
                    "SharedPlanDocumentsWorkflow",
                    SharedPlanDocumentsInput(share_batch=share_batch),
                    id=shared_plan_documents_workflow_id(share_batch),
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    # A closed run never blocks a new one: rows set back to
                    # waiting (staff "Read again") are read again.
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                continue
            started += 1
        return started
