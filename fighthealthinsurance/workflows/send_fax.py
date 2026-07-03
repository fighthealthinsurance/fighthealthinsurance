"""``SendFaxWorkflow`` -- durable orchestration of a single appeal fax.

This replaces two pieces of Ray machinery:

* the immediate ``FaxActor.do_send_fax`` call, and
* the ``FaxPollingActor`` 60s loop that re-sends faxes older than an hour --
  here that delay is just a durable ``workflow.sleep`` timer.

The workflow body is deterministic: it only orchestrates. All I/O (DB, vendor
fax send, email) happens in the activities defined in
``fighthealthinsurance.activities.fax``, and only opaque identifiers cross the
boundary, so no PHI lands in workflow history.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

from fighthealthinsurance.fax_status import (
    STATUS_ALREADY_SENT,
    STATUS_MISSING_DENIAL,
    STATUS_MISSING_DESTINATION,
    STATUS_NOT_FOUND,
)
from fighthealthinsurance.workflows.types import SendFaxInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import fax as fax_activities

# How long to wait before sending when ``delay_send`` is set. Mirrors the old
# fax-polling actor's "older than 1 hour" threshold.
DELAYED_SEND_WAIT = timedelta(hours=1)

# Finalize records the outcome (sent/fax_success/appeal) and notifies the user.
# Once the vendor send has happened this bookkeeping MUST eventually run, or the
# fax is physically delivered while the DB says sent=False and nobody is
# notified. So: retry forever with capped backoff -- the workflow stays visibly
# running/retrying in the Temporal UI through e.g. a DB outage instead of dying
# into a silent FAILED state. (maximum_attempts=0 means unlimited.)
FINALIZE_RETRY = RetryPolicy(
    maximum_attempts=0,
    maximum_interval=timedelta(minutes=5),
)


@workflow.defn
class SendFaxWorkflow:
    @workflow.run
    async def run(self, fax_input: SendFaxInput) -> bool:
        if fax_input.delay_send:
            await workflow.sleep(DELAYED_SEND_WAIT)

        status = await workflow.execute_activity(
            fax_activities.precheck_fax,
            args=[fax_input.hashed_email, fax_input.fax_uuid],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        # Terminal precheck outcomes: nothing left to send.
        if status in (STATUS_NOT_FOUND, STATUS_ALREADY_SENT, STATUS_MISSING_DENIAL):
            return False

        # Missing destination is recorded as a failed send (with the user
        # follow-up email), matching the original actor behavior.
        if status == STATUS_MISSING_DESTINATION:
            await workflow.execute_activity(
                fax_activities.finalize_fax,
                args=[fax_input.hashed_email, fax_input.fax_uuid, False, True],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=FINALIZE_RETRY,
            )
            return False

        # status == STATUS_OK: send (with retries), then record the outcome.
        # The send is idempotent (vendor_send_completed marker), so retrying is
        # safe -- a re-run after a crash short-circuits instead of re-faxing.
        try:
            success = await workflow.execute_activity(
                fax_activities.send_fax_via_vendor,
                args=[fax_input.hashed_email, fax_input.fax_uuid],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as e:
            # Let cancellation cancel the workflow; otherwise record a failed
            # send and still run finalize so the user is notified.
            if is_cancelled_exception(e):
                raise
            workflow.logger.warning("Vendor fax send failed after retries")
            success = False

        await workflow.execute_activity(
            fax_activities.finalize_fax,
            args=[fax_input.hashed_email, fax_input.fax_uuid, success, False],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FINALIZE_RETRY,
        )
        return success
