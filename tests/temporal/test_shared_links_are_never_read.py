"""A share's links are never read or fetched by its reading or the sweep.

The real SharedPlanDocumentsWorkflow and PlanDocumentsSweepWorkflow run
here with their real activities, against the database, on the Temporal test
server. Every HTTP client and outgoing socket in Python is patched to fail
if called. A link row has no file and is marked a link: the reading lists
only the share's files, and the sweep never starts a reading for a share
that holds only links.

Each test keeps its database work on a thread of its own
(ThreadSensitiveContext), so no connection outlives it on the shared thread
that the database-free tests here run on.

Requires the Temporal test server, which ``temporalio`` downloads on first run.
"""

import datetime
import functools
import uuid
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance import temporal_client
from fighthealthinsurance.activities import plan_documents_sweep as sweep_activities
from fighthealthinsurance.activities import shared_plan_documents as reading_activities
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.shared_plan_documents import save_shared_documents
from fighthealthinsurance.workflows.plan_documents_sweep import (
    PlanDocumentsSweepWorkflow,
)
from fighthealthinsurance.workflows.shared_plan_documents import (
    SharedPlanDocumentsWorkflow,
    shared_plan_documents_workflow_id,
)
from fighthealthinsurance.workflows.types import (
    PlanDocumentsSweepInput,
    SharedPlanDocumentsInput,
)

LINKS = [
    "https://benefits.crumpler-clinic.example.org/2026/plan-documents",
    "http://www.crumplermutual.example.com/plans/gold-ppo",
]
SUMMARY = b"Summary of Benefits, Crumpler Mutual Gold PPO. Overall deductible: $1,500"
ACTIVITIES = [
    reading_activities.list_shared_plan_documents,
    reading_activities.process_shared_plan_document,
    reading_activities.mark_shared_plan_document_unreadable,
    sweep_activities.find_due_plan_documents_reminders,
    sweep_activities.send_plan_documents_reminder,
    sweep_activities.find_waiting_share_batches,
]


@contextmanager
def _no_network(calls):
    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("something tried to reach the network")

    async def arefuse(*args, **kwargs):
        refuse(*args, **kwargs)

    with ExitStack() as stack:
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
            "urllib.request.urlopen",
            "requests.sessions.Session.request",
            "httpx.Client.send",
        ):
            stack.enter_context(patch(target, refuse))
        for target in ("httpx.AsyncClient.send", "aiohttp.ClientSession._request"):
            stack.enter_context(patch(target, arefuse))
        yield calls


async def _share(with_file: bool) -> list[PolicyDocument]:
    files = [SimpleUploadedFile("sbc.txt", SUMMARY)] if with_file else []
    with patch.object(temporal_client, "dispatch_shared_plan_documents"):
        rows = await sync_to_async(save_shared_documents)(
            files,
            source=PolicyDocument.SOURCE_SHARED,
            details={"insurer_name": "Crumpler Health Mutual"},
            hashed_email=None,
            links=LINKS,
        )
    # Saved two hours ago, so the sweep would pick up anything still waiting.
    await PolicyDocument.objects.filter(pk__in=[row.pk for row in rows]).aupdate(
        shared_at=timezone.now() - datetime.timedelta(hours=2)
    )
    return rows


async def _row(row: PolicyDocument) -> PolicyDocument:
    return await PolicyDocument.objects.aget(pk=row.pk)


async def _assert_links_untouched(rows: list[PolicyDocument]) -> None:
    for row in rows:
        if row.source != PolicyDocument.SOURCE_SHARED_LINK:
            continue
        now = await _row(row)
        assert now.processing_status == PolicyDocument.PROCESSING_LINK
        assert now.shared_link == row.shared_link
        assert now.processed_at is None
        assert now.cleaned_text == ""


async def _delete_stored_files() -> None:
    async for doc in PolicyDocument.objects.all():
        if doc.document_enc:
            await sync_to_async(doc.document_enc.delete)(save=False)


def _own_db_thread(test):
    """Run ``test`` with its database work on a thread that ends with it."""

    @functools.wraps(test)
    async def run():
        async with ThreadSensitiveContext():
            try:
                await test()
            finally:
                await _delete_stored_files()

    return run


async def _run(workflow_cls, request, workflow_id, calls, also_wait_for=None):
    """Run ``workflow_cls`` with the real activities while nothing may reach
    the network; with ``also_wait_for``, wait for that workflow id too."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = str(uuid.uuid4())
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanDocumentsSweepWorkflow, SharedPlanDocumentsWorkflow],
            activities=ACTIVITIES,
        ):
            with _no_network(calls):
                result = await env.client.execute_workflow(
                    workflow_cls.run, request, id=workflow_id, task_queue=task_queue
                )
                other = None
                if also_wait_for:
                    other = await env.client.get_workflow_handle(
                        also_wait_for
                    ).result()
    return result, other


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@_own_db_thread
async def test_the_reading_of_a_share_reads_its_file_and_never_its_links():
    rows = await _share(with_file=True)
    batch = str(rows[0].share_batch)
    calls: list = []
    result, _ = await _run(
        SharedPlanDocumentsWorkflow,
        SharedPlanDocumentsInput(share_batch=batch),
        shared_plan_documents_workflow_id(batch),
        calls,
    )
    assert result == {PolicyDocument.PROCESSING_READY: 1}
    assert calls == []
    assert (await _row(rows[0])).processing_status == PolicyDocument.PROCESSING_READY
    await _assert_links_untouched(rows)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@_own_db_thread
async def test_the_sweep_starts_no_reading_for_a_share_of_links():
    rows = await _share(with_file=False)
    # Even set back to waiting, a link is not a file the sweep reads.
    await PolicyDocument.objects.filter(pk=rows[0].pk).aupdate(
        processing_status=PolicyDocument.PROCESSING_RECEIVED
    )
    calls: list = []
    result, _ = await _run(
        PlanDocumentsSweepWorkflow,
        PlanDocumentsSweepInput(),
        str(uuid.uuid4()),
        calls,
    )
    assert result == {"reminders_sent": 0, "readings_started": 0}
    assert calls == []
    await _assert_links_untouched(rows[1:])
    first = await _row(rows[0])
    assert (first.processed_at, first.cleaned_text) == (None, "")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@_own_db_thread
async def test_the_sweep_reads_a_waiting_file_and_leaves_its_links_alone():
    rows = await _share(with_file=True)
    batch = str(rows[0].share_batch)
    calls: list = []
    result, reading = await _run(
        PlanDocumentsSweepWorkflow,
        PlanDocumentsSweepInput(),
        str(uuid.uuid4()),
        calls,
        also_wait_for=shared_plan_documents_workflow_id(batch),
    )
    assert result == {"reminders_sent": 0, "readings_started": 1}
    assert reading == {PolicyDocument.PROCESSING_READY: 1}
    assert calls == []
    await _assert_links_untouched(rows)
