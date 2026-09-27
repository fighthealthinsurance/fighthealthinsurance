"""Chat log lines carry sizes, ids, error classes and flags, never chat text.

Each test plants a distinctive sentinel in one piece of patient data (the
user's message, a model reply, a document name, the chat history, an
exception raised while handling them) and checks that no log line written
while that data was in flight contains it. Exceptions may still attach their
traceback, the way the deployed sinks write it; only the log message itself
is held to the class name. Lines built from our own fixed text (the chat
lookup reasons) are pinned word for word, and uploads and long pastes with a
non-string document name still store.
"""

import contextlib
import typing
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from loguru import logger
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.context_manager import background_generate_summary
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import (
    ChatDocument,
    ChatType,
    OngoingChat,
    PolicyDocument,
    ProfessionalUser,
)
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.chat_fixtures import FRESH_REPLY, RecordingChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


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


def _leaks(lines, sentinel):
    return [line for line in lines if sentinel in line]


def _messages(records):
    return [r["message"] for r in records]


class _FrameRecorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)


async def _make_chat(username, npi, **chat_kwargs):
    user = await User.objects.acreate_user(
        username=username, password="testpass", email=f"{username}@example.com"
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number=npi
    )
    chat_kwargs.setdefault("chat_history", [])
    chat_kwargs.setdefault("summary_for_next_call", [])
    chat = await OngoingChat.objects.acreate(
        professional_user=professional, **chat_kwargs
    )
    return user, chat


def _patched_router(models):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
        return_value=(models, []),
    )


def _patch_fire_and_forget():
    return patch(
        "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )


async def _run_now(coro):
    await coro


async def _drop(coro):
    # Stands in for background work the test doesn't need, without leaving
    # an un-awaited coroutine behind.
    coro.close()


def _skip_document_summaries():
    return patch(
        "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
        side_effect=_drop,
    )


class ChatTurnLogsTest(APITestCase):
    async def test_turn_logs_carry_no_message_or_reply_text(self):
        user, chat = await _make_chat("sizelog1", "9999940001")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        # Sentinel first, so even a short prefix of the reply would carry it.
        reply = f"SENTINEL-reply-1 {FRESH_REPLY}"
        model = RecordingChatModel(always_reply=reply)
        with _captured_logs() as (records, lines):
            with _patched_router([model]), _patch_fire_and_forget(), patch(
                "fighthealthinsurance.chat_interface.get_document_context_for_message",
                side_effect=RuntimeError("SENTINEL-error-1"),
            ):
                await interface.handle_chat_message(
                    "My MRI claim was denied SENTINEL-user-1"
                )

        self.assertTrue(model.calls, "the turn never reached the model")
        self.assertEqual(_leaks(lines, "SENTINEL-user-1"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-reply-1"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-error-1"), [])
        messages = _messages(records)
        self.assertIn(
            f"Using best result (response_chars={len(reply)})",
            messages,
        )
        self.assertIn(
            f"Skipping document context for chat {chat.id}: RuntimeError",
            messages,
        )

    async def test_primary_and_retry_failures_log_error_classes(self):
        user, chat = await _make_chat("sizelog2", "9999940002")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )

        async def _boom(calls, *args, **kwargs):
            for call in calls:
                close = getattr(call, "close", None)
                if close:
                    close()
            raise RuntimeError("SENTINEL-error-2")

        with _captured_logs() as (records, lines):
            with _patched_router([RecordingChatModel()]), _patch_fire_and_forget():
                with patch(
                    "fighthealthinsurance.chat_interface.best_two_within_timelimit",
                    side_effect=_boom,
                ), patch(
                    "fighthealthinsurance.chat.retry_handler.best_two_within_timelimit",
                    side_effect=_boom,
                ):
                    await interface.handle_chat_message("Please help SENTINEL-user-2")

        self.assertEqual(_leaks(lines, "SENTINEL-user-2"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-error-2"), [])
        messages = _messages(records)
        self.assertIn("Primary models all failed: RuntimeError", messages)
        self.assertIn("Fallback models also failed: RuntimeError", messages)

    async def test_status_messages_log_only_their_size(self):
        user, chat = await _make_chat("sizelog3", "9999940003")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        status = "Searching PubMed for: SENTINEL-status-3..."
        with _captured_logs() as (records, lines):
            await interface.send_status_message(status)

        self.assertEqual(_leaks(lines, "SENTINEL-status-3"), [])
        self.assertIn(
            f"Chat {chat.id} status (status_chars={len(status)})",
            _messages(records),
        )

    async def test_document_upload_logs_sizes_not_name_or_text(self):
        user, chat = await _make_chat("sizelog4", "9999940004")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        name = "SENTINEL-docname-4.pdf"
        text = "Denial letter body SENTINEL-doctext-4 " * 5
        with _captured_logs() as (records, lines):
            with _patched_router(
                [RecordingChatModel(always_reply=FRESH_REPLY)]
            ), _patch_fire_and_forget(), patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ):
                await interface.handle_chat_message(
                    text, is_document=True, document_name=name
                )

        self.assertEqual(_leaks(lines, "SENTINEL-docname-4"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-doctext-4"), [])
        self.assertIn(
            f"Document uploaded in chat {chat.id} "
            f"(name_chars={len(name)}, {len(text)} chars)",
            _messages(records),
        )

    async def test_long_paste_logs_sizes_not_name_or_text(self):
        user, chat = await _make_chat("sizelog5", "9999940005")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        name = "SENTINEL-pastename-5.txt"
        text = "SENTINEL-paste-5 " + ("my records say " * 800)
        with _captured_logs() as (records, lines):
            with _patched_router(
                [RecordingChatModel(always_reply=FRESH_REPLY)]
            ), _patch_fire_and_forget(), patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ):
                await interface.handle_chat_message(text, document_name=name)

        self.assertEqual(_leaks(lines, "SENTINEL-pastename-5"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-paste-5"), [])
        self.assertTrue(
            [
                m
                for m in _messages(records)
                if m.startswith(f"Long pasted message in chat {chat.id}")
                and m.endswith(f"for reference (name_chars={len(name)})")
            ],
            "expected the long-paste line with the name size",
        )

    async def test_numeric_document_name_upload_still_stores(self):
        user, chat = await _make_chat("sizelog15", "9999940015")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        text = "Denial letter body " * 5
        with _captured_logs() as (records, lines):
            with _patched_router(
                [RecordingChatModel(always_reply=FRESH_REPLY)]
            ), _patch_fire_and_forget(), _skip_document_summaries():
                await interface.handle_chat_message(
                    text, is_document=True, document_name=12345
                )

        stored = [
            d async for d in ChatDocument.objects.filter(chat=chat).values_list(
                "document_name", flat=True
            )
        ]
        self.assertEqual(stored, ["12345"])
        self.assertIn(
            f"Document uploaded in chat {chat.id} "
            f"(name_chars=5, {len(text)} chars)",
            _messages(records),
        )

    async def test_numeric_document_name_long_paste_still_stores(self):
        user, chat = await _make_chat("sizelog16", "9999940016")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        text = "my records say " * 800
        with _captured_logs() as (records, lines):
            with _patched_router(
                [RecordingChatModel(always_reply=FRESH_REPLY)]
            ), _patch_fire_and_forget(), _skip_document_summaries():
                await interface.handle_chat_message(text, document_name=12345)

        stored = [
            d async for d in ChatDocument.objects.filter(chat=chat).values_list(
                "document_name", flat=True
            )
        ]
        self.assertEqual(stored, ["12345"])
        self.assertTrue(
            [
                m
                for m in _messages(records)
                if m.startswith(f"Long pasted message in chat {chat.id}")
                and m.endswith("for reference (name_chars=5)")
            ],
            "expected the long-paste line with the name size",
        )

    async def test_microsite_lookup_error_logs_class_name(self):
        user, chat = await _make_chat(
            "sizelog6", "9999940006", microsite_slug="sizelog-microsite"
        )
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        with _captured_logs() as (records, lines):
            with _patched_router(
                [RecordingChatModel(always_reply=FRESH_REPLY)]
            ), _patch_fire_and_forget(), patch(
                "fighthealthinsurance.chat_interface.get_microsite",
                side_effect=RuntimeError("SENTINEL-error-6"),
            ):
                await interface.handle_chat_message("Hello there")

        self.assertEqual(_leaks(lines, "SENTINEL-error-6"), [])
        self.assertIn(
            f"Error loading microsite for chat {chat.id}: RuntimeError",
            _messages(records),
        )

    async def test_microsite_context_error_logs_class_name(self):
        user, chat = await _make_chat(
            "sizelog7", "9999940007", microsite_slug="sizelog-microsite"
        )
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        microsite = MagicMock()
        microsite.pubmed_search_terms = []
        microsite.get_combined_context.side_effect = RuntimeError("SENTINEL-error-7")
        with _captured_logs() as (records, lines):
            with _patched_router([RecordingChatModel(always_reply=FRESH_REPLY)]), patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                side_effect=_run_now,
            ), patch(
                "fighthealthinsurance.chat_interface.get_microsite",
                return_value=microsite,
            ):
                await interface.handle_chat_message("Hello there")

        microsite.get_combined_context.assert_called()
        messages = _messages(records)
        self.assertFalse([m for m in messages if "SENTINEL-error-7" in m])
        self.assertIn("Error loading microsite context: RuntimeError", messages)

    async def test_policy_analysis_error_logs_class_name(self):
        user, chat = await _make_chat(
            "sizelog8", "9999940008", session_key="sizelog-session-8"
        )
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        with _captured_logs() as (records, lines):
            with patch.object(
                PolicyDocument.objects,
                "filter",
                side_effect=RuntimeError("SENTINEL-error-8"),
            ):
                await interface._handle_policy_analysis(
                    chat, "My question: SENTINEL-user-8"
                )

        self.assertEqual(_leaks(lines, "SENTINEL-user-8"), [])
        messages = _messages(records)
        self.assertFalse([m for m in messages if "SENTINEL-error-8" in m])
        self.assertIn(
            f"Error handling policy analysis for chat {chat.id}: RuntimeError",
            messages,
        )

    async def test_user_info_error_logs_class_name(self):
        user, chat = await _make_chat("sizelog9", "9999940009")
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        with _captured_logs() as (records, lines):
            with patch.object(
                OngoingChat,
                "summarize_user",
                side_effect=RuntimeError("SENTINEL-error-9"),
            ):
                self.assertEqual(await interface._get_user_info(), "a user")

        self.assertEqual(_leaks(lines, "SENTINEL-error-9"), [])
        self.assertIn(
            "Could not generate detailed user info: RuntimeError",
            _messages(records),
        )

    async def test_background_summary_error_logs_class_name(self):
        placeholder = "[summary placeholder] [2]"
        user, chat = await _make_chat(
            "sizelog10",
            "9999940010",
            chat_history=[
                {"role": "user", "content": "SENTINEL-history-10"},
                {"role": "assistant", "content": FRESH_REPLY},
            ],
            summary_for_next_call=[placeholder],
        )
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.summarize_chat_history",
                new=AsyncMock(side_effect=RuntimeError("SENTINEL-error-10")),
            ):
                await background_generate_summary(chat.id, placeholder)

        self.assertEqual(_leaks(lines, "SENTINEL-history-10"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-error-10"), [])
        self.assertIn(
            f"Background summary generation failed for chat {chat.id}: " "RuntimeError",
            _messages(records),
        )


class ChatLookupLogsTest(APITestCase):
    async def test_unknown_chat_id_logs_class_and_fixed_reason(self):
        chat_id = "0b7c3a52-6f1e-4d8a-9c1b-2e5f7a9d4c10"
        with _captured_logs() as (records, lines):
            chat = await OngoingChatConsumer()._get_or_create_chat(
                None,
                chat_type=ChatType.PATIENT,
                chat_id=chat_id,
                session_key="sizelog-session-17",
                email="sizelog17@example.com",
            )
        self.assertNotEqual(str(chat.id), chat_id)
        self.assertIn(
            f"Chat with id {chat_id!r} not found (DoesNotExist: no matching "
            "chat). Creating new chat.",
            _messages(records),
        )

    async def test_session_mismatch_logs_class_and_fixed_reason(self):
        user, existing = await _make_chat(
            "sizelog18", "9999940018", session_key="sizelog-session-18"
        )
        with _captured_logs() as (records, lines):
            chat = await OngoingChatConsumer()._get_or_create_chat(
                None,
                chat_type=ChatType.PATIENT,
                chat_id=str(existing.id),
                session_key="sizelog-other-session",
                email="sizelog18@example.com",
            )
        self.assertNotEqual(chat.id, existing.id)
        self.assertIn(
            f"Chat with id {str(existing.id)!r} not found (DoesNotExist: "
            "session key mismatch). Creating new chat.",
            _messages(records),
        )


class _AnalysisModel:
    """Chat backend stub for the denied-item analysis: one canned reply."""

    def __init__(self, reply=None, error=None):
        self._reply = reply
        self._error = error

    async def generate_chat_response(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._reply, None


class DeniedItemAnalysisLogsTest(APITestCase):
    async def _analyze(self, username, npi, model):
        user, chat = await _make_chat(
            username,
            npi,
            chat_history=[
                {"role": "user", "content": "My SENTINEL-history-11 was denied"},
                {"role": "assistant", "content": FRESH_REPLY},
            ],
        )
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends",
                return_value=[model],
            ):
                await OngoingChatConsumer()._analyze_denied_items(str(chat.id))
        self.assertEqual(_leaks(lines, "SENTINEL-history-11"), [])
        return chat, records, lines

    async def test_prompt_and_answer_logged_as_sizes(self):
        reply = (
            '{"denied_item": "SENTINEL-item-11 MRI", '
            '"denied_reason": "SENTINEL-reason-11 not necessary"}'
        )
        chat, records, lines = await self._analyze(
            "sizelog11", "9999940011", _AnalysisModel(reply=reply)
        )
        self.assertEqual(_leaks(lines, "SENTINEL-item-11"), [])
        self.assertEqual(_leaks(lines, "SENTINEL-reason-11"), [])
        messages = _messages(records)
        self.assertTrue(
            [
                m
                for m in messages
                if m.startswith(f"Denied item extraction for chat {chat.id}")
                and "prompt_chars=" in m
            ]
        )
        self.assertTrue(
            [
                m
                for m in messages
                if m.startswith(f"Analysis for chat {chat.id}: item_len=")
            ]
        )
        # The analysis still stored what it found.
        fresh = await OngoingChat.objects.aget(id=chat.id)
        self.assertIn("SENTINEL-item-11", fresh.denied_item or "")

    async def test_unparseable_answer_logged_as_size(self):
        reply = "{SENTINEL-answer-12 is not json}"
        chat, records, lines = await self._analyze(
            "sizelog12", "9999940012", _AnalysisModel(reply=reply)
        )
        self.assertEqual(_leaks(lines, "SENTINEL-answer-12"), [])
        self.assertIn(
            "Could not parse JSON from analysis response for chat "
            f"{chat.id} (response_chars={len(reply)})",
            _messages(records),
        )

    async def test_answer_without_json_logged_as_size(self):
        reply = "The item was SENTINEL-answer-13 and nothing more."
        chat, records, lines = await self._analyze(
            "sizelog13", "9999940013", _AnalysisModel(reply=reply)
        )
        self.assertEqual(_leaks(lines, "SENTINEL-answer-13"), [])
        self.assertIn(
            f"No JSON found in analysis response for chat {chat.id} "
            f"(response_chars={len(reply)})",
            _messages(records),
        )

    async def test_analysis_error_logs_class_name(self):
        chat, records, lines = await self._analyze(
            "sizelog14",
            "9999940014",
            _AnalysisModel(error=RuntimeError("SENTINEL-error-14")),
        )
        messages = _messages(records)
        self.assertFalse([m for m in messages if "SENTINEL-error-14" in m])
        self.assertIn(
            f"Error analyzing denied items for chat {chat.id}: RuntimeError",
            messages,
        )
