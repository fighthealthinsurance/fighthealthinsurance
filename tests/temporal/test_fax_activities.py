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


@patch("fighthealthinsurance.fax_send_core.load_fax", return_value=None)
def test_finalize_fax_returns_false_when_fax_not_found(mock_load):
    env = ActivityEnvironment()
    result = env.run(fax_activities.finalize_fax, "h", "u", True, False)
    assert result is False


@patch("fighthealthinsurance.fax_send_core.finalize_fax", return_value=True)
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_finalize_fax_delegates_to_core(mock_load, mock_finalize):
    env = ActivityEnvironment()
    fake_fax = object()
    mock_load.return_value = fake_fax
    result = env.run(fax_activities.finalize_fax, "h", "u", True, False)
    mock_finalize.assert_called_once_with(fake_fax, True, False)
    assert result is True


@patch("fighthealthinsurance.fax_send_core.send_fax_via_vendor")
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_send_fax_via_vendor_heartbeats_while_sending(mock_load, mock_send):
    """The activity heartbeats while the blocking vendor call runs, so a dead
    worker is detected by heartbeat timeout instead of start-to-close."""
    import time

    from fighthealthinsurance.activities import fax as fax_module

    mock_load.return_value = Mock(vendor_send_completed=False, uuid="u")

    def slow_send(fax):
        time.sleep(0.15)
        return True

    mock_send.side_effect = slow_send
    beats: list = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)
    with patch.object(fax_module, "HEARTBEAT_INTERVAL_S", 0.02):
        result = env.run(fax_activities.send_fax_via_vendor, "h", "u")
    assert result is True
    assert len(beats) >= 2


@patch("fighthealthinsurance.fax_send_core.load_fax", return_value=None)
def test_release_send_claim_not_found_returns_false(mock_load):
    env = ActivityEnvironment()
    assert env.run(fax_activities.release_send_claim, "h", "u") is False


@patch("fighthealthinsurance.fax_send_core.release_send_claim")
@patch("fighthealthinsurance.fax_send_core.load_fax")
def test_release_send_claim_delegates_to_core(mock_load, mock_release):
    fake_fax = object()
    mock_load.return_value = fake_fax
    env = ActivityEnvironment()
    assert env.run(fax_activities.release_send_claim, "h", "u") is True
    mock_release.assert_called_once_with(fake_fax)


def test_claim_is_taken_only_after_document_assembly():
    """Document assembly is the memory-hungry phase (it OOM-killed the worker
    2026-08-30); a crash there must not leave the vendor-send claim stuck."""
    from unittest.mock import MagicMock

    from fighthealthinsurance import fax_send_core

    fax = Mock()
    fax.vendor_send_completed = False
    fax.destination = "000"
    fax.uuid = "u"
    fax.get_temporary_document_path.side_effect = RuntimeError("simulated OOM")
    with patch("fighthealthinsurance.models.FaxesToSend", MagicMock()) as mock_model:
        with pytest.raises(RuntimeError):
            fax_send_core.send_fax_via_vendor(fax)
        mock_model.objects.filter.assert_not_called()
