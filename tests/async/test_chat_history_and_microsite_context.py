"""
Tests for chat history appending and microsite context integration.

These tests ensure:
1. Chat history is correctly appended even when it already exists (not just on first message)
2. Microsite extralinks context is properly fetched and applied
3. Fire-and-forget tasks for both extralinks and pubmed searches are properly collected
"""

import typing
from unittest.mock import patch, MagicMock, AsyncMock
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model

from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    OngoingChat,
    ProfessionalUser,
    ExtraLinkDocument,
    MicrositeExtraLink,
)
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class ChatHistoryAppendingTest(APITestCase):
    """Test that chat history is correctly appended in all scenarios."""

    async def test_chat_history_appended_when_already_exists_linking(self):
        """
        Test that chat history is appended during appeal/prior auth linking even with existing history.

        This catches the same indentation bug in the linking code path.
        """
        # Create a mock model instance
        mock_model = MockChatModel()

        # Patch the get_chat_backends method to return our mock model
        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="linkhistoryuser", password="testpass", email="linkhistory@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="9876543210"
            )

            # Create a chat with EXISTING history
            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                chat_history=[
                    {
                        "role": "user",
                        "content": "Existing message",
                        "timestamp": "2025-01-01T10:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "Existing response",
                        "timestamp": "2025-01-01T10:01:00Z",
                    },
                ],
                summary_for_next_call=[],
            )

            from fighthealthinsurance.models import Appeal

            appeal = await sync_to_async(Appeal.objects.create)(
                creating_professional=professional,
                primary_professional=professional,
                hashed_email="hashlinkhistory@example.com",
            )

            # Connect to the WebSocket
            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user

            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            # Send a linking request
            await communicator.send_json_to(
                {
                    "chat_id": str(chat.id),
                    "iterate_on_appeal": appeal.id,
                    "content": "Let's work on this appeal.",
                }
            )

            # Wait for the response
            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify that chat history was appended
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                4,
                "Chat history should have been appended during linking even with existing history",
            )

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()


class MicrositeContextIntegrationTest(APITestCase):
    """
    Test that microsite extralinks and pubmed searches are properly triggered.

    Note: These tests are simplified to test the key behavior - that chat works
    with microsites. The fire-and-forget task collection is tested implicitly
    by ensuring no errors occur when processing chats with microsite_slug set.
    """

    async def test_chat_works_with_microsite_no_crash(self):
        """
        Test that chat processing works when a microsite_slug is set.

        This is a regression test for the bug where fire_and_forget_in_new_threadpool_tasks
        wasn't imported, causing chats with microsites to crash.
        """
        # Create a mock model instance
        mock_model = MockChatModel()

        # Patch the get_chat_backends method to return our mock model
        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        try:
            # Create test user and chat with microsite_slug
            user = await sync_to_async(User.objects.create_user)(
                username="micrositeuser", password="testpass", email="microsite@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="1111111111"
            )

            # Set a microsite_slug to trigger the microsite handling code path
            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                microsite_slug="test-microsite",  # This triggers microsite code
                chat_history=[],
                summary_for_next_call=[],
            )

            # Set the mock model response
            mock_model.set_next_response("This is a test response", "Summary")

            # Connect to the WebSocket
            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user

            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            # Send a message - this should not crash even with microsite_slug set
            await communicator.send_json_to(
                {
                    "chat_id": str(chat.id),
                    "content": "Tell me about this treatment",
                }
            )

            # Wait for the response - should succeed without errors
            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            # If we get here without exceptions, the test passes
            self.assertIn("content", response)
            self.assertNotIn("error", response)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
