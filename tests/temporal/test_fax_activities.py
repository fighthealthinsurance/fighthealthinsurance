"""Tests for the fax Temporal activity wrappers.

These verify the thin wrapper behavior -- load the fax, delegate to
``fax_send_core``, and the not-found fallbacks -- using ``ActivityEnvironment``,
which runs in-process and needs no Temporal test server. The wrapped business
logic itself lives in ``fax_send_core``.

The activities are synchronous, so ``ActivityEnvironment.run`` returns their
result directly (no ``await``) and these are plain sync tests.
"""

import uuid
from unittest.mock import Mock, patch

import pytest

from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import fax as fax_activities
from fighthealthinsurance.fax_status import STATUS_NOT_FOUND, STATUS_OK


@patch("fighthealthinsurance.fax_send_core.load_fax", return_value=None)
def test_precheck_fax_not_found_returns_status(mock_load):
    env = ActivityEnvironment()
    result = env.run(fax_activities.precheck_fax, "h", str(uuid.uuid4()))
    assert result == STATUS_NOT_FOUND


@patch("fighthealthinsurance.fax_send_core.precheck_fax", return_value=STATUS_OK)
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_precheck_fax_delegates_to_core(mock_load, mock_precheck):
    env = ActivityEnvironment()
    fake_fax = object()
    mock_load.return_value = fake_fax
    result = env.run(fax_activities.precheck_fax, "h", "u")
    mock_load.assert_called_once_with("h", "u")
    mock_precheck.assert_called_once_with(fake_fax)
    assert result == STATUS_OK


@patch("fighthealthinsurance.fax_send_core.load_fax", return_value=None)
def test_send_fax_via_vendor_not_found_returns_false(mock_load):
    env = ActivityEnvironment()
    result = env.run(fax_activities.send_fax_via_vendor, "h", "u")
    assert result is False


@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_send_fax_via_vendor_skips_when_already_completed(mock_load):
    """Idempotency guard: an already-handed-off fax is not re-sent on retry."""
    mock_load.return_value = Mock(vendor_send_completed=True, uuid="u")
    env = ActivityEnvironment()
    result = env.run(fax_activities.send_fax_via_vendor, "h", "u")
    assert result is True


@patch("fighthealthinsurance.fax_send_core.send_fax_via_vendor")
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_send_fax_via_vendor_sanitizes_exceptions(mock_load, mock_send):
    """Raised errors must not leak vendor/document text into workflow history."""
    from temporalio.exceptions import ApplicationError

    mock_load.return_value = Mock(vendor_send_completed=False, uuid="u")
    mock_send.side_effect = Exception("sensitive patient details /tmp/doc.pdf")
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(fax_activities.send_fax_via_vendor, "h", "u")
    assert "sensitive" not in str(exc_info.value)
    assert "u" in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@patch("fighthealthinsurance.fax_send_core.finalize_fax", return_value=True)
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_finalize_fax_delegates_to_core(mock_load, mock_finalize):
    env = ActivityEnvironment()
    fake_fax = object()
    mock_load.return_value = fake_fax
    result = env.run(fax_activities.finalize_fax, "h", "u", True, False)
    mock_finalize.assert_called_once_with(fake_fax, True, False)
    assert result is True
