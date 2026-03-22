"""
Tests for background context summary generation when the model omits the panda emoji.

These test the end-to-end flow through the WebSocket consumer, verifying that:
1. A placeholder is stored when context_part is None
2. fire_and_forget is called with background_generate_summary
3. The background task replaces the placeholder with a real summary
"""

import contextlib
import typing
from unittest.mock import patch, AsyncMock

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from asgiref.sync import sync_to_async

from fighthealthinsurance.chat.context_manager import MISSING_CONTEXT_PREFIX
from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class MockChatModelNoContext(MockChatModel):
    """A mock model that returns responses without context (simulates missing panda emoji)."""

    def __init__(self):
        super().__init__()
        self.set_persistent_response("Here is my response about your denial.", "")

    async def generate_chat_response(self, message, **kwargs):
        response, _ = await super().generate_chat_response(message, **kwargs)
        # Return None for context to simulate missing panda emoji
        return response, None


class BackgroundSummaryIntegrationTest(APITestCase):
    """Test that missing context summaries trigger background generation."""

    async def test_missing_context_stores_placeholder_and_fires_background_task(self):
        """
        When the model returns None for context_part, a placeholder should be
        stored in summary_for_next_call and fire_and_forget should be called
        with the background summary generator.
        """
        mock_model = MockChatModelNoContext()

        with contextlib.ExitStack() as stack:
            mock_get_chat_backends = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
                )
            )
            mock_get_chat_backends.return_value = [mock_model]

            mock_get_fallback = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
                )
            )
            mock_get_fallback.return_value = ([mock_model], [])

            mock_fire_and_forget = stack.enter_context(
                patch(
                    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                    new_callable=AsyncMock,
                )
            )

            user = await sync_to_async(User.objects.create_user)(
                username="bg_summary_user",
                password="testpass",
                email="bgsummary@example.com",
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="5555555555"
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

            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "My insurance denied my GLP-1"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Reload chat from DB
            await chat.arefresh_from_db()

            # Verify a placeholder was stored
            self.assertTrue(
                len(chat.summary_for_next_call) > 0,
                "summary_for_next_call should have at least one entry",
            )
            last_summary = chat.summary_for_next_call[-1]
            self.assertIn(
                MISSING_CONTEXT_PREFIX,
                str(last_summary),
                f"Expected placeholder in summary, got: {last_summary}",
            )

            # Verify fire_and_forget was called (for the background summary task)
            mock_fire_and_forget.assert_called()

            await communicator.disconnect()

    async def test_background_task_replaces_placeholder_before_next_message(self):
        """
        Simulate the background task completing inline (via mock fire_and_forget):
        1. First message: model omits context -> placeholder stored
        2. fire_and_forget runs the background task synchronously, replacing
           the placeholder with a real summary in the DB
        3. Verify the DB entry is the generated summary, not the placeholder
        """
        mock_model = MockChatModelNoContext()

        with contextlib.ExitStack() as stack:
            mock_get_chat_backends = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
                )
            )
            mock_get_chat_backends.return_value = [mock_model]

            mock_get_fallback = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
                )
            )
            mock_get_fallback.return_value = ([mock_model], [])

            # Execute fire_and_forget inline so background task runs immediately
            async def mock_fire_and_forget_impl(coro):
                return await coro

            stack.enter_context(
                patch(
                    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                    side_effect=mock_fire_and_forget_impl,
                )
            )

            mock_summarize = stack.enter_context(
                patch(
                    "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
                    new_callable=AsyncMock,
                )
            )
            mock_summarize.return_value = (
                "Patient denied GLP-1 coverage, asking for help"
            )

            user = await sync_to_async(User.objects.create_user)(
                username="bg_replace_user",
                password="testpass",
                email="bgreplace@example.com",
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="6666666666"
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

            # Send first message — model omits context, background task fires & completes
            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "My GLP-1 was denied"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            # Reload and verify placeholder was replaced by background task
            await chat.arefresh_from_db()

            self.assertTrue(len(chat.summary_for_next_call) > 0)
            last_summary = chat.summary_for_next_call[-1]
            self.assertNotIn(
                MISSING_CONTEXT_PREFIX,
                str(last_summary),
                "Placeholder should have been replaced by background summary",
            )
            self.assertEqual(
                last_summary,
                "Patient denied GLP-1 coverage, asking for help",
            )

            await communicator.disconnect()

    async def test_normal_context_does_not_trigger_background_task(self):
        """
        When the model returns a context summary normally (with panda emoji),
        no placeholder should be stored and no background task should fire.
        """
        mock_model = MockChatModel()
        mock_model.set_persistent_response(
            "Here is my response.", "Helping patient with GLP-1 denial"
        )

        with contextlib.ExitStack() as stack:
            mock_get_chat_backends = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
                )
            )
            mock_get_chat_backends.return_value = [mock_model]

            mock_get_fallback = stack.enter_context(
                patch(
                    "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
                )
            )
            mock_get_fallback.return_value = ([mock_model], [])

            mock_fire_and_forget = stack.enter_context(
                patch(
                    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                    new_callable=AsyncMock,
                )
            )

            user = await sync_to_async(User.objects.create_user)(
                username="normal_ctx_user",
                password="testpass",
                email="normalctx@example.com",
            )
            professional = await sync_to_async(ProfessionalUser.objects.create)(
                user=user, active=True, npi_number="7777777777"
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

            await communicator.send_json_to(
                {"chat_id": str(chat.id), "content": "My insurance denied my claim"}
            )

            response = await communicator.receive_json_from(timeout=15)
            while "status" in response:
                response = await communicator.receive_json_from(timeout=15)

            self.assertIn("content", response)

            await chat.arefresh_from_db()

            # Summary should be the real one from the model, no placeholder
            self.assertTrue(len(chat.summary_for_next_call) > 0)
            for entry in chat.summary_for_next_call:
                self.assertNotIn(
                    MISSING_CONTEXT_PREFIX,
                    str(entry),
                    "No placeholder should exist when model provides context",
                )

            # fire_and_forget should NOT have been called (no microsite, no missing context)
            mock_fire_and_forget.assert_not_called()

            await communicator.disconnect()
