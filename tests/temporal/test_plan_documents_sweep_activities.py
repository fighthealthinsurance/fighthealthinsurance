"""Tests for the plan documents sweep's activity wrappers.

``ActivityEnvironment`` runs them in-process with the work patched out:
numbers are checked before anything runs, results pass through, and errors
carry the class name only. A send is never retried, whatever failed. The
real sends run against the database in tests/sync/test_plan_documents_reminder.py.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.db.utils import OperationalError, ProgrammingError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import plan_documents_sweep as activities

_REMINDERS = "fighthealthinsurance.plan_documents_reminder"
_PROCESSING = "fighthealthinsurance.plan_documents_processing"
SECRET = "rebecca.crumpler@example.com"


@pytest.fixture(autouse=True)
def _no_database_round_trip():
    with patch(f"{activities.__name__}._aclose_old_connections", AsyncMock()):
        yield


async def _run(activity_fn, *args):
    return await ActivityEnvironment().run(activity_fn, *args)


@pytest.mark.asyncio
@patch(f"{_REMINDERS}.adue_reminder_ids", new_callable=AsyncMock, return_value=[3, 4])
async def test_due_reminders_are_row_ids(mock_due):
    assert await _run(activities.find_due_plan_documents_reminders, 50) == [3, 4]
    mock_due.assert_awaited_once_with(50)


@pytest.mark.asyncio
@patch(f"{_REMINDERS}.asend_reminder", new_callable=AsyncMock, return_value=True)
async def test_a_send_takes_a_row_id_and_returns_a_boolean(mock_send):
    assert await _run(activities.send_plan_documents_reminder, 3) is True
    mock_send.assert_awaited_once_with(3)


@pytest.mark.asyncio
@patch(f"{_PROCESSING}.awaiting_batches", new_callable=AsyncMock, return_value=["b"])
async def test_waiting_shares_pass_through(mock_waiting):
    assert await _run(activities.find_waiting_share_batches, 15, 50) == ["b"]
    mock_waiting.assert_awaited_once_with(15, 50)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity_fn, args",
    [
        (activities.find_due_plan_documents_reminders, (0,)),
        (activities.find_due_plan_documents_reminders, (101,)),
        (activities.find_due_plan_documents_reminders, (True,)),
        (activities.send_plan_documents_reminder, ("3",)),
        (activities.send_plan_documents_reminder, (0,)),
        (activities.find_waiting_share_batches, (0, 50)),
        (activities.find_waiting_share_batches, (15, 501)),
    ],
)
async def test_bad_numbers_are_never_retried(activity_fn, args):
    with pytest.raises(ApplicationError) as raised:
        await _run(activity_fn, *args)
    assert raised.value.non_retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OperationalError(SECRET), RuntimeError(SECRET)])
async def test_a_failed_send_is_never_retried_and_names_only_its_class(error):
    with patch(f"{_REMINDERS}.asend_reminder", AsyncMock(side_effect=error)):
        with pytest.raises(ApplicationError) as raised:
            await _run(activities.send_plan_documents_reminder, 3)
    assert raised.value.non_retryable
    assert raised.value.message == f"{type(error).__name__} for reminder 3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, retried",
    [(ProgrammingError(SECRET), False), (OperationalError(SECRET), True)],
)
async def test_lookups_retry_database_hiccups_but_not_schema_errors(error, retried):
    with patch(f"{_REMINDERS}.adue_reminder_ids", AsyncMock(side_effect=error)):
        with pytest.raises(ApplicationError) as raised:
            await _run(activities.find_due_plan_documents_reminders, 50)
    assert raised.value.non_retryable is (not retried)
    assert SECRET not in raised.value.message
