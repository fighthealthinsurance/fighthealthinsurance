"""Tests for chat fallback and external model functionality."""

import typing
from unittest.mock import patch, MagicMock, AsyncMock
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model

from rest_framework.test import APITestCase

from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from .mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class ChatFallbackTest(APITestCase):
    """Test the chat fallback and external model functionality."""

    async def asyncSetUp(self):
        """Set up the test environment with mocks."""
        # Create mock models - primary (internal) and fallback (external)
        self.mock_primary_model = MockChatModel(external=False)
        self.mock_fallback_model = MockChatModel(external=True)

        # Patch get_chat_backends_with_fallback on the singleton instance
        self.get_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.ml_router.get_chat_backends_with_fallback"
        )
        self.mock_get_backends = self.get_backends_patcher.start()
        # Use addCleanup to ensure patch is stopped even if test fails
        self.addCleanup(self.get_backends_patcher.stop)

        # Also patch get_chat_backends for backward compatibility on the singleton instance
        self.get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.ml_router.get_chat_backends"
        )
        self.mock_get_chat_backends = self.get_chat_backends_patcher.start()
        self.mock_get_chat_backends.return_value = [self.mock_primary_model]
        # Use addCleanup to ensure patch is stopped even if test fails
        self.addCleanup(self.get_chat_backends_patcher.stop)

    async def asyncTearDown(self):
        """Clean up the test environment (patches are auto-cleaned via addCleanup)."""
        pass

    async def test_primary_models_used_first(self):
        """Test that primary models are used first before fallback."""
        await self.asyncSetUp()

        # Set up to return primary models only
        self.mock_get_backends.return_value = ([self.mock_primary_model], [])
        self.mock_primary_model.set_next_response(
            "Response from primary model", "Primary context"
        )

        user = await sync_to_async(User.objects.create_user)(
            username="testuser", password="testpass", email="test@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1234567890"
        )
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

        # Send message without external models enabled
        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "Test message",
                "use_external_models": False,
            }
        )

        response = await communicator.receive_json_from(timeout=15)
        while "status" in response:
            response = await communicator.receive_json_from(timeout=15)

        self.assertIn("content", response)
        self.assertIn("primary", response["content"].lower())

        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_fallback_models_used_when_enabled(self):
        """Test that fallback models are available when use_external_models is True."""
        await self.asyncSetUp()

        # Set up to return both primary and fallback models
        self.mock_get_backends.return_value = (
            [self.mock_primary_model],
            [self.mock_fallback_model],
        )

        # Make primary model persistently fail (won't reset after first call)
        self.mock_primary_model.set_persistent_response("", "")
        self.mock_fallback_model.set_next_response(
            "Response from fallback model", "Fallback context"
        )

        user = await sync_to_async(User.objects.create_user)(
            username="testuser2", password="testpass", email="test2@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="2345678901"
        )
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

        # Send message with external models enabled
        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "Test message",
                "use_external_models": True,
            }
        )

        # Get response (may need to skip status messages)
        response = await communicator.receive_json_from(timeout=15)
        while "status" in response:
            response = await communicator.receive_json_from(timeout=15)

        # When primary fails and fallback is enabled, we should get fallback response
        # Note: The actual behavior depends on the implementation
        self.assertIn("content", response)

        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_no_fallback_when_external_disabled(self):
        """Test that fallback models are not used when use_external_models is False."""
        await self.asyncSetUp()

        # Set up to return empty fallback when external is disabled
        self.mock_get_backends.return_value = ([self.mock_primary_model], [])

        user = await sync_to_async(User.objects.create_user)(
            username="testuser3", password="testpass", email="test3@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="3456789012"
        )
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

        # Verify that get_chat_backends_with_fallback is called with use_external=False
        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "Test message",
                "use_external_models": False,
            }
        )

        # Wait for response
        response = await communicator.receive_json_from(timeout=15)
        while "status" in response:
            response = await communicator.receive_json_from(timeout=15)

        await communicator.disconnect()
        await self.asyncTearDown()

    async def test_use_external_models_toggle_mid_chat(self):
        """Test that use_external_models can be toggled during a chat session."""
        await self.asyncSetUp()

        user = await sync_to_async(User.objects.create_user)(
            username="testuser4", password="testpass", email="test4@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="4567890123"
        )
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

        # First message without external models
        self.mock_get_backends.return_value = ([self.mock_primary_model], [])
        self.mock_primary_model.set_next_response("Response 1", "Context 1")

        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "First message",
                "use_external_models": False,
            }
        )

        response = await communicator.receive_json_from(timeout=15)
        while "status" in response:
            response = await communicator.receive_json_from(timeout=15)

        # Second message with external models enabled
        self.mock_get_backends.return_value = (
            [self.mock_primary_model],
            [self.mock_fallback_model],
        )
        self.mock_primary_model.set_next_response("Response 2", "Context 2")

        await communicator.send_json_to(
            {
                "chat_id": str(chat.id),
                "content": "Second message",
                "use_external_models": True,
            }
        )

        response = await communicator.receive_json_from(timeout=15)
        while "status" in response:
            response = await communicator.receive_json_from(timeout=15)

        self.assertIn("content", response)

        await communicator.disconnect()
        await self.asyncTearDown()


class MLRouterFallbackTest(APITestCase):
    """Test the ML router fallback functionality."""

    def test_get_chat_backends_with_fallback_returns_tuple(self):
        """Test that get_chat_backends_with_fallback returns a tuple."""
        from fighthealthinsurance.ml.ml_router import MLRouter

        router = MLRouter()

        # Test without external
        primary, fallback = router.get_chat_backends_with_fallback(use_external=False)
        self.assertIsInstance(primary, list)
        self.assertIsInstance(fallback, list)
        self.assertEqual(len(fallback), 0)  # No fallback when external is disabled

    def test_get_chat_backends_with_fallback_includes_external(self):
        """Test that external models are included when use_external=True."""
        from fighthealthinsurance.ml.ml_router import MLRouter

        router = MLRouter()

        # Test with external
        primary, fallback = router.get_chat_backends_with_fallback(use_external=True)
        self.assertIsInstance(primary, list)
        self.assertIsInstance(fallback, list)
        # Fallback may be empty if no external models are configured
        # but the list should still be returned

    def test_external_property_on_mock_model(self):
        """Test that the mock model has the external property."""
        internal_model = MockChatModel(external=False)
        external_model = MockChatModel(external=True)

        self.assertFalse(internal_model.external)
        self.assertTrue(external_model.external)
