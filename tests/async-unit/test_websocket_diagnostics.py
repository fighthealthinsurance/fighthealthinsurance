"""Unit tests for fighthealthinsurance.websockets.log_zero_appeal_diagnostics.

The diagnostic helper distinguishes three failure modes when an appeal
session ends with 0 appeals delivered:

1. persisted_count > 0:  server has rows -> delivery/wire failure
2. persisted_count == 0: server has no rows -> generation failure
3. persisted_count == -1: lookup never ran or failed -> unknown

Each branch produces a different log message so triage can be pointed
at the right thing. These tests pin the behavior so future refactors
don't accidentally collapse the lookup-failed case back into the
"generation produced nothing" bucket.
"""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from fighthealthinsurance.websockets import log_zero_appeal_diagnostics


def _make_count_mock(return_value=None, side_effect=None):
    """Build a chained mock for ProposedAppeal.objects.filter(...).acount()."""
    queryset = MagicMock()
    if side_effect is not None:
        queryset.acount = AsyncMock(side_effect=side_effect)
    else:
        queryset.acount = AsyncMock(return_value=return_value)
    objects = MagicMock()
    objects.filter = MagicMock(return_value=queryset)
    return objects


def _patch_models(persisted_count_mock, denial_first_mock=None):
    """Patch ProposedAppeal and Denial managers used by the helper."""
    if denial_first_mock is None:
        denial_first_mock = AsyncMock(return_value=None)
    denial_qs = MagicMock()
    denial_qs.afirst = denial_first_mock
    denial_objects = MagicMock()
    denial_objects.filter = MagicMock(return_value=denial_qs)
    return (
        patch(
            "fighthealthinsurance.websockets.ProposedAppeal.objects",
            persisted_count_mock,
        ),
        patch(
            "fighthealthinsurance.websockets.Denial.objects",
            denial_objects,
        ),
    )


def _captured_logger():
    """Return a context-manager + recorder for the loguru logger used in
    the websockets module. Patches `logger.error` to a MagicMock so we
    can inspect the formatted message; loguru otherwise bypasses
    Python's logging module so caplog doesn't see it.
    """
    captured: list = []

    def _record(msg, *args, **kwargs):
        captured.append(msg)

    cm = patch("fighthealthinsurance.websockets.logger.error", side_effect=_record)
    return cm, captured


@pytest.mark.asyncio
async def test_persisted_gt_zero_logs_delivery_failure():
    """3 persisted rows, 0 delivered -> delivery/wire failure log."""
    objects = _make_count_mock(return_value=3)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=5,
            last_status_phase="generating",
            transport="websocket",
        )

    assert len(captured) == 1
    msg = captured[0]
    assert "delivery/wire failure" in msg
    assert "3 ProposedAppeal" in msg
    assert "[websocket]" in msg


@pytest.mark.asyncio
async def test_persisted_zero_logs_generation_failure():
    """0 persisted rows -> generation produced nothing log."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=99,
            status_count=2,
            last_status_phase="init",
            transport="rest",
        )

    assert len(captured) == 1
    msg = captured[0]
    assert "Generation produced nothing" in msg
    assert "[rest]" in msg


@pytest.mark.asyncio
async def test_lookup_unavailable_when_db_raises():
    """DB failure during lookup must NOT be classified as 'generation
    produced nothing'. The unknown branch (-1) emits its own message."""
    objects = _make_count_mock(side_effect=RuntimeError("db down"))
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=1,
            last_status_phase="research",
            transport="websocket",
        )

    msg = captured[-1]  # the diagnostic log; an earlier warning may
    # come from the lookup_error handler but it goes through
    # logger.opt(...).warning(), not logger.error()
    assert "lookup unavailable" in msg
    # And critically, NOT misclassified as a generation failure
    assert "Generation produced nothing" not in msg


@pytest.mark.asyncio
async def test_invalid_string_denial_id_skips_db_call_and_logs_unavailable():
    """A denial_id that can't be coerced to int must skip the DB call
    entirely and emit the lookup-unavailable log."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id="not-an-int",
            status_count=0,
            last_status_phase=None,
            transport="rest",
        )

    assert "lookup unavailable" in captured[-1]
    # acount() must not have been awaited — we never had a valid id
    objects.filter.return_value.acount.assert_not_called()


@pytest.mark.asyncio
async def test_none_denial_id_skips_db_call():
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, _captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=None,
            status_count=0,
            last_status_phase=None,
            transport="websocket",
        )
    objects.filter.return_value.acount.assert_not_called()


@pytest.mark.asyncio
async def test_int_denial_id_passes_through_to_filter():
    """Integer denial_id must reach the queryset filter as an int."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, _captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=0,
            last_status_phase=None,
            transport="websocket",
        )
    objects.filter.assert_called_once_with(for_denial__denial_id=42)


@pytest.mark.asyncio
async def test_str_denial_id_is_coerced_to_int():
    """Numeric string from JSON payload must be coerced to int before
    the queryset lookup so Django's int field accepts it."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, _captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id="42",
            status_count=0,
            last_status_phase=None,
            transport="websocket",
        )
    objects.filter.assert_called_once_with(for_denial__denial_id=42)


@pytest.mark.asyncio
async def test_stream_error_appended_to_log():
    """When stream_error is set, it must appear in the log so the
    triggering exception is visible alongside the diagnostic."""
    objects = _make_count_mock(return_value=2)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=1,
            last_status_phase="generating",
            transport="rest",
            stream_error="connection reset",
        )

    assert "stream_error=connection reset" in captured[-1]
