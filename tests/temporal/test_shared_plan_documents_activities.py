"""Tests for the shared plan documents activity wrappers.

``ActivityEnvironment`` runs them in-process with the work in
``plan_documents_processing`` patched out: what is passed through, and what
an error turns into. Errors carry the exception's class name and the row's
id only, in history and in the logs; schema errors and bad ids are not
retried. The work itself is tested against the database in tests/sync.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.db.utils import OperationalError, ProgrammingError
from loguru import logger
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import shared_plan_documents as activities

_CORE = "fighthealthinsurance.plan_documents_processing"
BATCH = "5b7d1f0e-6a43-4f8e-9c1d-2e3f4a5b6c7d"
ROW = "0c1d2e3f-4a5b-4c6d-8e9f-a0b1c2d3e4f5"
SECRET = "Member name: Rebecca Lee Crumpler"


@pytest.fixture(autouse=True)
def _no_database_round_trip():
    with patch(f"{activities.__name__}._aclose_old_connections", AsyncMock()):
        yield


@pytest.mark.asyncio
@patch(f"{_CORE}.abatch_document_ids", new_callable=AsyncMock, return_value=[ROW])
async def test_listing_passes_the_row_ids_through(mock_ids):
    result = await ActivityEnvironment().run(
        activities.list_shared_plan_documents, BATCH
    )
    assert result == [ROW]
    mock_ids.assert_awaited_once_with(BATCH)


@pytest.mark.asyncio
@patch(f"{_CORE}.aprocess_document", new_callable=AsyncMock, return_value="gone")
async def test_reading_a_row_that_is_gone_reports_gone(mock_process):
    result = await ActivityEnvironment().run(
        activities.process_shared_plan_document, ROW
    )
    assert result == "gone"
    mock_process.assert_awaited_once_with(ROW)


@pytest.mark.asyncio
@patch(f"{_CORE}.amark_unreadable", new_callable=AsyncMock, return_value="unreadable")
async def test_marking_passes_the_status_through(mock_mark):
    result = await ActivityEnvironment().run(
        activities.mark_shared_plan_document_unreadable, ROW
    )
    assert result == "unreadable"
    mock_mark.assert_awaited_once_with(ROW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity_fn",
    [
        activities.list_shared_plan_documents,
        activities.process_shared_plan_document,
        activities.mark_shared_plan_document_unreadable,
    ],
)
async def test_an_id_that_is_not_a_uuid_is_never_retried(activity_fn):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activity_fn, "Rebecca Lee Crumpler")
    assert raised.value.non_retryable
    assert "Crumpler" not in raised.value.message


@pytest.mark.asyncio
async def test_a_failed_reading_names_only_its_class_and_the_row():
    failing = AsyncMock(side_effect=RuntimeError(SECRET))
    with patch(f"{_CORE}.aprocess_document", failing):
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(
                activities.process_shared_plan_document, ROW
            )
    assert raised.value.message == f"RuntimeError for shared plan document {ROW}"
    assert not raised.value.non_retryable
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_failed_reading_logs_nothing_from_the_file():
    lines: list = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        with patch(
            f"{_CORE}.aprocess_document", AsyncMock(side_effect=RuntimeError(SECRET))
        ):
            with pytest.raises(ApplicationError):
                await ActivityEnvironment().run(
                    activities.process_shared_plan_document, ROW
                )
    finally:
        logger.remove(sink)
    assert any("RuntimeError" in line for line in lines)
    assert not any("Crumpler" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, retried",
    [(ProgrammingError(SECRET), False), (OperationalError(SECRET), True)],
)
async def test_schema_errors_are_not_retried_and_database_hiccups_are(error, retried):
    with patch(f"{_CORE}.abatch_document_ids", AsyncMock(side_effect=error)):
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(
                activities.list_shared_plan_documents, BATCH
            )
    assert raised.value.non_retryable is (not retried)
    assert raised.value.message == (
        f"{type(error).__name__} for share batch {BATCH}"
    )
