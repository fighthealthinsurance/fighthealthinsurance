"""Integration tests for the delete-data safety handoff in ChatInterface.

Two end-to-end paths through `ChatInterface.handle_chat_message`:

1. User-side regex match short-circuits before the LLM is consulted.
2. LLM response containing the delete-data sentinel is swapped for the
   canned `DELETE_DATA_RESPONSE`. The second test uses an ongoing
   (non-empty-history) chat so it also exercises the every-turn instruction
   injection that keeps the sentinel handoff working past the first message.
"""

import typing
from unittest.mock import AsyncMock, patch

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.safety_filters import (
    DELETE_DATA_RESPONSE,
    DELETE_DATA_SENTINEL,
)
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import (
    ChatType,
    OngoingChat,
    ProfessionalUser,
)
from fighthealthinsurance.prompt_templates import DELETE_DATA_INSTRUCTION

from .mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class DeleteDataHandoffIntegrationTest(APITestCase):
    """End-to-end coverage for the delete-data short-circuit and sentinel swap."""

    async def asyncSetUp(self):
        self.mock_model = MockChatModel()

        self.get_chat_backends_patcher = patch(
            "fighthealthinsurance.ml.ml_router.ml_router.get_chat_backends"
        )
        self.mock_get_chat_backends = self.get_chat_backends_patcher.start()
        self.mock_get_chat_backends.return_value = [self.mock_model]

        self.get_chat_backends_with_fallback_patcher = patch(
            "fighthealthinsurance.ml.ml_router.ml_router.get_chat_backends_with_fallback"
        )
        self.mock_get_chat_backends_with_fallback = (
            self.get_chat_backends_with_fallback_patcher.start()
        )
        self.mock_get_chat_backends_with_fallback.return_value = (
            [self.mock_model],
            [],
        )

    async def asyncTearDown(self):
        self.get_chat_backends_patcher.stop()
        self.get_chat_backends_with_fallback_patcher.stop()

    async def _make_chat_and_interface(
        self,
        captured: list,
        username: str,
        npi_number: str,
        chat_history=None,
    ):
        """Create a professional user, chat, and ChatInterface wired to `captured`."""
        user = await database_sync_to_async(User.objects.create_user)(
            username=username,
            password="testpass",
            email=f"{username}@example.com",
        )
        professional = await database_sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number=npi_number
        )
        chat = await database_sync_to_async(OngoingChat.objects.create)(
            professional_user=professional,
            chat_type=ChatType.PROFESSIONAL,
            chat_history=chat_history if chat_history is not None else [],
            summary_for_next_call=[],
        )

        async def send_json(payload):
            captured.append(payload)

        chat_interface = ChatInterface(
            send_json_message_func=send_json,
            chat=chat,
            user=user,
        )
        return chat, chat_interface

    async def test_user_delete_request_short_circuits_without_llm_call(self):
        """Regex match on user message must skip the LLM and reply with the canned text."""
        await self.asyncSetUp()
        try:
            captured: list = []
            chat, chat_interface = await self._make_chat_and_interface(
                captured,
                username="deleteshortcircuit",
                npi_number="1112223333",
            )

            llm_spy = AsyncMock(return_value=("would-be-LLM-reply", "{}"))
            self.mock_model.generate_chat_response = llm_spy

            await chat_interface.handle_chat_message("Please delete my account")

            llm_spy.assert_not_called()

            assistant_payloads = [p for p in captured if p.get("role") == "assistant"]
            self.assertEqual(len(assistant_payloads), 1)
            self.assertEqual(assistant_payloads[0]["content"], DELETE_DATA_RESPONSE)

            await database_sync_to_async(chat.refresh_from_db)()
            self.assertEqual(len(chat.chat_history), 2)
            self.assertEqual(chat.chat_history[0]["role"], "user")
            self.assertEqual(
                chat.chat_history[0]["content"], "Please delete my account"
            )
            self.assertEqual(chat.chat_history[1]["role"], "assistant")
            self.assertEqual(chat.chat_history[1]["content"], DELETE_DATA_RESPONSE)
        finally:
            await self.asyncTearDown()

    async def test_llm_sentinel_response_is_swapped_on_ongoing_chat(self):
        """LLM emits the sentinel on a non-new chat; response must be swapped.

        Pre-populating chat_history makes this an ongoing chat, which exercises
        the every-turn DELETE_DATA_INSTRUCTION injection. We also assert that
        the LLM received the instruction in its prompt to lock in that path.
        """
        await self.asyncSetUp()
        try:
            captured: list = []
            chat, chat_interface = await self._make_chat_and_interface(
                captured,
                username="deletesentinelswap",
                npi_number="4445556666",
                chat_history=[
                    {
                        "role": "user",
                        "content": "hi",
                        "timestamp": "2026-01-01T00:00:00",
                    },
                    {
                        "role": "assistant",
                        "content": "Hello, how can I help?",
                        "timestamp": "2026-01-01T00:00:01",
                    },
                ],
            )

            # User phrasing that the regex does NOT match, so we exercise the
            # LLM path and the sentinel-swap branch rather than the
            # short-circuit branch.
            oblique_user_message = (
                "I'd appreciate it if you'd wipe everything you have on me, please."
            )

            # Capture the message the LLM saw so we can verify the every-turn
            # instruction was injected. set_next_response controls what the
            # mock returns; we wrap generate_chat_response only to record the
            # message argument.
            self.mock_model.set_next_response(
                f"Sure thing.\n{DELETE_DATA_SENTINEL}\n",
                str({"summary": "sentinel-emitted"}),
            )

            seen_messages: list = []
            original_generate = self.mock_model.generate_chat_response

            async def recording_generate(message, *args, **kwargs):
                seen_messages.append(message)
                return await original_generate(message, *args, **kwargs)

            self.mock_model.generate_chat_response = recording_generate

            await chat_interface.handle_chat_message(oblique_user_message)

            assistant_payloads = [p for p in captured if p.get("role") == "assistant"]
            self.assertTrue(assistant_payloads, "Expected at least one assistant reply")
            self.assertEqual(assistant_payloads[-1]["content"], DELETE_DATA_RESPONSE)

            self.assertTrue(
                seen_messages, "LLM should have been invoked on an ongoing chat"
            )
            self.assertIn(DELETE_DATA_INSTRUCTION, seen_messages[0])
        finally:
            await self.asyncTearDown()
