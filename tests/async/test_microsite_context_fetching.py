"""
Tests for microsite context fetching in chats.

These are regression tests for bugs where:
1. Microsite context wasn't fetched when it should have been
2. fire_and_forget wasn't called correctly
"""

import asyncio
import typing
from unittest.mock import patch, AsyncMock, MagicMock
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model

from rest_framework.test import APITestCase

from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

from asgiref.sync import sync_to_async

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class MicrositeContextFetchingTest(APITestCase):
    """Test that microsite context is fetched correctly."""

    async def test_extralinks_only_microsite_fetches_context(self):
        """
        Test that when a microsite has only extralinks, get_combined_context is called.

        This is a regression test for bugs where microsite context wasn't fetched.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget to track if it's called and actually execute the coroutine
        async def mock_fire_and_forget_impl(coro):
            """Execute the coroutine instead of ignoring it."""
            return await coro

        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            side_effect=mock_fire_and_forget_impl
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()

        # Mock get_microsite
        get_microsite_patcher = patch(
            "fighthealthinsurance.chat_interface.get_microsite"
        )
        mock_get_microsite = get_microsite_patcher.start()

        mock_microsite = MagicMock()
        mock_microsite.slug = "test-microsite"
        mock_microsite.default_procedure = "Test Procedure"
        mock_microsite.extralinks = [
            {"url": "https://example.com/doc1.pdf", "title": "Test Document"}
        ]
        mock_microsite.pubmed_search_terms = []
        mock_microsite.get_combined_context = AsyncMock(return_value="Extralink context here")
        mock_get_microsite.return_value = mock_microsite

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="extralinkuser", password="testpass", email="extralink@example.com"
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
                {"chat_id": str(chat.id), "content": "Tell me about this treatment"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify fire_and_forget was called
            mock_fire_and_forget.assert_called_once()

            # Verify the coroutine passed to fire_and_forget is the fetch_microsite_context function
            call_args = mock_fire_and_forget.call_args[0][0]
            self.assertTrue(asyncio.iscoroutine(call_args))
            self.assertIn("fetch_microsite_context", call_args.__name__)

            # Verify get_combined_context was called on the microsite
            mock_microsite.get_combined_context.assert_called_once()
            call_kwargs = mock_microsite.get_combined_context.call_args[1]
            self.assertIsNone(call_kwargs["pubmed_tools"], "pubmed_tools should be None when no pubmed_search_terms")
            self.assertEqual(call_kwargs["max_extralink_docs"], 5)
            self.assertEqual(call_kwargs["max_extralink_chars"], 2000)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()

    async def test_pubmed_only_microsite_fetches_context(self):
        """
        Test that when a microsite has only pubmed_search_terms, get_combined_context is called with pubmed_tools.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget to track if it's called and actually execute the coroutine
        async def mock_fire_and_forget_impl(coro):
            """Execute the coroutine instead of ignoring it."""
            return await coro

        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            side_effect=mock_fire_and_forget_impl
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()

        get_microsite_patcher = patch(
            "fighthealthinsurance.chat_interface.get_microsite"
        )
        mock_get_microsite = get_microsite_patcher.start()

        mock_microsite = MagicMock()
        mock_microsite.slug = "test-pubmed-microsite"
        mock_microsite.default_procedure = "Asthma Treatment"
        mock_microsite.extralinks = []
        mock_microsite.pubmed_search_terms = ["asthma inhaler", "bronchodilator"]
        mock_microsite.get_combined_context = AsyncMock(return_value="PubMed context here")
        mock_get_microsite.return_value = mock_microsite

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="pubmeduser", password="testpass", email="pubmed@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="2222222222"
            )

            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                microsite_slug="test-pubmed-microsite",
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
                {"chat_id": str(chat.id), "content": "Tell me about asthma treatment"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify fire_and_forget was called
            mock_fire_and_forget.assert_called_once()

            # Verify get_combined_context was called with pubmed_tools
            mock_microsite.get_combined_context.assert_called_once()
            call_kwargs = mock_microsite.get_combined_context.call_args[1]
            self.assertIsNotNone(call_kwargs["pubmed_tools"], "pubmed_tools should be provided when pubmed_search_terms exist")
            self.assertEqual(call_kwargs["max_pubmed_terms"], 3)
            self.assertEqual(call_kwargs["max_pubmed_articles"], 20)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()

    async def test_both_extralinks_and_pubmed_microsite_fetches_combined_context(self):
        """
        Test that when a microsite has both extralinks and pubmed, get_combined_context handles both.

        This is a regression test for the bug where tasks weren't properly collected together.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget to track if it's called and actually execute the coroutine
        async def mock_fire_and_forget_impl(coro):
            """Execute the coroutine instead of ignoring it."""
            return await coro

        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            side_effect=mock_fire_and_forget_impl
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()

        get_microsite_patcher = patch(
            "fighthealthinsurance.chat_interface.get_microsite"
        )
        mock_get_microsite = get_microsite_patcher.start()

        mock_microsite = MagicMock()
        mock_microsite.slug = "test-both-microsite"
        mock_microsite.default_procedure = "Test Treatment"
        mock_microsite.extralinks = [
            {"url": "https://example.com/doc.pdf", "title": "Doc"}
        ]
        mock_microsite.pubmed_search_terms = ["test treatment"]
        mock_microsite.get_combined_context = AsyncMock(return_value="Extralink context\n\nPubMed context")
        mock_get_microsite.return_value = mock_microsite

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="bothuser", password="testpass", email="both@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="3333333333"
            )

            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                microsite_slug="test-both-microsite",
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

            # Verify fire_and_forget was called ONCE (not twice)
            # This catches the bug where separate tasks would cause multiple calls
            mock_fire_and_forget.assert_called_once()

            # Verify get_combined_context was called with BOTH pubmed_tools and extralink params
            mock_microsite.get_combined_context.assert_called_once()
            call_kwargs = mock_microsite.get_combined_context.call_args[1]
            self.assertIsNotNone(call_kwargs["pubmed_tools"])
            self.assertEqual(call_kwargs["max_extralink_docs"], 5)
            self.assertEqual(call_kwargs["max_pubmed_terms"], 3)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()

    async def test_no_microsite_slug_no_context_fetched(self):
        """
        Test that when a chat has no microsite_slug, no microsite context is fetched.

        This ensures we don't waste resources on non-microsite chats.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget to track if it's called and actually execute the coroutine
        async def mock_fire_and_forget_impl(coro):
            """Execute the coroutine instead of ignoring it."""
            return await coro

        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            side_effect=mock_fire_and_forget_impl
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()

        try:
            user = await sync_to_async(User.objects.create_user)(
                username="nomicrositeuser", password="testpass", email="nomicrosite@example.com"
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="4444444444"
            )

            chat = await sync_to_async(OngoingChat.objects.create)(
                professional_user=professional,
                microsite_slug=None,  # No microsite
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
                {"chat_id": str(chat.id), "content": "Tell me about appeals"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Verify fire_and_forget was NOT called since there's no microsite
            mock_fire_and_forget.assert_not_called()

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
