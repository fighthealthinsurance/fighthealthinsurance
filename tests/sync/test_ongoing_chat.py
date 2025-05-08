"""Test the ongoing chat functionality"""

import typing
from unittest.mock import patch
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model

from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    OngoingChat,
    ProfessionalUser,
    UserDomain,
    ExtraUserProperties,
)
from fighthealthinsurance.websockets import OngoingChatConsumer
from fhi_users.models import ProfessionalDomainRelation
from .mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async, async_to_sync

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class OngoingChatWebSocketTest(APITestCase):
    """Test the WebSocket endpoints for ongoing chat."""

    async def asyncSetUp(self):
        """Set up the test environment with mocks."""
        # Create a mock model instance
        self.mock_model = MockChatModel()

        # Patch the get_chat_backends method to return our mock model
        self.get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        self.mock_get_chat_backends = self.get_chat_backends_patcher.start()
        self.mock_get_chat_backends.return_value = [self.mock_model]

    async def asyncTearDown(self):
        """Clean up the test environment."""
        self.get_chat_backends_patcher.stop()

    async def test_ongoing_chat_websocket(self):
        """Test that the ongoing chat WebSocket connection works and generates responses."""
        # Set up the async environment
        await self.asyncSetUp()

        # Create a user
        user = await sync_to_async(User.objects.create_user)(
            username="testuser", password="testpass", email="test@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1234567890"
        )

        # Create a chat
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[
                {
                    "role": "user",
                    "content": "How do I appeal a denied claim for an MRI?",
                    "timestamp": "2025-01-01T12:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "To appeal a denied MRI claim, you'll need to gather the denial letter, medical records supporting necessity, and a letter from the referring physician. Submit these with an appeal letter citing specific insurance policy provisions.",
                    "timestamp": "2025-01-01T12:01:00Z",
                },
            ],
            summary_for_next_call=[
                "Professional is asking about appealing a denied MRI claim. I provided basic appeal steps."
            ],
        )

        # Connect to the WebSocket with authenticated user
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )

        # Add the user to the scope
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send a new message
        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "What specific codes should I reference for the MRI denial?",
            }
        )

        # Wait for a response
        response = await communicator.receive_json_from(timeout=15)

        # Verify the response contains expected fields
        self.assertIn("chat_id", response)
        self.assertIn("content", response)
        self.assertEqual(response["chat_id"], str(chat.id))
        self.assertIsNotNone(response["content"])

        # Disconnect
        await communicator.disconnect()

        # Verify message was added to chat history
        chat = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
        self.assertEqual(
            len(chat.chat_history), 4
        )  # Original 2 messages + new user message + assistant response
        self.assertEqual(chat.chat_history[-2]["role"], "user")
        self.assertEqual(
            chat.chat_history[-2]["content"],
            "What specific codes should I reference for the MRI denial?",
        )
        self.assertEqual(chat.chat_history[-1]["role"], "assistant")

        # Verify context summary was updated
        self.assertIsNotNone(chat.summary_for_next_call)
        self.assertIn(
            "Professional is asking about appealing a denied MRI claim. I provided basic appeal steps.",
            chat.summary_for_next_call,
        )

        # Clean up
        await self.asyncTearDown()

    async def test_start_new_chat(self):
        """Test starting a new chat without providing a chat ID."""
        # Set up the async environment
        await self.asyncSetUp()

        # Create a user
        user = await sync_to_async(User.objects.create_user)(
            username="testuser", password="testpass", email="test@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1234567890"
        )

        # Connect to the WebSocket with authenticated user
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )

        # Add the user to the scope
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send a message without a chat_id
        await communicator.send_json_to(
            {"content": "How do I check if a procedure requires prior authorization?"}
        )

        # Wait for a response
        response = await communicator.receive_json_from(timeout=15)

        # Verify the response contains a new chat_id
        self.assertIn("chat_id", response)
        self.assertIn("content", response)
        self.assertIsNotNone(response["chat_id"])
        self.assertIsNotNone(response["content"])

        # Disconnect
        await communicator.disconnect()

        # Verify a new chat was created
        chat_id = response["chat_id"]
        chat_exists = await OngoingChat.objects.filter(id=chat_id).aexists()
        self.assertTrue(chat_exists)

        # Verify message was added to chat history
        chat = await sync_to_async(OngoingChat.objects.get)(id=chat_id)
        self.assertEqual(len(chat.chat_history), 2)  # User message + assistant response
        self.assertEqual(chat.chat_history[0]["role"], "user")
        self.assertEqual(
            chat.chat_history[0]["content"],
            "How do I check if a procedure requires prior authorization?",
        )
        self.assertEqual(chat.chat_history[1]["role"], "assistant")

        # Verify replay works, we need to make a new connection.
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )

        # Add the user to the scope
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send a message without a chat_id
        await communicator.send_json_to({"replay": True, "chat_id": chat_id})

        # Wait for a response
        response = await communicator.receive_json_from(timeout=15)
        self.assertIn("chat_id", response)
        self.assertIn("messages", response)

        # Clean up
        await self.asyncTearDown()

    async def test_authentication_required(self):
        """Test that authentication is required for the chat WebSocket."""
        # Set up the async environment
        await self.asyncSetUp()

        # Connect to the WebSocket without an authenticated user
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )

        # Add unauthenticated user to the scope
        communicator.scope["user"] = None

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send a message
        await communicator.send_json_to(
            {"message": "This should fail due to authentication"}
        )

        # Should receive an error response
        response = await communicator.receive_json_from(timeout=5)
        self.assertIn("error", response)
        self.assertEqual(response["error"], "Authentication required")

        # Disconnect
        await communicator.disconnect()

        # Clean up
        await self.asyncTearDown()
