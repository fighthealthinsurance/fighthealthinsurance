"""
Tests for microsite extralinks and pubmed context fetching.

These tests verify that the tasks are created and passed to fire_and_forget correctly,
catching the indentation bugs that were fixed in commits 1cd51d42 and ff2d0af.
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


class MicrositeExtralinksTaskTest(APITestCase):
    """Test that extralinks tasks are created and collected properly."""

    async def test_extralinks_task_created_and_passed_to_fire_and_forget(self):
        """
        Verify that when a microsite has extralinks, the fetch task is created and passed to fire_and_forget.

        This is a regression test for the indentation bug where the task wasn't properly collected.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget to track what tasks are passed to it
        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool_tasks"
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()
        mock_fire_and_forget.return_value = AsyncMock()

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
        # Mock the async method get_combined_context
        mock_microsite.get_combined_context = AsyncMock(return_value="Mock combined context")
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

            # Verify fire_and_forget was called with microsite context task
            mock_fire_and_forget.assert_called_once()
            call_args = mock_fire_and_forget.call_args[0][0]

            self.assertIsInstance(call_args, list)
            self.assertEqual(len(call_args), 1, "Should have exactly 1 task (microsite context)")

            task = call_args[0]
            self.assertTrue(asyncio.iscoroutine(task))
            self.assertIn("fetch_microsite_context", task.__name__)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()


class MicrositePubmedTaskTest(APITestCase):
    """Test that pubmed search tasks are created and collected properly."""

    async def test_pubmed_task_created_and_passed_to_fire_and_forget(self):
        """
        Verify that when a microsite has pubmed_search_terms, the search task is created and passed to fire_and_forget.

        This is a regression test for the indentation bug where pubmed tasks weren't properly collected.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget
        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool_tasks"
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()
        mock_fire_and_forget.return_value = AsyncMock()

        # Mock get_microsite
        get_microsite_patcher = patch(
            "fighthealthinsurance.chat_interface.get_microsite"
        )
        mock_get_microsite = get_microsite_patcher.start()

        mock_microsite = MagicMock()
        mock_microsite.slug = "test-pubmed-microsite"
        mock_microsite.default_procedure = "Asthma Treatment"
        mock_microsite.extralinks = []
        mock_microsite.pubmed_search_terms = ["asthma inhaler", "bronchodilator"]
        # Mock the async method get_combined_context
        mock_microsite.get_combined_context = AsyncMock(return_value="Mock combined context with PubMed")
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

            # Verify fire_and_forget was called with microsite context task
            mock_fire_and_forget.assert_called_once()
            call_args = mock_fire_and_forget.call_args[0][0]

            self.assertIsInstance(call_args, list)
            self.assertEqual(len(call_args), 1, "Should have exactly 1 task (microsite context)")

            task = call_args[0]
            self.assertTrue(asyncio.iscoroutine(task))
            self.assertIn("fetch_microsite_context", task.__name__)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()


class MicrositeBothTasksTest(APITestCase):
    """Test that microsite context with both extralinks and pubmed is fetched together."""

    async def test_combined_context_fetched(self):
        """
        Verify that when a microsite has both extralinks and pubmed_search_terms,
        a single task is created that fetches both via get_combined_context.

        This test ensures the refactored approach uses get_combined_context
        instead of separate tasks.
        """
        mock_model = MockChatModel()
        mock_model.set_next_response("Test response", "Test summary")

        get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        )
        mock_get_chat_backends = get_chat_backends_patcher.start()
        mock_get_chat_backends.return_value = [mock_model]

        # Mock fire_and_forget
        fire_and_forget_patcher = patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool_tasks"
        )
        mock_fire_and_forget = fire_and_forget_patcher.start()
        mock_fire_and_forget.return_value = AsyncMock()

        # Mock get_microsite
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
        # Mock the combined context method
        mock_microsite.get_combined_context = AsyncMock(return_value="Mock combined context with both extralinks and PubMed")
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

            # Verify fire_and_forget was called ONCE with a single microsite context task
            mock_fire_and_forget.assert_called_once()
            call_args = mock_fire_and_forget.call_args[0][0]

            self.assertIsInstance(call_args, list)
            self.assertEqual(
                len(call_args), 1, "Should have exactly 1 task (combined microsite context)"
            )

            # Verify the task is fetch_microsite_context
            task = call_args[0]
            self.assertTrue(asyncio.iscoroutine(task))
            self.assertIn("fetch_microsite_context", task.__name__)

            await communicator.disconnect()
        finally:
            get_chat_backends_patcher.stop()
            fire_and_forget_patcher.stop()
            get_microsite_patcher.stop()
