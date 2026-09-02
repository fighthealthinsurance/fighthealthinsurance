"""Tests for the appeal-journey Temporal activity wrappers.

These verify the thin wrapper behavior -- load the denial, delegate to
``appeal_journey_core``, and the not-found fallbacks -- using
``ActivityEnvironment``, which runs in-process and needs no Temporal test
server. The wrapped business logic itself lives in ``appeal_journey_core``.
"""

import uuid
from unittest.mock import Mock, patch

import pytest

from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import appeal_journey as journey_activities
from fighthealthinsurance.appeal_journey_core import STATUS_NOT_FOUND, STATUS_OK


@patch("fighthealthinsurance.appeal_journey_core.load_denial", return_value=None)
def test_precheck_not_found_returns_status(mock_load):
    env = ActivityEnvironment()
    result = env.run(
        journey_activities.precheck_appeal_journey, "h", str(uuid.uuid4())
    )
    assert result == STATUS_NOT_FOUND


@patch(
    "fighthealthinsurance.appeal_journey_core.precheck_appeal_journey",
    return_value=STATUS_OK,
)
@patch("fighthealthinsurance.appeal_journey_core.load_denial")
def test_precheck_delegates_to_core(mock_load, mock_precheck):
    env = ActivityEnvironment()
    fake_denial = object()
    mock_load.return_value = fake_denial
    result = env.run(journey_activities.precheck_appeal_journey, "h", "u")
    mock_load.assert_called_once_with("h", "u")
    mock_precheck.assert_called_once_with(fake_denial)
    assert result == STATUS_OK


@patch("fighthealthinsurance.appeal_journey_core.load_denial", return_value=None)
def test_generate_not_found_stores_nothing(mock_load):
    env = ActivityEnvironment()
    assert env.run(journey_activities.generate_and_store_appeals, "h", "u") == 0


@patch(
    "fighthealthinsurance.appeal_journey_core.generate_and_store_appeals",
    return_value=2,
)
@patch("fighthealthinsurance.appeal_journey_core.load_denial")
def test_generate_delegates_to_core(mock_load, mock_generate):
    env = ActivityEnvironment()
    fake_denial = Mock(uuid="u")
    mock_load.return_value = fake_denial
    result = env.run(journey_activities.generate_and_store_appeals, "h", "u")
    mock_generate.assert_called_once_with(fake_denial)
    assert result == 2


@patch("fighthealthinsurance.appeal_journey_core.generate_and_store_appeals")
@patch("fighthealthinsurance.appeal_journey_core.load_denial")
def test_generate_sanitizes_exceptions(mock_load, mock_generate):
    """Raised errors must not leak denial/model text into workflow history."""
    from temporalio.exceptions import ApplicationError

    mock_load.return_value = Mock(uuid="u")
    mock_generate.side_effect = Exception("sensitive diagnosis text")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(journey_activities.generate_and_store_appeals, "h", "u")
    assert "sensitive" not in str(exc_info.value)
    assert "u" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
