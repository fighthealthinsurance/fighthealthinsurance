"""``SharedPlanDocumentsWorkflow`` -- reading the files of one share.

Started when a share's rows have been saved (the share page, or a ticked
box on Explain Policy or the appeal's plan documents step), and by the
plan documents sweep for a share still waiting after a while. Workflow id
``shared-plan-documents-{share batch}``, so one share has one open run.

It lists the share's rows still waiting, then reads each one in turn: the
activity pulls out the text, takes out personal details as best it can,
and stores the cleaned text on the row as ready for review, or marks the
row unreadable when no text could be read. A row whose reading fails on
every attempt is marked unreadable, so the sweep never picks it up again
and staff still have the original. A row deleted in the meantime is
reported as gone, and nothing is written for it.

History holds the share's uuid, the rows' ids and their statuses. The text
stays in Django.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from fighthealthinsurance.workflows.types import SharedPlanDocumentsInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import (
        shared_plan_documents as plan_document_activities,
    )

# Lookups and status writes: quick, and a few tries ride out a database
# hiccup. If they run out, the run fails and the hourly sweep starts again.
BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
)
# A reading gets three tries, so a short storage outage passes; after that
# the row is marked unreadable rather than read again every hour.
READ_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
)
# The readers stop after 60s; this leaves room for fetching the file.
READ_TIMEOUT = timedelta(minutes=3)


@workflow.defn
class SharedPlanDocumentsWorkflow:
    @workflow.run
    async def run(self, request: SharedPlanDocumentsInput) -> dict[str, int]:
        document_ids = await workflow.execute_activity(
            plan_document_activities.list_shared_plan_documents,
            request.share_batch,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=BOOKKEEPING_RETRY,
        )
        statuses: dict[str, int] = {}
        for document_id in document_ids:
            try:
                status = await workflow.execute_activity(
                    plan_document_activities.process_shared_plan_document,
                    document_id,
                    start_to_close_timeout=READ_TIMEOUT,
                    retry_policy=READ_RETRY,
                )
            except ActivityError:
                workflow.logger.warning("a shared plan document was not read")
                status = await workflow.execute_activity(
                    plan_document_activities.mark_shared_plan_document_unreadable,
                    document_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=BOOKKEEPING_RETRY,
                )
            statuses[status] = statuses.get(status, 0) + 1
        return statuses
