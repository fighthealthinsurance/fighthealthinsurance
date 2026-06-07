"""
Integration tests for long pasted messages in the ongoing chat flow.

Verifies end-to-end (through the WebSocket consumer) that:
1. A huge paste is preserved in document storage and replaced in chat history
   with a compact marker (history stays bounded).
2. The full text is never fanned out to the model backends.
3. A normal short message still takes the original/primary path unchanged.
"""

import contextlib
import typing
from itertools import pairwise
from unittest.mock import AsyncMock, patch

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from asgiref.sync import sync_to_async

from fighthealthinsurance.chat.message_preprocessor import (
    DIRECT_CHAT_HARD_LIMIT_CHARS,
)
from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class RecordingMockModel(MockChatModel):
    """Mock model that records every message it is asked to generate against."""

    def __init__(self):
        super().__init__()
        self.received_messages: list[str] = []
        self.set_persistent_response(
            "Here is some guidance about your denial and next steps.",
            "Summary: discussed the denial.",
        )

    async def generate_chat_response(self, message, **kwargs):
        self.received_messages.append(message)
        return await super().generate_chat_response(message, **kwargs)


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


@contextlib.contextmanager
def _patched_backends(mock_model):
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
        yield stack


async def _drain_to_content(communicator):
    response = await communicator.receive_json_from(timeout=20)
    while "status" in response:
        response = await communicator.receive_json_from(timeout=20)
    return response


class LongPasteChatTest(APITestCase):
    async def test_huge_paste_is_stored_and_history_stays_compact(self):
        big = (
            "This claim was denied because the requested service is considered "
            "not medically necessary. "
        ) * 700  # ~63k chars, well over the hard limit
        self.assertGreater(len(big), DIRECT_CHAT_HARD_LIMIT_CHARS)

        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ) as mock_store:
                user, chat = await _make_professional_chat("longpaste1", "9999900001")

                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": big}
                    )
                    response = await _drain_to_content(communicator)
                    self.assertIn("content", response)
                finally:
                    await communicator.disconnect()

                # The full original text was preserved via document storage.
                mock_store.assert_awaited_once()
                store_kwargs = mock_store.await_args.kwargs
                self.assertEqual(len(store_kwargs["full_text"]), len(big))
                self.assertTrue(
                    store_kwargs["document_name"].startswith("pasted_message_")
                )

                # Chat history stores a compact marker, not the 63k blob.
                await chat.arefresh_from_db()
                user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
                self.assertEqual(len(user_msgs), 1)
                stored = user_msgs[0]["content"]
                self.assertLess(len(stored), 500)
                self.assertIn("stored for reference", stored.lower())
                self.assertNotIn(big, stored)

                # The full text was never fanned out to the backend.
                self.assertTrue(mock_model.received_messages)
                for msg in mock_model.received_messages:
                    self.assertNotIn(big, msg)
                    self.assertLess(len(msg), len(big))

    async def test_message_alternation_after_huge_paste(self):
        big = "Denied as experimental treatment. " * 1000  # ~34k chars
        self.assertGreater(len(big), DIRECT_CHAT_HARD_LIMIT_CHARS)

        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ):
                user, chat = await _make_professional_chat("longpaste2", "9999900002")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": big}
                    )
                    await _drain_to_content(communicator)
                finally:
                    await communicator.disconnect()

                await chat.arefresh_from_db()
                roles = [m.get("role") for m in chat.chat_history]
                # Alternation: no two consecutive messages share a role.
                for prev, nxt in pairwise(roles):
                    self.assertNotEqual(prev, nxt)
                self.assertEqual(roles[-1], "assistant")


class NormalMessagePrimaryPathTest(APITestCase):
    async def test_short_message_stored_verbatim_and_not_routed_to_storage(self):
        msg = "Why was my physical therapy claim denied?"
        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ) as mock_store:
                user, chat = await _make_professional_chat("normalmsg1", "9999900003")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": msg}
                    )
                    response = await _drain_to_content(communicator)
                    self.assertIn("content", response)
                finally:
                    await communicator.disconnect()

                # No long-paste storage for a normal message.
                mock_store.assert_not_awaited()

                # The raw message is stored verbatim (primary path).
                await chat.arefresh_from_db()
                user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
                self.assertEqual(len(user_msgs), 1)
                self.assertEqual(user_msgs[0]["content"], msg)
