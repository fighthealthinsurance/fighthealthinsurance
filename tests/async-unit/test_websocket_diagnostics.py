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

import asyncio
import json
import re
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from django.db.utils import OperationalError

from fighthealthinsurance.websockets import (
    _AppealGenTraceFields,
    _stream_error_is_client_disconnect,
    log_zero_appeal_diagnostics,
)


def _captured_warning():
    """Capture logger.warning messages (the client-disconnect branch logs at
    WARNING, not ERROR)."""
    captured: list = []

    def _record(msg, *args, **kwargs):
        captured.append(msg)

    cm = patch("fighthealthinsurance.websockets.logger.warning", side_effect=_record)
    return cm, captured


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
    """Integer denial_id must reach the queryset filter as an int.

    speculative=False excludes the held-back background precompute so the
    diagnostic counts only rows this session could have produced/served.
    """
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
    objects.filter.assert_called_once_with(for_denial_id=42, speculative=False)


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
    objects.filter.assert_called_once_with(for_denial_id=42, speculative=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("denial_id", [True, False])
async def test_bool_denial_id_does_not_coerce_to_int(denial_id):
    """`isinstance(True, int)` is True in Python -- without the bool
    guard, a JSON `true`/`false` would coerce to 1/0 and point
    diagnostics at the wrong denial record. Lock the bool exclusion in."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=denial_id,
            status_count=0,
            last_status_phase=None,
            transport="websocket",
        )
    # No DB lookup attempted, and the log message indicates lookup
    # unavailable -- not "Generation produced nothing".
    objects.filter.return_value.acount.assert_not_called()
    assert "lookup unavailable" in captured[-1]


@pytest.mark.asyncio
async def test_stream_error_appended_to_log():
    """When stream_error is set, it must appear in the log so the
    triggering exception is visible alongside the diagnostic.

    Uses a SERVER-side error: a client-disconnect marker would (correctly) be
    routed to the WARNING branch instead of this delivery-failure ERROR.
    """
    objects = _make_count_mock(return_value=2)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=1,
            last_status_phase="generating",
            transport="rest",
            stream_error="Server error while generating appeals.",
        )

    assert "stream_error=Server error while generating appeals." in captured[-1]


@pytest.mark.asyncio
async def test_client_disconnect_with_persisted_rows_is_a_warning_not_an_error():
    """A client that hangs up AFTER drafts were persisted (the common iOS case)
    must not be reported as a delivery/wire failure ERROR -- that is the alert
    noise the disconnect classification exists to remove. The persisted count
    still has to appear so triage can see the rows aren't lost."""
    objects = _make_count_mock(return_value=3)
    p1, p2 = _patch_models(objects)
    err_cm, errors = _captured_logger()
    warn_cm, warnings = _captured_warning()
    with p1, p2, err_cm, warn_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=4,
            last_status_phase="generating",
            transport="websocket",
            stream_error="connection reset",
        )

    assert errors == []
    assert "client disconnected during generating" in warnings[-1]
    assert "3 ProposedAppeal row(s)" in warnings[-1]


@pytest.mark.asyncio
async def test_generation_trace_fields_appear_in_log():
    """The generation-trace fields (gen_id / timing / model / shed tier) must
    be threaded into the log so a client report joins to the server trace."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    log_cm, captured = _captured_logger()
    with p1, p2, log_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=3,
            last_status_phase="generating",
            transport="websocket",
            generation_id="abc123def456",
            make_appeals_seconds=142.4,
            first_model="fhi-legacy",
            shed_tier=2,
            models_tried="fhi-legacy:timeout,fhi-2025:no_output",
        )

    msg = captured[-1]
    # Shared searchable tag so client + server appeal failures group together.
    assert "APPEAL_GEN_DIAG" in msg
    assert "gen_id=abc123def456" in msg
    assert "make_appeals_s=142.4" in msg
    assert "first_model=fhi-legacy" in msg
    assert "shed_tier=2" in msg
    assert "models_tried=[fhi-legacy:timeout,fhi-2025:no_output]" in msg


@pytest.mark.asyncio
async def test_appeal_gen_diag_tag_present_on_all_branches():
    """Every zero-appeal branch carries the APPEAL_GEN_DIAG tag."""
    for persisted in (5, 0):
        objects = _make_count_mock(return_value=persisted)
        p1, p2 = _patch_models(objects)
        log_cm, captured = _captured_logger()
        with p1, p2, log_cm:
            await log_zero_appeal_diagnostics(
                denial_id=42,
                status_count=1,
                last_status_phase="generating",
                transport="websocket",
            )
        assert "APPEAL_GEN_DIAG" in captured[-1]


class TestAppealGenTraceFields:
    """_AppealGenTraceFields parses the generation-trace fields off the status
    frames both transports stream, keeping that frame-shape knowledge in one
    place."""

    def test_captures_generation_id_from_init_frame(self):
        f = _AppealGenTraceFields()
        f.update_from_frame(
            {"type": "status", "phase": "init", "generation_id": "gen-xyz"}
        )
        assert f.as_kwargs()["generation_id"] == "gen-xyz"

    def test_captures_timing_and_model_from_done_frame(self):
        f = _AppealGenTraceFields()
        f.update_from_frame(
            {
                "type": "status",
                "phase": "done",
                "generation_id": "gen-xyz",
                "make_appeals_seconds": 12.3,
                "first_model": "fhi-2025",
                "shed_tier": 1,
            }
        )
        kw = f.as_kwargs()
        assert kw == {
            "generation_id": "gen-xyz",
            "make_appeals_seconds": 12.3,
            "first_model": "fhi-2025",
            "shed_tier": 1,
            "models_tried": None,
        }

    def test_first_model_none_sentinel_is_normalized_to_none(self):
        """The done frame sends first_model='none' when no model won; that
        sentinel must not leak into the log as a literal model name."""
        f = _AppealGenTraceFields()
        f.update_from_frame(
            {
                "type": "status",
                "phase": "done",
                "first_model": "none",
                "shed_tier": None,
            }
        )
        assert f.as_kwargs()["first_model"] is None

    def test_non_done_frames_leave_timing_unset(self):
        f = _AppealGenTraceFields()
        f.update_from_frame({"type": "status", "phase": "generating"})
        kw = f.as_kwargs()
        assert kw["make_appeals_seconds"] is None
        assert kw["first_model"] is None

    def test_captures_models_tried_from_done_frame(self):
        f = _AppealGenTraceFields()
        f.update_from_frame(
            {
                "type": "status",
                "phase": "done",
                "models_tried": "fhi-legacy:no_output,sonar:http_429",
            }
        )
        assert f.as_kwargs()["models_tried"] == "fhi-legacy:no_output,sonar:http_429"

    def test_models_tried_none_sentinel_normalized(self):
        f = _AppealGenTraceFields()
        f.update_from_frame({"type": "status", "phase": "done", "models_tried": "none"})
        assert f.as_kwargs()["models_tried"] is None


class TestStreamErrorClassification:
    """A zero-appeal stream_error that names a closed transport is a client
    disconnect, not a server/model failure."""

    @pytest.mark.parametrize(
        "err",
        [
            "unable to perform operation on <TCPTransport closed=True reading=False>; the handler is closed",
            "TCPTransport closed=True",
            "Connection reset by peer",
            "[Errno 32] Broken pipe",
            "Connection lost",
        ],
    )
    def test_disconnect_markers_match(self, err):
        assert _stream_error_is_client_disconnect(err)

    @pytest.mark.parametrize(
        "err", [None, "", "Server error while generating appeals."]
    )
    def test_non_disconnect_does_not_match(self, err):
        assert not _stream_error_is_client_disconnect(err)


@pytest.mark.asyncio
async def test_client_disconnect_during_research_is_not_generation_failure():
    """The reported production line: last_phase=research + closed transport
    must read as a client disconnect (WARNING), not 'Generation produced
    nothing' (ERROR) which points triage at the model backends."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    warn_cm, warnings = _captured_warning()
    err_cm, errors = _captured_logger()
    with p1, p2, warn_cm, err_cm:
        await log_zero_appeal_diagnostics(
            denial_id=22880,
            status_count=5,
            last_status_phase="research",
            transport="websocket",
            stream_error=(
                "unable to perform operation on <TCPTransport closed=True "
                "reading=False 0x5629d9289e10>; the handler is closed"
            ),
        )
    # No ERROR-level "Generation produced nothing".
    assert errors == []
    assert len(warnings) == 1
    msg = warnings[0]
    assert "client disconnected during research" in msg
    assert "generation not reached" in msg
    assert "Generation produced nothing" not in msg
    assert "APPEAL_GEN_DIAG" in msg


@pytest.mark.asyncio
async def test_client_disconnect_during_generating_notes_generation_started():
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    warn_cm, warnings = _captured_warning()
    err_cm, errors = _captured_logger()
    with p1, p2, warn_cm, err_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=8,
            last_status_phase="generating",
            transport="websocket",
            stream_error="the handler is closed",
        )
    assert errors == []
    assert (
        "client disconnected during generating after generation started" in warnings[0]
    )


@pytest.mark.asyncio
async def test_server_side_connection_reset_is_not_filed_as_a_disconnect():
    """A server-side failure whose message merely mentions a reset connection
    (Postgres dropping the app's session mid-generation) must stay an ERROR.

    The disconnect classification is pure string matching, so callers that can
    prove the failure came out of the generator rather than a socket write pass
    error_from_send=False -- without it a real outage is downgraded to WARNING
    and stops paging."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    warn_cm, warnings = _captured_warning()
    err_cm, errors = _captured_logger()
    with p1, p2, warn_cm, err_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=4,
            last_status_phase="generating",
            transport="rest",
            stream_error=(
                "OperationalError: could not receive data from server: "
                "Connection reset by peer"
            ),
            error_from_send=False,
        )
    assert warnings == []
    assert "Generation produced nothing" in errors[-1]


@pytest.mark.asyncio
async def test_rest_fallback_wires_error_from_send_false():
    """End-to-end over the REST fallback view, not the helper in isolation.

    The test above pins the CLASSIFICATION; this one pins the WIRING, and only
    this one fails if streaming_appeals_rest_fallback stops passing
    error_from_send=False (or passes the wrong value). Drive the real view with
    a generator that dies on a Postgres reset mid-stream and assert it is still
    filed as a generation failure.
    """
    from rest_framework.test import APIRequestFactory

    from fighthealthinsurance import rest_views

    reset_error = "could not receive data from server: Connection reset by peer"

    async def _boom(_data):
        # One status frame first, so the run looks like a real generation that
        # got under way rather than an immediate blow-up.
        yield json.dumps({"type": "status", "phase": "init"}) + "\n"
        raise OperationalError(reset_error)

    request = APIRequestFactory().post(
        "/ziggy/rest/appeals/streaming_fallback",
        {"denial_id": 42, "email": "a@b.com", "semi_sekret": "s"},
        format="json",
    )

    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    warn_cm, warnings = _captured_warning()
    err_cm, errors = _captured_logger()
    with p1, p2, warn_cm, err_cm, patch.object(
        rest_views.common_view_logic,
        "get_denial_for_action",
        return_value=MagicMock(),
    ), patch.object(
        rest_views.common_view_logic.AppealsBackendHelper,
        "generate_appeals",
        _boom,
    ), patch.object(
        # The view logs its own traceback for the failure; silence it so the
        # test output isn't a wall of stack trace.
        rest_views,
        "logger",
        MagicMock(),
    ):
        response = rest_views.streaming_appeals_rest_fallback(request)
        async for _chunk in response.streaming_content:
            pass

    # Reached log_zero_appeal_diagnostics as a GENERATION failure. Without
    # error_from_send=False the "connection reset" substring would match the
    # client-disconnect markers and downgrade this to a WARNING.
    assert warnings == []
    assert errors, "expected a zero-appeal ERROR from the REST fallback"
    assert "Generation produced nothing" in errors[-1]
    assert reset_error in errors[-1]


@pytest.mark.asyncio
async def test_rest_fallback_disconnect_log_includes_stream_timing():
    """A REST client hanging up mid-stream logs how long the stream ran and
    how long it had been silent.

    The client can only report "the connection dropped"; the server-side
    elapsed/idle pair is what tells triage whether an intermediary reaped an
    idle stream (long idle gap) or the connection died while we were actively
    writing (idle near zero).
    """
    from rest_framework.test import APIRequestFactory

    from fighthealthinsurance import rest_views

    async def _two_status_frames(_data):
        yield json.dumps({"type": "status", "phase": "generating"}) + "\n"
        yield json.dumps({"type": "status", "phase": "generating"}) + "\n"

    request = APIRequestFactory().post(
        "/ziggy/rest/appeals/streaming_fallback",
        {"denial_id": 42, "email": "a@b.com", "semi_sekret": "s"},
        format="json",
    )

    fake_logger = MagicMock()
    with patch.object(
        rest_views.common_view_logic,
        "get_denial_for_action",
        return_value=MagicMock(),
    ), patch.object(
        rest_views.common_view_logic.AppealsBackendHelper,
        "generate_appeals",
        _two_status_frames,
    ), patch.object(
        rest_views, "logger", fake_logger
    ):
        response = rest_views.streaming_appeals_rest_fallback(request)
        # Drive the view's own generator (not the `streaming_content`
        # wrapper): closing it is what delivers GeneratorExit to the
        # disconnect branch, exactly as an ASGI client hangup does.
        stream = response._iterator
        # Leading "\n", first record, its framing "\n", then the second
        # record -- leaving the generator suspended on a yield, mid-stream.
        for _ in range(4):
            await stream.__anext__()
        await stream.aclose()

    messages = [call.args[0] for call in fake_logger.warning.call_args_list]
    assert messages, "expected a client-disconnect WARNING"
    assert "client disconnected mid-stream" in messages[-1]
    assert re.search(r"elapsed=\d+\.\d+s idle=\d+\.\d+s", messages[-1]), messages[-1]


@pytest.mark.asyncio
async def test_rest_fallback_disconnect_idle_excludes_generation_gap():
    """`idle` measures silence since the last frame WE sent, not since the last
    frame we finished bookkeeping for.

    A hangup injects GeneratorExit at whichever `yield` the stream is suspended
    on, so the idle timestamp has to be stamped before each yield. Stamped
    after, a disconnect immediately following a slow-to-produce frame reports
    the whole generation gap as idle -- which would point triage at an idle
    proxy timeout when the server was writing right up to the disconnect.
    """
    from rest_framework.test import APIRequestFactory

    from fighthealthinsurance import rest_views

    # Comfortably wider than the log's 0.1s formatting granularity, so the
    # buggy (stamp-after-yield) reading of ~1 gap can't round into the passing
    # band and make this flaky.
    gap = 0.4

    async def _slow_between_frames(_data):
        for _ in range(2):
            await asyncio.sleep(gap)
            yield json.dumps({"type": "status", "phase": "generating"}) + "\n"

    request = APIRequestFactory().post(
        "/ziggy/rest/appeals/streaming_fallback",
        {"denial_id": 42, "email": "a@b.com", "semi_sekret": "s"},
        format="json",
    )

    fake_logger = MagicMock()
    with patch.object(
        rest_views.common_view_logic,
        "get_denial_for_action",
        return_value=MagicMock(),
    ), patch.object(
        rest_views.common_view_logic.AppealsBackendHelper,
        "generate_appeals",
        _slow_between_frames,
    ), patch.object(
        rest_views, "logger", fake_logger
    ):
        response = rest_views.streaming_appeals_rest_fallback(request)
        stream = response._iterator
        # Stop on the second record: the stream has just produced a frame after
        # a `gap`-long wait, so a hangup here is the "died mid-write" case.
        for _ in range(4):
            await stream.__anext__()
        await stream.aclose()

    message = fake_logger.warning.call_args_list[-1].args[0]
    match = re.search(r"elapsed=(\d+\.\d+)s idle=(\d+\.\d+)s", message)
    assert match, message
    elapsed, idle = float(match.group(1)), float(match.group(2))
    # Two generation gaps have elapsed, but we were writing at the moment of
    # the disconnect, so the idle window must not include either of them.
    # The 0.1s slack on elapsed is the log's formatting granularity.
    assert elapsed >= 2 * gap - 0.1, message
    assert idle < gap / 2, message


@pytest.mark.asyncio
async def test_zero_persisted_without_disconnect_still_generation_failure():
    """A zero-appeal session whose stream_error is NOT a disconnect keeps the
    'Generation produced nothing' ERROR."""
    objects = _make_count_mock(return_value=0)
    p1, p2 = _patch_models(objects)
    err_cm, errors = _captured_logger()
    with p1, p2, err_cm:
        await log_zero_appeal_diagnostics(
            denial_id=42,
            status_count=3,
            last_status_phase="generating",
            transport="websocket",
            stream_error="Server error while generating appeals.",
        )
    assert "Generation produced nothing" in errors[-1]


class TestDeniedItemsAnalysisDispatchGuard:
    """enqueue_denied_items_analysis runs on every chat disconnect. Touching the
    actor ref with no cluster configured makes Ray auto-init a whole LOCAL
    cluster inside the ASGI process, so it must be gated."""

    @pytest.mark.asyncio
    async def test_skips_dispatch_when_no_ray_cluster(self):
        from fighthealthinsurance.websockets import enqueue_denied_items_analysis

        with patch(
            "fighthealthinsurance.base_actor_ref.ray_cluster_available",
            return_value=False,
        ), patch(
            "fighthealthinsurance.denied_items_analysis_actor_ref."
            "denied_items_analysis_actor_ref"
        ) as mock_ref:
            await enqueue_denied_items_analysis(chat_id="chat-1")
        # The ref must not even be touched -- `.get` is what auto-inits Ray.
        mock_ref.get.run_analysis.remote.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_when_a_cluster_is_available(self):
        from fighthealthinsurance.websockets import enqueue_denied_items_analysis

        with patch(
            "fighthealthinsurance.base_actor_ref.ray_cluster_available",
            return_value=True,
        ), patch(
            "fighthealthinsurance.denied_items_analysis_actor_ref."
            "denied_items_analysis_actor_ref"
        ) as mock_ref:
            await enqueue_denied_items_analysis(chat_id="chat-1")
        mock_ref.get.run_analysis.remote.assert_called_once_with(chat_id="chat-1")
