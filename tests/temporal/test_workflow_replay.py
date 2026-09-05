"""Replay safety for the long-lived journey workflows (enable gate 10).

``IntakeJourneyWorkflow`` runs for THIRTY DAYS. Any workflow-code change
deployed inside that window is replayed against histories written by the old
code, and a change that alters the sequence of commands a workflow issues
makes those in-flight runs fail with a non-determinism error -- they do not
fail loudly at deploy time, they wedge later, one at a time, on workflows a
patient is waiting on.

Two protections:

1. ``test_recorded_histories_still_replay`` replays every history JSON in
   ``tests/temporal/histories/`` against the CURRENT workflow definitions.
   The committed baseline is what makes this a real gate: it is a FIXED
   history, so a future workflow change is checked against yesterday's
   command sequence rather than against itself.

2. ``test_a_completed_journey_replays_against_current_code`` generates a
   history in-process and replays it. This exercises the harness, but note
   what it cannot do: both sides move together, so it can never catch
   determinism drift on its own. The committed baseline does that.

Replay does not run activities: only the workflow's own decisions are
replayed, which is exactly the surface non-determinism affects.
"""

import asyncio
import json
import os
import pathlib
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from fighthealthinsurance.workflows import registry as workflow_registry
from fighthealthinsurance.workflows.generate_appeal import GenerateAppealWorkflow
from fighthealthinsurance.workflows.intake_journey import IntakeJourneyWorkflow
from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow
from fighthealthinsurance.workflows.types import (
    GenerateAppealInput,
    IntakeJourneyInput,
    SendFaxInput,
)

HISTORY_DIR = pathlib.Path(__file__).parent / "histories"

# Derived from the SAME registry run_temporal_worker uses, not a hand-kept
# copy: a workflow enabled in production without a recorded history must fail
# this gate rather than pass it invisibly (external review).
ALL_WORKFLOWS = workflow_registry.all_enabled_workflows()


@workflow.defn(name="GenerateAppealWorkflow")
class _StubGenerateAppeal:
    """Child stub: the parent's history records the child's start and result,
    which is all replay of the PARENT needs."""

    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        return 3


def _stub_activities():
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


async def _run_a_completed_journey(env, task_queue):
    handle = await env.client.start_workflow(
        IntakeJourneyWorkflow.run,
        IntakeJourneyInput(hashed_email="h", denial_uuid="u", contact_opt_in=True),
        id=str(uuid.uuid4()),
        task_queue=task_queue,
    )
    await handle.signal(IntakeJourneyWorkflow.form_completed)
    assert await handle.result() == "completed"
    return await handle.fetch_history()


def _history_paths() -> dict:
    """Recorded histories keyed by the workflow type they replay."""
    found = {}
    for path in sorted(HISTORY_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        started = raw["events"][0]["workflowExecutionStartedEventAttributes"]
        found.setdefault(started["workflowType"]["name"], []).append(path)
    return found


def test_every_enabled_workflow_has_a_replay_history():
    """The CORPUS decides what is actually replayed, not the class list.

    Replayer(workflows=...) only makes classes available; the workflow type is
    selected from each history. So a corpus holding only IntakeJourneyWorkflow
    lets changes to GenerateAppealWorkflow or SendFaxWorkflow pass this gate
    untouched (external review). Require one history per enabled workflow, and
    take the list from the production registry so a NEWLY enabled workflow
    fails here instead of slipping through.
    """
    recorded = _history_paths()
    missing = [w.__name__ for w in ALL_WORKFLOWS if w.__name__ not in recorded]
    assert not missing, (
        f"no replay history for {missing}. Every workflow the worker can "
        f"register needs one, or a change to it is unguarded. See "
        f"tests/temporal/histories/README.md"
    )


@pytest.mark.asyncio
async def test_recorded_histories_still_replay():
    """Replay every checked-in history against the current workflow code.

    This is the gate. The baselines are committed, so a workflow change that
    alters a command sequence fails HERE rather than wedging a live run.
    """
    histories = sorted(HISTORY_DIR.glob("*.json"))
    assert histories, (
        "no recorded histories -- an empty corpus would make this gate "
        "vacuously green; see tests/temporal/histories/README.md"
    )
    # The real classes only: a stub sharing a workflow name would collide, and
    # each history selects its own type from this set.
    replayer = Replayer(workflows=ALL_WORKFLOWS)
    for path in histories:
        await replayer.replay_workflow(
            WorkflowHistory.from_json(path.stem, path.read_text())
        )


@pytest.mark.asyncio
async def test_a_completed_journey_replays_against_current_code():
    """Exercises the harness end to end. Cannot catch drift by itself --
    both the history and the code move together -- which is why the
    committed baseline above exists."""
    task_queue = str(uuid.uuid4())
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal],
            activities=_stub_activities(),
        ):
            history = await _run_a_completed_journey(env, task_queue)

    replayer = Replayer(workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal])
    await replayer.replay_workflow(WorkflowHistory(history.run_id, history.events))


def _fax_activities():
    @activity.defn(name="precheck_fax")
    async def precheck_fax(hashed_email: str, fax_uuid: str) -> str:
        return "ok"

    @activity.defn(name="send_fax_via_vendor")
    async def send_fax_via_vendor(hashed_email: str, fax_uuid: str) -> str:
        return "sent"

    @activity.defn(name="finalize_fax")
    async def finalize_fax(
        hashed_email: str,
        fax_uuid: str,
        fax_success: bool,
        missing_destination: bool,
    ) -> bool:
        return True

    @activity.defn(name="release_send_claim")
    async def release_send_claim(hashed_email: str, fax_uuid: str) -> bool:
        return True

    return [precheck_fax, send_fax_via_vendor, finalize_fax, release_send_claim]


def _appeal_activities():
    @activity.defn(name="precheck_appeal_journey")
    async def precheck_appeal_journey(hashed_email: str, denial_uuid: str) -> str:
        return "ok"

    @activity.defn(name="generate_and_store_appeals")
    async def generate_and_store_appeals(hashed_email: str, denial_uuid: str) -> int:
        return 3

    return [precheck_appeal_journey, generate_and_store_appeals]


async def _capture(
    env, task_queue, workflows, activities, entry, arg, signal_completion=True
):
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
    ):
        handle = await env.client.start_workflow(
            entry, arg, id=str(uuid.uuid4()), task_queue=task_queue
        )
        if entry is IntakeJourneyWorkflow.run and signal_completion:
            await handle.signal(IntakeJourneyWorkflow.form_completed)
        # These workflows use an unbounded durable retry policy, so a stub
        # whose signature does not match the real activity retries forever
        # rather than failing. Bound it: a mis-shaped stub should surface as a
        # timeout in seconds, not a run that never ends.
        await asyncio.wait_for(handle.result(), timeout=60)
        return await handle.fetch_history()


@pytest.mark.asyncio
async def test_capture_baseline_histories():
    """Regenerate the checked-in baselines. Skipped by default.

        FHI_CAPTURE_HISTORY=1 tox -e py313-django52-temporal -- \
            tests/temporal/test_workflow_replay.py -k capture_baseline

    Run this ONLY when a workflow change is intentional and the new command
    sequence has been reviewed. Overwriting a baseline is how you tell CI
    "this drift is expected" -- doing it reflexively defeats the gate.
    """
    # Exact match, not truthiness: os.environ.get() is truthy for "0" and
    # "false", so a stray value would silently REWRITE the baselines -- which
    # is how a gate turns green without anyone deciding it should (external
    # review). Keep this variable unset in CI.
    if os.environ.get("FHI_CAPTURE_HISTORY") != "1":
        pytest.skip("set FHI_CAPTURE_HISTORY=1 to regenerate the baselines")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        plans = [
            (
                "send_fax_completed",
                [SendFaxWorkflow],
                _fax_activities(),
                SendFaxWorkflow.run,
                SendFaxInput(hashed_email="h", fax_uuid="f", delay_send=False),
            ),
            (
                "generate_appeal_completed",
                [GenerateAppealWorkflow],
                _appeal_activities(),
                GenerateAppealWorkflow.run,
                GenerateAppealInput(hashed_email="h", denial_uuid="u"),
            ),
            (
                "send_fax_delayed",
                [SendFaxWorkflow],
                _fax_activities(),
                SendFaxWorkflow.run,
                SendFaxInput(hashed_email="h", fax_uuid="f", delay_send=True),
            ),
            (
                "intake_journey_abandoned",
                [IntakeJourneyWorkflow, _StubGenerateAppeal],
                _stub_activities(),
                IntakeJourneyWorkflow.run,
                IntakeJourneyInput(
                    hashed_email="h", denial_uuid="u", contact_opt_in=True
                ),
            ),
            (
                "intake_journey_completed",
                [IntakeJourneyWorkflow, _StubGenerateAppeal],
                _stub_activities(),
                IntakeJourneyWorkflow.run,
                IntakeJourneyInput(
                    hashed_email="h", denial_uuid="u", contact_opt_in=True
                ),
            ),
        ]
        for name, workflows, activities, entry, arg in plans:
            # The abandoned journey must NOT be signalled: it runs the nudge
            # and close timers out, which is the branch the 30-day lifetime
            # actually exercises and the one the completion history misses.
            history = await _capture(
                env,
                str(uuid.uuid4()),
                workflows,
                activities,
                entry,
                arg,
                signal_completion=(name != "intake_journey_abandoned"),
            )
            out = HISTORY_DIR / f"{name}.json"
            out.write_text(history.to_json())
            print(f"wrote {out}")


def test_the_worker_hands_the_registry_result_to_the_worker_unmodified():
    """The registry must be what production registers, not merely consulted.

    A source check for "calls the registry" is not enough: appending to the
    returned list would register an unbaselined workflow while every string
    assertion still passed (external review). So also require that neither
    list is mutated between the registry call and Worker(workflows=...).

    Still structural rather than behavioural -- constructing a real Worker
    needs a live client -- so it closes the named hole without pretending to
    be a full guarantee.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "management"
        / "commands"
        / "run_temporal_worker.py"
    ).read_text()
    assert "workflow_registry.fax_workflows()" in source
    assert "workflow_registry.appeal_workflows(" in source
    # No hand-rolled list left behind.
    assert "fax_workflows: List[type] = [SendFaxWorkflow]" not in source
    # ...and nothing added to either list after the registry produced it.
    for name in ("fax_workflows", "appeal_workflows"):
        assert f"{name}.append(" not in source, f"{name} is mutated after the registry"
        assert f"{name} +=" not in source, f"{name} is extended after the registry"
        assert f"workflows={name}" in source, f"{name} is not what Worker receives"
