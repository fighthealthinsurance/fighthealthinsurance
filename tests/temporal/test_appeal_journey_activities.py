"""Tests for the appeal-journey Temporal activity wrappers.

These verify the thin wrapper behavior -- load the denial, delegate to the
async core, and the not-found fallbacks -- using ``ActivityEnvironment``,
which runs in-process and needs no Temporal test server. The activities are
asyncio activities, so ``ActivityEnvironment.run`` is awaited. The wrapped
business logic itself lives in ``appeal_journey_core``.
"""

import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import appeal_journey as journey_activities
from fighthealthinsurance.appeal_journey_core import (
    STATUS_NOT_FOUND,
    STATUS_OK,
    JourneyIncomplete,
)

_MOD = "fighthealthinsurance.activities.appeal_journey"


@pytest.mark.asyncio
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock, return_value=None)
async def test_precheck_not_found_returns_status(mock_load):
    env = ActivityEnvironment()
    result = await env.run(
        journey_activities.precheck_appeal_journey, "h", str(uuid.uuid4())
    )
    assert result == STATUS_NOT_FOUND


@pytest.mark.asyncio
@patch(f"{_MOD}.aprecheck_appeal_journey", new_callable=AsyncMock, return_value=STATUS_OK)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_precheck_delegates_to_core(mock_load, mock_precheck):
    env = ActivityEnvironment()
    fake_denial = object()
    mock_load.return_value = fake_denial
    result = await env.run(journey_activities.precheck_appeal_journey, "h", "u")
    mock_load.assert_awaited_once_with("h", "u")
    mock_precheck.assert_awaited_once_with(fake_denial)
    assert result == STATUS_OK


@pytest.mark.asyncio
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock, return_value=None)
async def test_generate_not_found_stores_nothing(mock_load):
    env = ActivityEnvironment()
    assert await env.run(journey_activities.generate_and_store_appeals, "h", "u") == 0


@pytest.mark.asyncio
@patch(f"{_MOD}.agenerate_and_store_appeals", new_callable=AsyncMock, return_value=2)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_generate_delegates_to_core(mock_load, mock_generate):
    env = ActivityEnvironment()
    fake_denial = Mock(uuid="u")
    mock_load.return_value = fake_denial
    result = await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    mock_generate.assert_awaited_once_with(fake_denial)
    assert result == 2


@pytest.mark.asyncio
@patch(f"{_MOD}.agenerate_and_store_appeals", new_callable=AsyncMock)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_journey_incomplete_is_a_retryable_application_error(
    mock_load, mock_generate
):
    """A run that falls short of the durable target must surface as a
    retryable ApplicationError carrying only counts and the opaque uuid."""
    mock_load.return_value = Mock(uuid="u")
    mock_generate.side_effect = JourneyIncomplete("1 of 3 drafts durable for denial u")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert "1 of 3" in str(exc_info.value)
    assert not exc_info.value.non_retryable


@pytest.mark.asyncio
@patch(f"{_MOD}.agenerate_and_store_appeals", new_callable=AsyncMock)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_generate_sanitizes_exceptions(mock_load, mock_generate):
    """Raised errors must not leak denial/model text into workflow history."""
    mock_load.return_value = Mock(uuid="u")
    mock_generate.side_effect = Exception("sensitive diagnosis text")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert "sensitive" not in str(exc_info.value)
    assert "u" in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
@patch(f"{_MOD}.aprecheck_appeal_journey", new_callable=AsyncMock)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_precheck_programming_error_is_non_retryable(mock_load, mock_precheck):
    """The precheck's retry policy is unbounded, so a schema/programming
    failure must be classified non-retryable instead of spinning forever
    disguised as a transient error."""
    from django.db.utils import ProgrammingError

    mock_load.return_value = object()
    mock_precheck.side_effect = ProgrammingError('column "nope" does not exist')
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.precheck_appeal_journey, "h", "u")
    assert exc_info.value.non_retryable
    # Sanitized: the failure names the error type and opaque uuid only.
    assert "nope" not in str(exc_info.value)


@pytest.mark.asyncio
@patch(f"{_MOD}.agenerate_and_store_appeals", new_callable=AsyncMock)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_generate_validation_error_is_non_retryable(mock_load, mock_generate):
    from django.core.exceptions import ValidationError

    mock_load.return_value = object()
    mock_generate.side_effect = ValidationError("bad value")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert exc_info.value.non_retryable


@pytest.mark.asyncio
@patch(f"{_MOD}.agenerate_and_store_appeals", new_callable=AsyncMock)
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_generate_operational_error_stays_retryable(mock_load, mock_generate):
    """A dropped/refused database connection is exactly the transient class
    the retry policy exists for -- it must NOT be marked non-retryable."""
    from django.db.utils import OperationalError

    mock_load.return_value = object()
    mock_generate.side_effect = OperationalError("server closed the connection")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert not exc_info.value.non_retryable


@pytest.mark.asyncio
@patch(f"{_MOD}.aload_denial", new_callable=AsyncMock)
async def test_generate_denial_lookup_schema_error_is_non_retryable(mock_load):
    """The denial LOOKUP runs before the inner classifier; a schema failure
    there must get the same non-retryable classification (PR review)."""
    from django.db.utils import ProgrammingError

    mock_load.side_effect = ProgrammingError('relation "nope" does not exist')
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert exc_info.value.non_retryable
    assert "nope" not in str(exc_info.value)
