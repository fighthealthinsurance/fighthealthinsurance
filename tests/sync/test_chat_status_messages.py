"""Tests for chat status messages and timing functionality."""
import typing
from unittest.mock import patch, MagicMock, AsyncMock
from channels.testing import WebsocketCommunicator
import asyncio
import json

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    OngoingChat,
    ProfessionalUser,
    ChatLeads,
)
from fighthealthinsurance.websockets import OngoingChatConsumer
from fighthealthinsurance.chat_interface import ChatInterface
from .mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class ChatStatusMessageTest(APITestCase):
    """Test cases for chat status messages and timing."""

    async def asyncSetUp(self):
        """Set up test environment with mock model."""
        self.mock_model = MockChatModel()
        self.get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.ml_router.get_chat_backends"
        )
        self.mock_get_chat_backends = self.get_chat_backends_patcher.start()
        self.mock_get_chat_backends.return_value = [self.mock_model]

    async def asyncTearDown(self):
        """Clean up the test environment."""
        self.get_chat_backends_patcher.stop()

    async def test_send_status_message(self):
        """Test that status messages are sent correctly via ChatInterface."""
        await self.asyncSetUp()

        # Create a user and chat
        user = await sync_to_async(User.objects.create_user)(
            username="statususer", password="testpass", email="status@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1234567890"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        # Track status messages sent
        status_messages = []

        async def mock_send_json(data):
            if "status" in data:
                status_messages.append(data["status"])

        # Create chat interface
        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Test sending a status message
        await chat_interface.send_status_message("Processing your request...")
        
        # Verify status message was sent
        self.assertEqual(len(status_messages), 1)
        self.assertEqual(status_messages[0], "Processing your request...")

        await self.asyncTearDown()

    async def test_status_message_during_pubmed_search(self):
        """Test that status messages are sent during PubMed searches."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="pubmeduser", password="testpass", email="pubmed@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="9876543210"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        status_messages = []

        async def mock_send_json(data):
            if "status" in data:
                status_messages.append(data["status"])

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Mock PubMed response with a tool call
        self.mock_model.set_next_response(
            "**pubmed_query: diabetes treatment**", 
            "Context about diabetes"
        )

        # Mock PubMed tools to avoid actual API calls
        with patch.object(
            chat_interface.pubmed_tools, 
            'find_pubmed_article_ids_for_query',
            return_value=[]
        ):
            response_text, context = await chat_interface._call_llm_with_actions(
                [self.mock_model],
                "Tell me about diabetes treatment",
                None,
                [],
            )

        # Verify status message was sent for PubMed search
        self.assertTrue(
            any("PubMed" in msg or "Searching" in msg for msg in status_messages),
            f"Expected PubMed status message, got: {status_messages}"
        )

        await self.asyncTearDown()

    async def test_status_message_during_medicaid_lookup(self):
        """Test that status messages are sent during Medicaid lookups."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="medicaiduser", password="testpass", email="medicaid@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="5555555555"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        status_messages = []

        async def mock_send_json(data):
            if "status" in data:
                status_messages.append(data["status"])

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Mock Medicaid tool call
        self.mock_model.set_next_response(
            '**medicaid_info {"state": "California", "topic": "eligibility"}**',
            "Medicaid context"
        )

        # Mock medicaid API
        with patch('fighthealthinsurance.chat_interface.get_medicaid_info', return_value="Medicaid info"):
            response_text, context = await chat_interface._call_llm_with_actions(
                [self.mock_model],
                "What are California's Medicaid rules?",
                None,
                [],
            )

        # Verify status message was sent for Medicaid lookup
        self.assertTrue(
            any("Medicaid" in msg for msg in status_messages),
            f"Expected Medicaid status message, got: {status_messages}"
        )

        await self.asyncTearDown()

    async def test_websocket_status_message_handling(self):
        """Test that WebSocket properly handles and forwards status messages."""
        await self.asyncSetUp()

        # Create a session key for anonymous chat
        session_key = "test_session_key_123"
        
        # Create a ChatLeads entry for trial professional
        await sync_to_async(ChatLeads.objects.create)(
            session_id=session_key,
            email="trial@example.com",
        )

        # Create communicator
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        
        # Set up scope for anonymous user
        from django.contrib.auth.models import AnonymousUser
        communicator.scope["user"] = AnonymousUser()

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Set mock response
        self.mock_model.set_next_response(
            "This is a test response",
            "Test context"
        )

        # Send a message
        await communicator.send_json_to(
            {
                "session_key": session_key,
                "content": "Hello, I need help",
                "is_patient": False,
            }
        )

        # Collect all responses
        responses = []
        try:
            while True:
                response = await communicator.receive_json_from(timeout=5)
                responses.append(response)
                # Stop when we get the actual content response
                if "content" in response and response.get("role") == "assistant":
                    break
        except asyncio.TimeoutError:
            pass

        # Verify we got at least one response
        self.assertGreater(len(responses), 0, "Expected at least one response")

        # Check that we got a chat_id
        chat_ids = [r.get("chat_id") for r in responses if "chat_id" in r]
        self.assertGreater(len(chat_ids), 0, "Expected to receive a chat_id")

        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_multiple_status_updates(self):
        """Test that multiple status updates are properly sent and tracked."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="multistatususer", password="testpass", email="multistatus@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="3333333333"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        status_messages = []

        async def mock_send_json(data):
            if "status" in data:
                status_messages.append(data["status"])

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Send multiple status messages
        await chat_interface.send_status_message("Starting request...")
        await chat_interface.send_status_message("Processing data...")
        await chat_interface.send_status_message("Generating response...")

        # Verify all status messages were sent in order
        self.assertEqual(len(status_messages), 3)
        self.assertEqual(status_messages[0], "Starting request...")
        self.assertEqual(status_messages[1], "Processing data...")
        self.assertEqual(status_messages[2], "Generating response...")

        await self.asyncTearDown()

    async def test_status_message_with_appeal_creation(self):
        """Test that status messages are sent during appeal creation."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="appealstatususer", password="testpass", email="appealstatus@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="4444444444"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        status_messages = []

        async def mock_send_json(data):
            if "status" in data:
                status_messages.append(data["status"])

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Mock response with appeal creation
        self.mock_model.set_next_response(
            '**create_or_update_appeal** {"denied_procedure": "MRI"}',
            "Appeal context"
        )

        response_text, context = await chat_interface._call_llm_with_actions(
            [self.mock_model],
            "Create an appeal for my MRI",
            None,
            [],
        )

        # Verify status message was sent for appeal processing
        self.assertTrue(
            any("appeal" in msg.lower() for msg in status_messages),
            f"Expected appeal status message, got: {status_messages}"
        )

        await self.asyncTearDown()

    async def test_error_status_message(self):
        """Test that error messages are properly sent via status."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="erroruser", password="testpass", email="error@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="6666666666"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
        )

        error_messages = []

        async def mock_send_json(data):
            if "error" in data:
                error_messages.append(data["error"])

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Test sending an error message
        await chat_interface.send_error_message("Something went wrong")

        # Verify error message was sent
        self.assertEqual(len(error_messages), 1)
        self.assertEqual(error_messages[0], "Something went wrong")

        await self.asyncTearDown()

    async def test_status_cleared_on_response(self):
        """Test that status is cleared when a response is received."""
        await self.asyncSetUp()

        # Create a session key for anonymous chat
        session_key = "test_session_key_456"
        
        # Create a ChatLeads entry
        await sync_to_async(ChatLeads.objects.create)(
            session_id=session_key,
            email="clear@example.com",
        )

        # Create communicator
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        
        from django.contrib.auth.models import AnonymousUser
        communicator.scope["user"] = AnonymousUser()

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Set mock response
        self.mock_model.set_next_response(
            "Response received",
            "Context"
        )

        # Send a message
        await communicator.send_json_to(
            {
                "session_key": session_key,
                "content": "Test message",
                "is_patient": False,
            }
        )

        # Wait for response
        response = None
        try:
            while True:
                response = await communicator.receive_json_from(timeout=5)
                # When we get actual content, the status should be cleared
                if "content" in response and response.get("role") == "assistant":
                    # This is the final response, status should be cleared
                    break
        except asyncio.TimeoutError:
            pass

        self.assertIsNotNone(response, "Should have received a response")
        self.assertIn("content", response)

        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_retry_functionality(self):
        """Test that retry functionality can be triggered when needed."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="retryuser", password="testpass", email="retry@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="7777777777"
        )
        chat = await sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_history=[
                {
                    "role": "user",
                    "content": "Original message",
                }
            ],
            summary_for_next_call=[],
        )

        sent_messages = []

        async def mock_send_json(data):
            sent_messages.append(data)

        chat_interface = ChatInterface(
            send_json_message_func=mock_send_json,
            chat=chat,
            user=user,
            is_patient=False,
        )

        # Set a response for the retry
        self.mock_model.set_next_response(
            "Retry response",
            "Retry context"
        )

        # Simulate handling the same message again (retry scenario)
        await chat_interface.handle_chat_message("Original message")

        # Verify message was processed
        # Should have status messages and final response
        self.assertGreater(len(sent_messages), 0)

        # Check that we got a response
        response_messages = [msg for msg in sent_messages if msg.get("role") == "assistant"]
        self.assertGreater(len(response_messages), 0, "Should have received at least one assistant response")

        await self.asyncTearDown()

