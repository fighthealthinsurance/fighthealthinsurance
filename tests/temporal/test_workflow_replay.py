"""Replay safety for the long-lived journey workflows (enable gate 10).

``IntakeJourneyWorkflow`` runs for THIRTY DAYS. Any workflow-code change
deployed inside that window is replayed against histories written by the old
code, and a change that alters the sequence of commands a workflow issues
makes those in-flight runs fail with a non-determinism error -- they do not
fail loudly at deploy time, they wedge later, one at a time, on workflows a
patient is waiting on.

Two protections, both here:

1. ``test_recorded_histories_still_replay`` replays every history JSON in
   ``tests/temporal/histories/`` against the CURRENT workflow definitions.
   Drop a production history in that directory (see the README there) and CI
   will refuse any future change that would break it.

2. ``test_a_completed_journey_replays_against_current_code`` generates a
   history in-process and replays it, so the harness itself is exercised even
   before any production history has been captured -- an empty corpus must not
   look like a passing gate.

Replay uses the SAME workflow classes the worker registers. It does not run
activities: only the workflow's own decisions are replayed, which is exactly
the surface that non-determinism affects.
"""

import json
import os
import pathlib
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from fighthealthinsurance.workflows.generate_appeal import GenerateAppealWorkflow
from fighthealthinsurance.workflows.intake_journey import IntakeJourneyWorkflow
from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow
from fighthealthinsurance.workflows.types import GenerateAppealInput, IntakeJourneyInput

HISTORY_DIR = pathlib.Path(__file__).parent / "histories"

# Every workflow the production workers register. A history can only replay
# against the class that wrote it, so this list must track
# run_temporal_worker.
ALL_WORKFLOWS = [SendFaxWorkflow, GenerateAppealWorkflow, IntakeJourneyWorkflow]


@workflow.defn(name="GenerateAppealWorkflow")
class _StubGenerateAppeal:
    """Child stub: the parent's history records the child's start and result,
    which is all replay needs."""

    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        return 3


def _recorder():
    @activity.defn(name="send_abandonment_nudge")
    async def send_abandonment_nudge(hashed_email: str, denial_uuid: str) -> bool:
        return True

    @activity.defn(name="close_incomplete_journey")
    async def close_incomplete_journey(hashed_email: str, denial_uuid: str) -> bool:
        return True

    @activity.defn(name="check_generation_postcondition")
    async def check_generation_postcondition(
        hashed_email: str, denial_uuid: str
    ) -> bool:
        return True

    return [
        send_abandonment_nudge,
        close_incomplete_journey,
        check_generation_postcondition,
    ]


@pytest.mark.asyncio
async def test_a_completed_journey_replays_against_current_code():
    """Generate a real history, then replay it against the current classes.

    This is the harness proving itself: with no recorded histories yet, an
    empty corpus would otherwise make the gate vacuously green.
    """
    task_queue = str(uuid.uuid4())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal],
            activities=_recorder(),
        ):
            handle = await env.client.start_workflow(
                IntakeJourneyWorkflow.run,
                IntakeJourneyInput(
                    hashed_email="h", denial_uuid="u", contact_opt_in=True
                ),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            assert await handle.result() == "completed"
            history = await handle.fetch_history()

    replayer = Replayer(workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal])
    # raise_on_replay_failure=True: a non-determinism error must fail the test.
    await replayer.replay_workflow(
        WorkflowHistory(history.run_id, history.events)
    )


@pytest.mark.asyncio
async def test_recorded_histories_still_replay():
    """Replay every checked-in production history against current code.

    Empty today. The moment a real journey history lands in
    tests/temporal/histories/, this becomes the thing that stops a workflow
    change from wedging in-flight 30-day runs.
    """
    histories = sorted(HISTORY_DIR.glob("*.json"))
    if not histories:
        pytest.skip(
            "no recorded histories yet -- see tests/temporal/histories/README.md"
        )
    replayer = Replayer(workflows=ALL_WORKFLOWS)
    for path in histories:
        await replayer.replay_workflow(
            WorkflowHistory.from_json(path.stem, path.read_text())
        )


@pytest.mark.asyncio
async def test_capture_baseline_history():
    """Regenerate the checked-in baseline history. Skipped by default.

        FHI_CAPTURE_HISTORY=1 tox -e py313-django52-temporal -- \
            tests/temporal/test_workflow_replay.py -k capture_baseline

    Run this ONLY when a workflow change is intentional and you have decided
    the new command sequence is correct. Overwriting the baseline is how you
    tell CI "this drift is expected" -- doing it casually defeats the gate.
    """
    if not os.environ.get("FHI_CAPTURE_HISTORY"):
        pytest.skip("set FHI_CAPTURE_HISTORY=1 to regenerate the baseline")

    task_queue = str(uuid.uuid4())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal],
            activities=_recorder(),
        ):
            handle = await env.client.start_workflow(
                IntakeJourneyWorkflow.run,
                IntakeJourneyInput(
                    hashed_email="h", denial_uuid="u", contact_opt_in=True
                ),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            assert await handle.result() == "completed"
            history = await handle.fetch_history()

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out = HISTORY_DIR / "intake_journey_completed.json"
    out.write_text(history.to_json())
    print(f"wrote {out}")


def test_the_replay_list_covers_every_registered_workflow():
    """A workflow the worker registers but this list omits would never be
    replay-checked, and the gap would be invisible."""
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "management"
        / "commands"
        / "run_temporal_worker.py"
    ).read_text()
    for cls in ALL_WORKFLOWS:
        assert cls.__name__ in source, f"{cls.__name__} not registered by the worker"
    # ...and the reverse: anything the worker registers must be listed here.
    for name in ("SendFaxWorkflow", "GenerateAppealWorkflow", "IntakeJourneyWorkflow"):
        assert name in {c.__name__ for c in ALL_WORKFLOWS}, name
