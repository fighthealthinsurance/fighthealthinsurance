"""
Test that microsite context is fetched and integrated into chats.

This is a regression test for bugs where:
1. Chat history wasn't appended when it already existed
2. Microsite context fetching wasn't properly triggered
"""

import typing
from unittest.mock import patch, AsyncMock, MagicMock
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.models import OngoingChat, ProfessionalUser, Appeal
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class MicrositeChatIntegrationTest(APITestCase):
    """Test that microsite context is properly fetched for chats."""

    async def test_microsite_context_fetched_for_chat(self):
        """Verify that microsite.get_combined_context is called for chats with a microsite_slug."""
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock get_microsite
        get_microsite_patcher = patch(
            "fighthealthinsurance.chat_interface.get_microsite"
        )
        mock_get_microsite = get_microsite_patcher.start()

        mock_microsite = MagicMock()
        mock_microsite.slug = "test-microsite"
        mock_microsite.default_procedure = "Test Procedure"
        mock_microsite.extralinks = [{"url": "https://example.com/doc.pdf"}]
        mock_microsite.pubmed_search_terms = ["test"]
        mock_microsite.get_combined_context = AsyncMock(return_value="Mock context")
        mock_get_microsite.return_value = mock_microsite

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="testuser", password="testpass", email="test@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="1111111111"
            )

            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                microsite_slug="test-microsite",
                chat_history=[],
                summary_for_next_call=[],
            )

            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user

            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "Tell me about this"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify get_combined_context was called
            mock_microsite.get_combined_context.assert_called_once()

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            get_microsite_patcher.stop()


class ChatHistoryAppendingTest(APITestCase):
    """Test that chat history is appended correctly."""

    async def test_chat_history_appended_when_already_exists(self):
        """
        Test that chat history is appended even when it already contains messages.

        This is a regression test for the bug where history appending was inside
        the `if not chat.chat_history:` block.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Second response", "Summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="historyuser", password="testpass", email="history@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="1234567890"
            )

            # Create an appeal for linking
            appeal = await sync_to_async(Appeal.objects.create)(
                creating_professional=professional,
                primary_professional=professional,
                hashed_email="hashtest@example.com",
            )

            # Create a chat with EXISTING history
            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                chat_history=[
                    {
                        "role": "user",
                        "content": "First message",
                        "timestamp": "2025-01-01T10:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "First response",
                        "timestamp": "2025-01-01T10:01:00Z",
                    },
                ],
                summary_for_next_call=[],
            )

            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user

            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            # Send a message with appeal linking
            await communicator.send_json_to(
                {
                    "chat_id": str(chat.id),
                    "iterate_on_appeal": appeal.id,
                    "content": "Let's work on this appeal.",
                }
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify that chat history was appended (should now have 4 messages)
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                4,
                "Chat history should have been appended even though it already existed",
            )

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
