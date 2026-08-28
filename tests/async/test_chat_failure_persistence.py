"""A failed chat turn must not erase the user's message.

Previously, when every model failed, the whole turn (including the user's
message) was dropped from chat_history -- after a reconnect the replay showed
the conversation as if the user never typed anything. Now the user message is
persisted even on failure, the client gets an error frame, and the failure is
logged at ERROR with the exception attached.

Also covers the transactional persistence helper directly: two interleaved
writers over the same chat row must not lose each other's messages.
"""

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, patch

from loguru import logger
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.chat_persistence import apersist_chat_turn
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import ChatTurn, OngoingChat

# Shared with the letter-fallback tests via chat_fixtures (tests/async is not
# an importable package name, so the helpers cannot live in this module).
from tests.chat_fixtures import (
    FrameRecorder as _FrameRecorder,
    llm_call_fails as _llm_call_fails,
    make_professional_chat as _make_professional_chat,
)


@contextlib.contextmanager
def _captured_logs():
    """Capture every record at DEBUG and above, plus each line as a sink that
    keeps tracebacks but not frame variables (as the deployed sinks do) would
    write it."""
    records: list = []
    lines: list = []

    def _sink(msg):
        records.append(msg.record)
        lines.append(str(msg))

    sink_id = logger.add(
        _sink,
        level="DEBUG",
        format="{message}\n{exception}",
        backtrace=False,
        diagnose=False,
    )
    try:
        yield records, lines
    finally:
        logger.remove(sink_id)


class ChatFailurePersistenceTest(APITestCase):
    async def _run_failing_turn(
        self, username, npi, side_effect, message="Why was my MRI claim denied?"
    ):
        user, chat = await _make_professional_chat(username, npi)
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with _llm_call_fails(side_effect):
            await interface.handle_chat_message(message)
        return chat, recorder

    async def test_user_message_persisted_when_llm_raises(self):
        chat, recorder = await self._run_failing_turn(
            "failpersist1", "9999910001", RuntimeError("all models down")
        )
        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [m for m in (fresh.chat_history or []) if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], "Why was my MRI claim denied?")
        # No hallucinated assistant reply was stored.
        assistant_msgs = [
            m for m in (fresh.chat_history or []) if m.get("role") == "assistant"
        ]
        self.assertEqual(assistant_msgs, [])

    async def test_error_frame_sent_when_llm_raises(self):
        chat, recorder = await self._run_failing_turn(
            "failpersist2", "9999910002", RuntimeError("all models down")
        )
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertTrue(
            error_frames, f"expected an error frame, got: {recorder.frames}"
        )

    async def test_user_message_persisted_when_llm_returns_nothing(self):
        chat, recorder = await self._run_failing_turn(
            "failpersist3", "9999910003", lambda *a, **k: (None, None)
        )
        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [m for m in (fresh.chat_history or []) if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)

    async def test_failure_logged_at_error_with_exception(self):
        records = []
        sink_id = logger.add(lambda msg: records.append(msg.record), level="ERROR")
        try:
            await self._run_failing_turn(
                "failpersist4", "9999910004", RuntimeError("distinctive-boom-marker")
            )
        finally:
            logger.remove(sink_id)
        error_records = [r for r in records if r["level"].name == "ERROR"]
        self.assertTrue(error_records)
        with_exception = [
            r
            for r in error_records
            if r["exception"] is not None
            and "distinctive-boom-marker" in str(r["exception"])
        ]
        self.assertTrue(
            with_exception,
            "expected an ERROR record carrying the exception traceback",
        )

    async def test_failure_log_records_sizes_not_message_text(self):
        """The failure branch logs the message size, the error class and the
        flags; the message itself never reaches any log line."""
        sentinel_message = "Why was my MRI denied? SENTINEL-failpersist-6"
        user, chat = await _make_professional_chat("failpersist6", "9999910009")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(),
            chat=chat,
            user=user,
        )
        with _captured_logs() as (records, lines):
            with _llm_call_fails(RuntimeError("distinctive-boom-marker")):
                await interface.handle_chat_message(sentinel_message)

        leaked = [line for line in lines if "SENTINEL-failpersist-6" in line]
        self.assertEqual(leaked, [], "the user message reached a log line")
        # Exception text stays in the attached traceback, never in a message.
        self.assertFalse(
            [r for r in records if "distinctive-boom-marker" in r["message"]]
        )
        failure_lines = [
            r["message"]
            for r in records
            if r["level"].name == "ERROR"
            and r["message"].startswith("Failed to generate a response")
        ]
        self.assertEqual(len(failure_lines), 1, failure_lines)
        self.assertIn(f"in chat {chat.id}", failure_lines[0])
        self.assertIn(f"message_chars={len(sentinel_message)}", failure_lines[0])
        self.assertIn("error=RuntimeError", failure_lines[0])
        self.assertIn("timed_out=False", failure_lines[0])
        self.assertIn("use_external_models=True", failure_lines[0])
        self.assertTrue(
            [
                r["message"]
                for r in records
                if r["message"]
                == f"Chat generation failed for chat {chat.id}: RuntimeError"
            ]
        )

    async def test_failure_log_when_models_return_nothing(self):
        sentinel_message = "Please help SENTINEL-failpersist-7"
        user, chat = await _make_professional_chat("failpersist7", "9999910010")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(),
            chat=chat,
            user=user,
            use_external_models=False,
        )
        with _captured_logs() as (records, lines):
            with _llm_call_fails(lambda *a, **k: (None, None)):
                await interface.handle_chat_message(sentinel_message)

        self.assertEqual(
            [line for line in lines if "SENTINEL-failpersist-7" in line], []
        )
        failure_lines = [
            r["message"]
            for r in records
            if r["message"].startswith("Failed to generate a response")
        ]
        self.assertEqual(len(failure_lines), 1, failure_lines)
        self.assertIn(f"message_chars={len(sentinel_message)}", failure_lines[0])
        self.assertIn("error=none", failure_lines[0])
        self.assertIn("use_external_models=False", failure_lines[0])

    async def test_failure_log_when_the_turn_times_out(self):
        sentinel_message = "Still waiting SENTINEL-failpersist-8"
        user, chat = await _make_professional_chat("failpersist8", "9999910011")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(),
            chat=chat,
            user=user,
        )

        async def stalled(*args, **kwargs):
            await asyncio.sleep(30)
            return ("should never be seen", None)

        with _captured_logs() as (records, lines):
            with patch.dict(
                "os.environ",
                {"FHI_CHAT_TURN_BUDGET": "1", "FHI_CHAT_HEARTBEAT_SECONDS": "600"},
            ), _llm_call_fails(stalled):
                await asyncio.wait_for(
                    interface.handle_chat_message(sentinel_message), timeout=20
                )

        self.assertEqual(
            [line for line in lines if "SENTINEL-failpersist-8" in line], []
        )
        failure_lines = [
            r["message"]
            for r in records
            if r["message"].startswith("Failed to generate a response")
        ]
        self.assertEqual(len(failure_lines), 1, failure_lines)
        self.assertIn(f"message_chars={len(sentinel_message)}", failure_lines[0])
        self.assertIn("error=none", failure_lines[0])
        self.assertIn("timed_out=True", failure_lines[0])

    async def test_retry_after_failure_does_not_duplicate_user_message(self):
        """The client retrying the same text after a failed turn must not
        produce two copies: the helper merges/dedupes against the fresh tail."""
        user, chat = await _make_professional_chat("failpersist5", "9999910005")
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with _llm_call_fails(RuntimeError("down")):
            await interface.handle_chat_message("Please help with my appeal")
            await interface.handle_chat_message("Please help with my appeal")
        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [m for m in (fresh.chat_history or []) if m.get("role") == "user"]
        self.assertEqual(
            len(user_msgs), 1, f"history grew duplicates: {fresh.chat_history}"
        )


class ChatFailureLoggingTest(APITestCase):
    """What a failed turn is allowed to say, and about whom.

    The failure log used to interpolate the user's own message:

        logger.error(f"Failed to generate response for user_message: '{...}'")

    Sentry fingerprints an issue by its message text, so one failure mode
    arrived as a new High-priority issue per distinct thing anybody typed --
    with their health situation, in their own words, as the issue title
    (PYTHON-DJANGO-00-MH and -MV are two of them).
    """

    _run_failing_turn = ChatFailurePersistenceTest._run_failing_turn

    async def test_the_users_own_words_stay_out_of_the_logs(self):
        private = "my daughter's gender-affirming surgery was denied"
        records = []
        sink_id = logger.add(lambda msg: records.append(msg.record), level="INFO")
        try:
            await self._run_failing_turn(
                "faillog1", "9999910011", RuntimeError("down"), message=private
            )
        finally:
            logger.remove(sink_id)
        leaked = [r["message"] for r in records if private in r["message"]]
        self.assertEqual(leaked, [], f"the user's message reached the logs: {leaked}")

    async def test_the_failure_log_still_says_enough_to_triage(self):
        records = []
        sink_id = logger.add(lambda msg: records.append(msg.record), level="ERROR")
        try:
            chat, _ = await self._run_failing_turn(
                "faillog2", "9999910012", lambda *a, **k: (None, None)
            )
        finally:
            logger.remove(sink_id)
        totals = [
            r["message"]
            for r in records
            if "Failed to generate a response" in r["message"]
        ]
        self.assertTrue(totals, f"expected a total-failure ERROR, got: {records}")
        self.assertIn(str(chat.id), totals[0])
        self.assertIn("message_chars=", totals[0])


class ChatClientHangupTest(APITestCase):
    """A user who closes the tab mid-turn is not a failure to report.

    Status and heartbeat frames go down the same socket as the reply, so a
    hangup surfaces here as a failed generation. Reporting it produced four
    more Sentry issues on top of the send itself, including a
    ``chat_turn_total_failure`` reliability event that pages (-M8).

    A hangup is what the consumer's send wrapper says it is -- ``ClientGone``
    -- and nothing else: the turn also wraps every tool handler and model
    call, and a transport-shaped error from one of THOSE is an outage the
    (still present) user has to hear about.
    """

    _run_failing_turn = ChatFailurePersistenceTest._run_failing_turn

    @staticmethod
    def _hangup():
        from fighthealthinsurance.client_gone import ClientGone

        return ClientGone()

    async def test_a_hangup_is_not_logged_at_error(self):
        records = []
        sink_id = logger.add(lambda msg: records.append(msg.record), level="ERROR")
        try:
            await self._run_failing_turn("hangup1", "9999910021", self._hangup())
        finally:
            logger.remove(sink_id)
        self.assertEqual([r["message"] for r in records], [])

    async def test_a_hangup_does_not_fire_the_reliability_event(self):
        with patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as mock_capture:
            await self._run_failing_turn("hangup2", "9999910022", self._hangup())
        mock_capture.assert_not_called()

    async def test_a_hangup_still_keeps_what_the_user_typed(self):
        """They may well reconnect; their message has to survive."""
        chat, _ = await self._run_failing_turn(
            "hangup3", "9999910023", self._hangup(), message="Please appeal this"
        )
        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [m for m in (fresh.chat_history or []) if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], "Please appeal this")

    async def test_a_genuine_failure_still_fires_the_reliability_event(self):
        """The exemption must be narrow."""
        with patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as mock_capture:
            await self._run_failing_turn(
                "hangup4", "9999910024", RuntimeError("all models down")
            )
        mock_capture.assert_called_once()

    async def test_a_reset_from_a_backend_is_not_a_hangup(self):
        """A model backend (or Postgres, via a tool handler) resetting the
        connection on US looks exactly like a departed client to a type
        sniff, and is nothing of the kind: the user is still there, and gets
        the error frame and the page."""
        with patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as mock_capture:
            chat, recorder = await self._run_failing_turn(
                "hangup5",
                "9999910025",
                ConnectionResetError("[Errno 104] Connection reset by peer"),
            )
        mock_capture.assert_called_once()
        self.assertTrue(
            [f for f in recorder.frames if "error" in f],
            f"the still-connected user must get an error frame: {recorder.frames}",
        )


class ChatHangupOutcomeTest(APITestCase):
    """A turn the user walked away from is counted "client_gone" -- once, not
    "ok", not "timeout" -- whichever write is first to find the socket closed.

    Each of these used to go wrong a different way (review). The heartbeat
    swallows its own send failures, so a departure it saw never reached
    handle_chat_message: the turn ran on, was counted "ok", and sent its reply
    into the closed socket. A departure the reply itself found was counted
    "ok" because the metric was recorded before the send. And a budget that
    ran out after the heartbeat saw the user leave logged an ERROR and counted
    the one turn under both "timeout" and "client_gone".
    """

    REPLY = "Here is what I found about your MRI denial."

    async def _run_turn(self, username, npi, send, *, reply_delay, env):
        """One turn with a mocked model; returns (chat, recorded outcomes,
        ERROR messages logged)."""
        user, chat = await _make_professional_chat(username, npi)
        interface = ChatInterface(send_json_message_func=send, chat=chat, user=user)

        async def reply(*args, **kwargs):
            await asyncio.sleep(reply_delay)
            return self.REPLY, None

        errors = []
        sink_id = logger.add(
            lambda msg: errors.append(msg.record["message"]), level="ERROR"
        )
        try:
            with patch.dict(os.environ, env), patch(
                "fighthealthinsurance.chat_interface.record_chat_turn"
            ) as mock_record, _llm_call_fails(reply):
                # Must not raise: nothing is sent into the closed socket.
                await interface.handle_chat_message("Why was my MRI claim denied?")
        finally:
            logger.remove(sink_id)
        return chat, [call.args[0] for call in mock_record.call_args_list], errors

    @staticmethod
    def _gone_on_every_frame():
        from fighthealthinsurance.client_gone import ClientGone

        async def gone(_frame):
            raise ClientGone()

        return gone

    async def _assistant_replies(self, chat):
        fresh = await OngoingChat.objects.aget(id=chat.id)
        return [m for m in (fresh.chat_history or []) if m.get("role") == "assistant"]

    async def test_a_hangup_seen_by_the_heartbeat_is_client_gone_not_ok(self):
        chat, outcomes, _ = await self._run_turn(
            "hbgone1",
            "9999910031",
            self._gone_on_every_frame(),
            # Long enough for several heartbeats at the interval below.
            reply_delay=0.3,
            env={"FHI_CHAT_HEARTBEAT_SECONDS": "0.05"},
        )
        self.assertEqual(outcomes, ["client_gone"])
        # ...and the reply is kept, so a reconnect replays it.
        self.assertEqual(len(await self._assistant_replies(chat)), 1)

    async def test_a_hangup_seen_only_by_the_reply_is_client_gone_not_ok(self):
        """A fast turn: no heartbeat fires, so the reply is the first write."""
        from fighthealthinsurance.client_gone import ClientGone

        async def gone_for_the_reply(frame):
            if frame.get("role") == "assistant":
                raise ClientGone()

        chat, outcomes, _ = await self._run_turn(
            "replygone1",
            "9999910032",
            gone_for_the_reply,
            reply_delay=0,
            env={"FHI_CHAT_HEARTBEAT_SECONDS": "60"},
        )
        self.assertEqual(outcomes, ["client_gone"])
        self.assertEqual(len(await self._assistant_replies(chat)), 1)

    async def test_a_turn_the_client_left_writes_no_chat_turn_row(self):
        """ChatTurn rows carry only the outcomes the dashboard reads; a
        departed client's turn is counted in the metric alone."""
        chat, outcomes, _ = await self._run_turn(
            "rowgone1",
            "9999910035",
            self._gone_on_every_frame(),
            reply_delay=0,
            env={"FHI_CHAT_HEARTBEAT_SECONDS": "60"},
        )
        self.assertEqual(outcomes, ["client_gone"])
        self.assertEqual(await ChatTurn.objects.filter(chat_id=chat.id).acount(), 0)

    async def test_a_delivered_turn_still_writes_its_ok_row(self):
        """Control for the test above: the harness does write rows."""
        chat, outcomes, _ = await self._run_turn(
            "rowok1",
            "9999910036",
            _FrameRecorder(),
            reply_delay=0,
            env={"FHI_CHAT_HEARTBEAT_SECONDS": "60"},
        )
        self.assertEqual(outcomes, ["ok"])
        self.assertEqual(
            [t.outcome async for t in ChatTurn.objects.filter(chat_id=chat.id)],
            ["ok"],
        )

    async def test_a_hangup_seen_by_a_tool_keeps_the_reply(self):
        """A tool's status frame is the first write to find the socket
        closed. The model's reply already exists, so it is persisted for a
        reconnect to replay, not discarded with the turn (review)."""
        from fighthealthinsurance.client_gone import ClientGone

        async def gone_for_status_frames(frame):
            if "status" in frame:
                raise ClientGone()

        user, chat = await _make_professional_chat("toolgone1", "9999910034")
        interface = ChatInterface(
            send_json_message_func=gone_for_status_frames, chat=chat, user=user
        )

        async def reply_with_a_tool_call(*args, **kwargs):
            # The tool step of _call_llm_with_actions, which the mock below
            # replaces: the real AppealTool on the interface's own senders.
            from fighthealthinsurance.chat.tools import AppealTool

            reply = self.REPLY + '\n**create_or_update_appeal** {"appeal_text": "x"}'
            tool = AppealTool(
                interface.send_status_message, interface.send_error_message
            )
            response, context, _ = await tool.handle(reply, "", chat=chat)
            return response, context

        with patch.dict(os.environ, {"FHI_CHAT_HEARTBEAT_SECONDS": "60"}), patch(
            "fighthealthinsurance.chat_interface.record_chat_turn"
        ) as mock_record, _llm_call_fails(reply_with_a_tool_call):
            await interface.handle_chat_message("Why was my MRI claim denied?")

        self.assertEqual(
            [call.args[0] for call in mock_record.call_args_list], ["client_gone"]
        )
        replies = await self._assistant_replies(chat)
        self.assertEqual([m["content"] for m in replies], [self.REPLY])

    async def test_no_frame_is_written_after_the_client_is_found_gone(self):
        """Once one send finds the socket closed, later frames fail at once
        instead of each making a doomed transport write."""
        from fighthealthinsurance.client_gone import ClientGone

        user, chat = await _make_professional_chat("gonewrite1", "9999910037")
        send = AsyncMock(side_effect=ClientGone())
        interface = ChatInterface(send_json_message_func=send, chat=chat, user=user)
        for _ in range(3):
            with self.assertRaises(ClientGone):
                await interface.send_status_message("Still working...")
        self.assertEqual(send.await_count, 1)

    async def test_a_departed_turn_still_notes_its_reply_check_health(self):
        """The reply check ran whether or not anyone stayed to read the
        reply, so its health note is written as on any other turn end."""
        user, chat = await _make_professional_chat("gonegate1", "9999910038")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        gate = AsyncMock()
        interface._reply_gate = gate
        with patch("fighthealthinsurance.chat_interface.record_chat_turn"):
            await interface._end_turn_client_gone()
        gate.anote_health.assert_awaited_once()
        self.assertIsNone(interface._reply_gate)

    async def test_a_budget_run_out_after_the_hangup_is_not_a_timeout(self):
        _, outcomes, errors = await self._run_turn(
            "budgetgone1",
            "9999910033",
            self._gone_on_every_frame(),
            # The heartbeat sees the departure well before the budget ends,
            # and the model outlasts the budget.
            reply_delay=2.0,
            env={
                "FHI_CHAT_HEARTBEAT_SECONDS": "0.05",
                "FHI_CHAT_TURN_BUDGET": "0.3",
            },
        )
        self.assertEqual(outcomes, ["client_gone"])
        self.assertEqual(errors, [], "a departure must not log an ERROR")


class PersistChatTurnHelperTest(APITestCase):
    async def test_interleaved_writers_lose_no_messages(self):
        """Two stale in-memory copies of the same chat both persist turns;
        all four messages must survive (the old asave() flow lost the first
        writer's turn entirely)."""
        user, chat = await _make_professional_chat("mergewriters", "9999910006")
        # Both writers hold the same (empty-history) snapshot.
        copy_a = await OngoingChat.objects.aget(id=chat.id)
        copy_b = await OngoingChat.objects.aget(id=chat.id)

        await apersist_chat_turn(
            copy_a,
            new_messages=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
            ],
        )
        await apersist_chat_turn(
            copy_b,
            new_messages=[
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
            ],
        )

        fresh = await OngoingChat.objects.aget(id=chat.id)
        contents = [m["content"] for m in fresh.chat_history]
        self.assertEqual(
            contents,
            ["first question", "first answer", "second question", "second answer"],
        )

    async def test_consecutive_user_messages_merge_against_fresh_tail(self):
        user, chat = await _make_professional_chat("mergeusers", "9999910007")
        await apersist_chat_turn(
            chat, new_messages=[{"role": "user", "content": "part one"}]
        )
        stale = await OngoingChat.objects.aget(id=chat.id)
        await apersist_chat_turn(
            stale, new_messages=[{"role": "user", "content": "part two"}]
        )
        fresh = await OngoingChat.objects.aget(id=chat.id)
        self.assertEqual(len(fresh.chat_history), 1)
        self.assertEqual(fresh.chat_history[0]["content"], "part one part two")

    async def test_summary_tail_dedupe(self):
        user, chat = await _make_professional_chat("mergesummary", "9999910008")
        await apersist_chat_turn(chat, new_summaries=["summary A"])
        stale = await OngoingChat.objects.aget(id=chat.id)
        await apersist_chat_turn(stale, new_summaries=["summary A", "summary B"])
        fresh = await OngoingChat.objects.aget(id=chat.id)
        self.assertEqual(fresh.summary_for_next_call, ["summary A", "summary B"])
