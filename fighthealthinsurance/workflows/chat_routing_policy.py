"""``ChatRoutingPolicyWorkflow`` -- the scheduled writer of chat routing
policies.

The ``chat-routing-policy`` Temporal Schedule (see
``temporal_client.ensure_chat_policy_schedule``) starts one run a day. A
run calls a single activity that reads ChatTurn metadata over a window (a
week by default) and appends one ChatRoutingPolicy row, which the chat path reads
through a short cache (``ml/chat_policy.py``). The activity is given the
run's own id and stores it on the row, so retried attempts still leave one
row per run. Temporal is never on the chat path itself: chat routes the
same way whether this runs or not, and the ``compute_chat_policy`` command
writes the same row by hand.

History holds numbers and ids only: the window and the run id in, the new
row's id out.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from fighthealthinsurance.workflows.types import ChatRoutingPolicyInput

with workflow.unsafe.imports_passed_through():
    from fighthealthinsurance.activities import (
        chat_routing_policy as policy_activities,
    )

# A few quick attempts, then give up: the schedule's next run, a day later,
# is the real retry. The last row stays fresh for 36 hours, so one missed run
# changes nothing, and a stale row only means chat routes by the default.
POLICY_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
)


@workflow.defn
class ChatRoutingPolicyWorkflow:
    @workflow.run
    async def run(self, request: ChatRoutingPolicyInput) -> int:
        row_id = await workflow.execute_activity(
            policy_activities.compute_and_store_chat_policy,
            args=[request.window_minutes, workflow.info().run_id],
            # One read of a week of ChatTurn rows and one insert.
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=POLICY_RETRY,
        )
        return int(row_id)
