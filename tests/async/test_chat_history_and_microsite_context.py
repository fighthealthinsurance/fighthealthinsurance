"""
Tests for chat history persistence in various scenarios.

These tests verify that chat history is correctly saved when:
1. Crisis keywords are detected (crisis response handling)
2. Appeal/prior auth linking occurs
3. Chat history already exists before the new message

This is important because messages must be persisted regardless of
whether chat_history is empty or already has messages.
"""

import typing
from unittest.mock import patch, AsyncMock, MagicMock
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.models import OngoingChat, ProfessionalUser, Appeal, Denial
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class CrisisResponseHistoryTest(APITestCase):
    """Test that crisis response messages are persisted to chat history."""

    async def test_crisis_response_saved_when_history_empty(self):
        """
        Test that crisis response is saved to chat history when history is empty.

        When a user sends a crisis-related message and chat_history is empty,
        both the user message and crisis response should be saved.
        """
        user = await sync_to_async(User.objects.create_user)(
            username="crisisuser1", password="testpass", email="crisis1@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1111111112"
        )

        # Create a chat with empty history
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        try:
            # Send a message that triggers crisis detection
            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "I want to kill myself"}
            )

            # Receive the crisis response
            response = await communicator.receive_json_from(timeout=15)
            # Skip status messages
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)
            self.assertIn("988", response["content"])  # Crisis hotline number

            # Verify chat history was saved
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                2,
                "Chat history should have 2 messages (user + assistant crisis response)",
            )

            # Verify the user message was saved
            self.assertEqual(chat_refreshed.chat_history[0]["role"], "user")
            self.assertEqual(
                chat_refreshed.chat_history[0]["content"], "I want to kill myself"
            )

            # Verify the crisis response was saved
            self.assertEqual(chat_refreshed.chat_history[1]["role"], "assistant")
            self.assertIn("988", chat_refreshed.chat_history[1]["content"])

        finally:
            await communicator.disconnect()

    async def test_crisis_response_saved_when_history_exists(self):
        """
        Test that crisis response is saved to chat history when history already has messages.

        This is a regression test to ensure that when chat_history is NOT empty,
        new messages are still appended and saved.
        """
        user = await sync_to_async(User.objects.create_user)(
            username="crisisuser2", password="testpass", email="crisis2@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="2222222223"
        )

        # Create a chat with EXISTING history
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[
                {
                    "role": "user",
                    "content": "Hello, I need help with my insurance",
                    "timestamp": "2025-01-01T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "I'd be happy to help you with your insurance appeal.",
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

        try:
            # Send a message that triggers crisis detection
            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "I want to hurt myself"}
            )

            # Receive the crisis response
            response = await communicator.receive_json_from(timeout=15)
            # Skip status messages
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify chat history was appended (should now have 4 messages)
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                4,
                "Chat history should have 4 messages (2 existing + 2 new)",
            )

            # Verify original messages are still there
            self.assertEqual(
                chat_refreshed.chat_history[0]["content"],
                "Hello, I need help with my insurance",
            )
            self.assertEqual(
                chat_refreshed.chat_history[1]["content"],
                "I'd be happy to help you with your insurance appeal.",
            )

            # Verify new messages were appended
            self.assertEqual(chat_refreshed.chat_history[2]["role"], "user")
            self.assertEqual(
                chat_refreshed.chat_history[2]["content"], "I want to hurt myself"
            )
            self.assertEqual(chat_refreshed.chat_history[3]["role"], "assistant")

        finally:
            await communicator.disconnect()


class LinkMessageHistoryTest(APITestCase):
    """Test that appeal/prior auth link messages are persisted to chat history."""

    async def test_link_message_saved_when_history_empty(self):
        """
        Test that link message is saved when linking an appeal to a chat with empty history.
        """
        user = await sync_to_async(User.objects.create_user)(
            username="linkuser1", password="testpass", email="link1@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="3333333334"
        )

        # Create a denial first (required for appeal)
        denial = await sync_to_async(Denial.objects.create)(
            creating_professional=professional,
        )

        # Create an appeal for linking
        appeal = await sync_to_async(Appeal.objects.create)(
            creating_professional=professional,
            primary_professional=professional,
            hashed_email="linkhash1@example.com",
            for_denial=denial,
        )

        # Create a chat with empty history
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        try:
            # Send a message with appeal linking
            await communicator.send_json_to(
                {
                    "chat_id": str(chat.id),
                    "iterate_on_appeal": appeal.id,
                    "content": "",  # Empty content for linking
                }
            )

            # Receive the response
            response = await communicator.receive_json_from(timeout=15)
            # Skip status messages
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)
            self.assertIn("linked", response["content"].lower())

            # Verify chat history was saved
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                2,
                "Chat history should have 2 messages (link message + response)",
            )

        finally:
            await communicator.disconnect()

    async def test_link_message_saved_when_history_exists(self):
        """
        Test that link message is saved when linking an appeal to a chat with existing history.

        This is a regression test to ensure that when chat_history is NOT empty,
        new link messages are still appended and saved.
        """
        user = await sync_to_async(User.objects.create_user)(
            username="linkuser2", password="testpass", email="link2@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="4444444445"
        )

        # Create a denial first (required for appeal)
        denial = await sync_to_async(Denial.objects.create)(
            creating_professional=professional,
        )

        # Create an appeal for linking
        appeal = await sync_to_async(Appeal.objects.create)(
            creating_professional=professional,
            primary_professional=professional,
            hashed_email="linkhash2@example.com",
            for_denial=denial,
        )

        # Create a chat with EXISTING history
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[
                {
                    "role": "user",
                    "content": "Previous conversation",
                    "timestamp": "2025-01-01T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Previous response",
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

        try:
            # Send a message with appeal linking
            await communicator.send_json_to(
                {
                    "chat_id": str(chat.id),
                    "iterate_on_appeal": appeal.id,
                    "content": "",
                }
            )

            # Receive the response
            response = await communicator.receive_json_from(timeout=15)
            # Skip status messages
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify chat history was appended (should now have 4 messages)
            chat_refreshed = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
            self.assertEqual(
                len(chat_refreshed.chat_history),
                4,
                "Chat history should have 4 messages (2 existing + 2 new link messages)",
            )

            # Verify original messages are still there
            self.assertEqual(
                chat_refreshed.chat_history[0]["content"], "Previous conversation"
            )
            self.assertEqual(
                chat_refreshed.chat_history[1]["content"], "Previous response"
            )

        finally:
            await communicator.disconnect()
