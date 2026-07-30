"""A failed chat turn must not erase the user's message.

Previously, when every model failed, the whole turn (including the user's
message) was dropped from chat_history -- after a reconnect the replay showed
the conversation as if the user never typed anything. Now the user message is
persisted even on failure, the client gets an error frame, and the failure is
logged at ERROR with the exception attached.

Also covers the transactional persistence helper directly: two interleaved
writers over the same chat row must not lose each other's messages.
"""

import contextlib
import typing
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from loguru import logger
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.chat_persistence import apersist_chat_turn
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


@contextlib.contextmanager
def _llm_call_fails(side_effect):
    """Route model selection to a mock backend and make the LLM call fail."""
    mock_model = MockChatModel()
    with contextlib.ExitStack() as stack:
        get_backends = stack.enter_context(
            patch("fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends")
        )
        get_backends.return_value = [mock_model]
        get_fallback = stack.enter_context(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
            )
        )
        get_fallback.return_value = ([mock_model], [])
        stack.enter_context(
            patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            )
        )
        stack.enter_context(
            patch.object(
                ChatInterface,
                "_call_llm_with_actions",
                new=AsyncMock(side_effect=side_effect),
            )
        )
        yield


async def _make_professional_chat(username, npi):
    user = await sync_to_async(User.objects.create_user)(
        username=username, password="testpass", email=f"{username}@example.com"
    )
    professional = await sync_to_async(ProfessionalUser.objects.create)(
        user=user, active=True, npi_number=npi
    )
    chat = await sync_to_async(OngoingChat.objects.create)(
        professional_user=professional,
        chat_history=[],
        summary_for_next_call=[],
    )
    return user, chat


class _FrameRecorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)


class ChatFailurePersistenceTest(APITestCase):
    async def _run_failing_turn(self, username, npi, side_effect):
        user, chat = await _make_professional_chat(username, npi)
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with _llm_call_fails(side_effect):
            await interface.handle_chat_message("Why was my MRI claim denied?")
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
