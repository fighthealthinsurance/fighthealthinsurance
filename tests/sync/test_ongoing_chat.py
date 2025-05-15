
    async def test_link_chat_to_appeal_success(self):
        """Test linking a chat to an appeal with permission."""
        await self.asyncSetUp()
        user = await sync_to_async(User.objects.create_user)(
            username="appealuser", password="testpass", email="appeal@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1111111111"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )
        from fighthealthinsurance.models import Appeal
        appeal = await sync_to_async(Appeal.objects.create)(
            creating_professional=professional,
            primary_professional=professional,
            hashed_email="hashappeal@example.com",
        )
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.send_json_to({
            "chat_id": str(chat.id),
            "iterate_on_appeal": appeal.id,
            "content": "Let's work on this appeal.",
        })
        response = await communicator.receive_json_from(timeout=15)
        self.assertIn("content", response)
        self.assertIn("happy to help you iterate on this appeal", response["content"])
        # Check system message in chat history
        chat_refresh = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
        self.assertTrue(any(
            m["role"] == "system" and "Linked this chat to Appeal" in m["content"]
            for m in chat_refresh.chat_history
        ))
        # Appeal is linked
        appeal_refresh = await sync_to_async(Appeal.objects.get)(id=appeal.id)
        self.assertEqual(appeal_refresh.chat_id, chat.id)
        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_link_chat_to_prior_auth_success(self):
        """Test linking a chat to a prior auth with permission."""
        await self.asyncSetUp()
        user = await sync_to_async(User.objects.create_user)(
            username="priorauthuser", password="testpass", email="priorauth@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="2222222222"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )
        from fighthealthinsurance.models import PriorAuthRequest
        prior_auth = await sync_to_async(PriorAuthRequest.objects.create)(
            creator_professional_user=professional,
            created_for_professional_user=professional,
            diagnosis="Test diagnosis",
            treatment="Test treatment",
            insurance_company="Test Insurance",
        )
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.send_json_to({
            "chat_id": str(chat.id),
            "iterate_on_prior_auth": str(prior_auth.id),
            "content": "Let's work on this prior auth.",
        })
        response = await communicator.receive_json_from(timeout=15)
        self.assertIn("content", response)
        self.assertIn("happy to help you iterate on this prior auth", response["content"])
        # Check system message in chat history
        chat_refresh = await sync_to_async(OngoingChat.objects.get)(id=chat.id)
        self.assertTrue(any(
            m["role"] == "system" and "Linked this chat to Prior Auth Request" in m["content"]
            for m in chat_refresh.chat_history
        ))
        # PriorAuthRequest is linked
        prior_auth_refresh = await sync_to_async(PriorAuthRequest.objects.get)(id=prior_auth.id)
        self.assertEqual(prior_auth_refresh.chat_id, chat.id)
        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_link_chat_to_appeal_permission_denied(self):
        """Test linking a chat to an appeal without permission is denied."""
        await self.asyncSetUp()
        user1 = await sync_to_async(User.objects.create_user)(
            username="user1", password="testpass", email="user1@example.com"
        )
        user2 = await sync_to_async(User.objects.create_user)(
            username="user2", password="testpass", email="user2@example.com"
        )
        professional1 = await sync_to_async(ProfessionalUser.objects.create)(
            user=user1, active=True, npi_number="3333333333"
        )
        professional2 = await sync_to_async(ProfessionalUser.objects.create)(
            user=user2, active=True, npi_number="4444444444"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional2,
            chat_history=[],
            summary_for_next_call=[],
        )
        from fighthealthinsurance.models import Appeal
        appeal = await sync_to_async(Appeal.objects.create)(
            creating_professional=professional1,
            primary_professional=professional1,
            hashed_email="hashappeal2@example.com",
        )
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user2
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.send_json_to({
            "chat_id": str(chat.id),
            "iterate_on_appeal": appeal.id,
            "content": "Try to link without permission.",
        })
        response = await communicator.receive_json_from(timeout=15)
        self.assertIn("error", response)
        self.assertIn("permission", response["error"])
        # Appeal is not linked
        appeal_refresh = await sync_to_async(Appeal.objects.get)(id=appeal.id)
        self.assertIsNone(appeal_refresh.chat_id)
        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_link_chat_to_prior_auth_permission_denied(self):
        """Test linking a chat to a prior auth without permission is denied."""
        await self.asyncSetUp()
        user1 = await sync_to_async(User.objects.create_user)(
            username="user3", password="testpass", email="user3@example.com"
        )
        user2 = await sync_to_async(User.objects.create_user)(
            username="user4", password="testpass", email="user4@example.com"
        )
        professional1 = await sync_to_async(ProfessionalUser.objects.create)(
            user=user1, active=True, npi_number="5555555555"
        )
        professional2 = await sync_to_async(ProfessionalUser.objects.create)(
            user=user2, active=True, npi_number="6666666666"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional2,
            chat_history=[],
            summary_for_next_call=[],
        )
        from fighthealthinsurance.models import PriorAuthRequest
        prior_auth = await sync_to_async(PriorAuthRequest.objects.create)(
            creator_professional_user=professional1,
            created_for_professional_user=professional1,
            diagnosis="Test diagnosis",
            treatment="Test treatment",
            insurance_company="Test Insurance",
        )
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user2
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.send_json_to({
            "chat_id": str(chat.id),
            "iterate_on_prior_auth": str(prior_auth.id),
            "content": "Try to link prior auth without permission.",
        })
        response = await communicator.receive_json_from(timeout=15)
        self.assertIn("error", response)
        self.assertIn("permission", response["error"])
        # PriorAuthRequest is not linked
        prior_auth_refresh = await sync_to_async(PriorAuthRequest.objects.get)(id=prior_auth.id)
        self.assertIsNone(prior_auth_refresh.chat_id)
        await communicator.disconnect()
        await self.asyncTearDown()
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
        self.assertEqual(response["error"], "User not authenticated.")

        # Disconnect
        await communicator.disconnect()

        # Clean up
        await self.asyncTearDown()
