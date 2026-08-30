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

from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    TimeoutError as TemporalTimeoutError,
    TimeoutType,
    is_cancelled_exception,
)

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

# Liveness for the vendor send: the activity heartbeats every ~10s while the
# blocking vendor call runs (see activities.fax._call_with_heartbeats). If the
# worker dies mid-send (OOM-killed at its memory limit on 2026-08-30), the
# heartbeats stop and the attempt fails in ~2 minutes instead of sitting
# silent until the 30-minute start-to-close.
SEND_HEARTBEAT_TIMEOUT = timedelta(seconds=120)

# A HEARTBEAT-timed-out attempt means the worker process is gone, so no vendor
# call can still be running -- the one failure where an automatic second
# attempt cannot double-fax. Anything else gets no automatic retry: the vendor
# layer (HylaFax/Sonic) already re-dials internally, and re-sending on a
# vendor-reported failure would burn paid submissions into the same problem.
MAX_SEND_ATTEMPTS = 2

# After a START_TO_CLOSE timeout the abandoned send thread may still be alive
# inside a living worker (Temporal cannot cancel it). Wait out the vendor
# layer's own internal timeouts before releasing the claim, so an explicit
# resend cannot overlap a zombie transmission.
ZOMBIE_DRAIN = timedelta(minutes=35)


def _timeout_type(error: BaseException) -> Optional[TimeoutType]:
    """The TimeoutType of an ActivityError's cause, or None if not a timeout."""
    cause = getattr(error, "cause", None)
    if isinstance(cause, TemporalTimeoutError):
        return cause.type
    return None


# Activities that must not give up partway through a transient outage retry
# forever with capped backoff, staying visibly running/retrying in the Temporal
# UI instead of dying into a silent FAILED state (maximum_attempts=0 = unlimited):
#
# * precheck -- a bounded retry that ran out would fail the whole workflow before
#   the fax was ever sent, orphaning it with no finalize and no notification (the
#   Ray delayed sweep is gated off under Temporal, so nothing else retries it).
# * finalize -- once the vendor send has happened this bookkeeping MUST run, or
#   the fax is physically delivered while the DB says sent=False and nobody is
#   notified.
#
# Terminal, non-transient outcomes are returned as STATUS_* values (not raised),
# so they are handled without retrying forever.
DURABLE_RETRY = RetryPolicy(
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
            retry_policy=DURABLE_RETRY,
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
                retry_policy=DURABLE_RETRY,
            )
            return False

        # status == STATUS_OK: send, then record the outcome. The atomic
        # vendor_send_completed claim in fax_send_core is the cross-orchestrator
        # dedupe; Temporal-level retries stay at maximum_attempts=1 because the
        # send is a synchronous thread Temporal cannot cancel -- a timed-out
        # attempt may still be transmitting, so blind retries could double-fax.
        # The one exception is a HEARTBEAT timeout (worker process died, thread
        # provably gone): release the leaked claim and try once more.
        success = False
        send_hung = False
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                success = await workflow.execute_activity(
                    fax_activities.send_fax_via_vendor,
                    args=[fax_input.hashed_email, fax_input.fax_uuid],
                    # The vendor layer has its own long internal timeouts (up
                    # to ~1300s per backend, across multiple backends), so the
                    # start_to_close window stays wide; the heartbeat timeout
                    # is what catches a dead worker quickly.
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=SEND_HEARTBEAT_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                break
            except ActivityError as e:
                # Let cancellation cancel the workflow; otherwise classify.
                if is_cancelled_exception(e):
                    raise
                timeout_type = _timeout_type(e)
                if (
                    timeout_type == TimeoutType.HEARTBEAT
                    and attempt < MAX_SEND_ATTEMPTS
                ):
                    workflow.logger.warning(
                        "Vendor fax send worker died mid-attempt; releasing claim and retrying"
                    )
                    await workflow.execute_activity(
                        fax_activities.release_send_claim,
                        args=[fax_input.hashed_email, fax_input.fax_uuid],
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=DURABLE_RETRY,
                    )
                    continue
                send_hung = timeout_type == TimeoutType.START_TO_CLOSE
                workflow.logger.warning("Vendor fax send failed after retries")
                success = False
                break

        # Finalize first so the user hears about a failure promptly; claim
        # cleanup below can afford to wait.
        await workflow.execute_activity(
            fax_activities.finalize_fax,
            args=[fax_input.hashed_email, fax_input.fax_uuid, success, False],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=DURABLE_RETRY,
        )

        if not success:
            # A failed send must never leave the claim stuck (that blocked all
            # resends until a human cleared it, 2026-08-30). In-process release
            # covers vendor-reported failures; this durable release covers the
            # paths where the process died holding the claim. After a
            # START_TO_CLOSE timeout the send thread may still be alive, so
            # wait out the vendor layer's internal timeouts first.
            if send_hung:
                await workflow.sleep(ZOMBIE_DRAIN)
            await workflow.execute_activity(
                fax_activities.release_send_claim,
                args=[fax_input.hashed_email, fax_input.fax_uuid],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=DURABLE_RETRY,
            )
        return success
