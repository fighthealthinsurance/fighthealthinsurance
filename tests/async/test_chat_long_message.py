"""
Integration tests for long pasted messages in the ongoing chat flow.

Verifies end-to-end (through the WebSocket consumer) that:
1. A huge paste is preserved in document storage and replaced in chat history
   with a compact marker (history stays bounded).
2. The full text is never fanned out to the model backends.
3. A normal short message still takes the original/primary path unchanged.
"""

import contextlib
import typing
from itertools import pairwise
from unittest.mock import AsyncMock, patch

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from asgiref.sync import sync_to_async

from fighthealthinsurance.chat.message_preprocessor import (
    DIRECT_CHAT_HARD_LIMIT_CHARS,
    DIRECT_CHAT_SOFT_LIMIT_CHARS,
)
from fighthealthinsurance.models import ChatDocument, OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class RecordingMockModel(MockChatModel):
    """Mock model that records every message it is asked to generate against."""

    def __init__(self):
        super().__init__()
        self.received_messages: list[str] = []
        self.set_persistent_response(
            "Here is some guidance about your denial and next steps.",
            "Summary: discussed the denial.",
        )

    async def generate_chat_response(self, message, **kwargs):
        self.received_messages.append(message)
        return await super().generate_chat_response(message, **kwargs)


class FailingMockModel(MockChatModel):
    """Mock model whose every generation attempt fails (returns nothing)."""

    async def generate_chat_response(self, message, **kwargs):
        return (None, None)


class VaryingMockModel(MockChatModel):
    """Mock model that answers something different every call, so multi-turn
    tests don't trip the repeated-reply rejection ladder. Records every
    message it was asked to generate against."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.received_messages: list[str] = []

    async def generate_chat_response(self, message, **kwargs):
        self.calls += 1
        self.received_messages.append(message)
        return (
            f"Reply number {self.calls} with fresh guidance about the denial.",
            f"Summary: turn {self.calls}.",
        )


async def _make_professional_chat(username, npi):
    user = await sync_to_async(User.objects.create_user)(
        username=username, password="testpass", email=f"{username}@example.com"
    )
    professional = await sync_to_async(ProfessionalUser.objects.create)(
        user=user, active=True, npi_number=npi
    )
    chat = await sync_to_async(OngoingChat.objects.create)(
        professional_user=professional,
        chat_history=[],
        summary_for_next_call=[],
    )
    return user, chat


@contextlib.contextmanager
def _patched_backends(mock_model):
    with contextlib.ExitStack() as stack:
        get_backends = stack.enter_context(
            patch("fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends")
        )
        get_backends.return_value = [mock_model]
        get_fallback = stack.enter_context(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
            )
        )
        get_fallback.return_value = ([mock_model], [])
        stack.enter_context(
            patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            )
        )
        yield stack


async def _drain_to_content(communicator):
    response = await communicator.receive_json_from(timeout=20)
    while "status" in response:
        response = await communicator.receive_json_from(timeout=20)
    return response


class LongPasteChatTest(APITestCase):
    async def test_huge_paste_is_stored_and_history_stays_compact(self):
        big = (
            "This claim was denied because the requested service is considered "
            "not medically necessary. "
        ) * 700  # ~63k chars, well over the hard limit
        self.assertGreater(len(big), DIRECT_CHAT_HARD_LIMIT_CHARS)

        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ) as mock_store:
                user, chat = await _make_professional_chat("longpaste1", "9999900001")

                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": big}
                    )
                    response = await _drain_to_content(communicator)
                    self.assertIn("content", response)
                finally:
                    await communicator.disconnect()

                # The full original text was preserved via document storage.
                mock_store.assert_awaited_once()
                store_kwargs = mock_store.await_args.kwargs
                self.assertEqual(len(store_kwargs["full_text"]), len(big))
                self.assertTrue(
                    store_kwargs["document_name"].startswith("pasted_message_")
                )

                # Chat history stores a compact marker, not the 63k blob.
                await chat.arefresh_from_db()
                user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
                self.assertEqual(len(user_msgs), 1)
                stored = user_msgs[0]["content"]
                self.assertLess(len(stored), 500)
                self.assertIn("stored for reference", stored.lower())
                self.assertNotIn(big, stored)

                # The full text was never fanned out to the backend.
                self.assertTrue(mock_model.received_messages)
                for msg in mock_model.received_messages:
                    self.assertNotIn(big, msg)
                    self.assertLess(len(msg), len(big))

    async def test_message_alternation_after_huge_paste(self):
        big = "Denied as experimental treatment. " * 1000  # ~34k chars
        self.assertGreater(len(big), DIRECT_CHAT_HARD_LIMIT_CHARS)

        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ):
                user, chat = await _make_professional_chat("longpaste2", "9999900002")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": big}
                    )
                    await _drain_to_content(communicator)
                finally:
                    await communicator.disconnect()

                await chat.arefresh_from_db()
                roles = [m.get("role") for m in chat.chat_history]
                # Alternation: no two consecutive messages share a role.
                for prev, nxt in pairwise(roles):
                    self.assertNotEqual(prev, nxt)
                self.assertEqual(roles[-1], "assistant")


class LongPasteAllModelsFailTest(APITestCase):
    """When every model fails on a long-paste turn, the user must get a
    useful acknowledgment (content stored, how to proceed) instead of the
    generic all-models-down error frame -- the paste IS stored and queued
    for analysis, so 'try again' would only duplicate the failure."""

    async def test_total_model_failure_yields_acknowledgment_not_error(self):
        big = "Coverage denied: intensive outpatient program not authorized. " * 400
        mock_model = FailingMockModel()
        with _patched_backends(mock_model):
            # Real document storage (the acknowledgment must only ever claim
            # "stored" when content truly is); only the summarization fan-out
            # is mocked so no background ML work runs.
            with patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ):
                user, chat = await _make_professional_chat("longpaste3", "9999900004")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": big}
                    )
                    response = await _drain_to_content(communicator)
                finally:
                    await communicator.disconnect()

                # An assistant message, not an error frame.
                self.assertNotIn("error", response)
                self.assertIn("content", response)
                ack = response["content"]
                self.assertIn("paste it again", ack)

                # The content really was stored, and the acknowledgment names
                # the document that actually exists.
                docs = [
                    d async for d in ChatDocument.objects.filter(chat_id=chat.id).all()
                ]
                self.assertEqual(len(docs), 1)
                self.assertEqual(docs[0].full_text, big)
                self.assertIn(docs[0].document_name, ack)

                # History shows a coherent, alternating exchange: the compact
                # marker followed by the acknowledgment.
                await chat.arefresh_from_db()
                roles = [m.get("role") for m in chat.chat_history]
                self.assertEqual(roles, ["user", "assistant"])
                self.assertIn("stored for reference", chat.chat_history[0]["content"])
                self.assertEqual(chat.chat_history[1]["content"], ack)

    async def test_setup_failure_after_storage_still_arms_summarization(self):
        # A raise between storage and the LLM pass (here: history prep) exits
        # the turn before the deferred kickoff in its finally block. The
        # document must survive as PENDING with the storage-time watchdog
        # armed to rescue it -- not be stranded unanalyzed.
        big = "Denial letter contents pasted just before a setup crash. " * 400
        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ) as mock_fire:
                with patch(
                    "fighthealthinsurance.chat_interface.prepare_history_for_llm",
                    side_effect=RuntimeError("boom after storage"),
                ):
                    user, chat = await _make_professional_chat(
                        "longpaste5", "9999900007"
                    )
                    communicator = WebsocketCommunicator(
                        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                    )
                    communicator.scope["user"] = user
                    connected, _ = await communicator.connect()
                    try:
                        await communicator.send_json_to(
                            {"chat_id": str(chat.id), "content": big}
                        )
                        response = await _drain_to_content(communicator)
                    finally:
                        await communicator.disconnect()

                # The turn itself failed (consumer-level error frame)...
                self.assertIn("error", response)

                # ...but the document was stored, is still PENDING, and the
                # watchdog was armed at storage time to start summarization.
                docs = [
                    d async for d in ChatDocument.objects.filter(chat_id=chat.id).all()
                ]
                self.assertEqual(len(docs), 1)
                self.assertEqual(
                    docs[0].processing_status, ChatDocument.Status.PENDING
                )
                fired_names = [
                    call.args[0].__name__ for call in mock_fire.call_args_list
                ]
                self.assertIn("_deferred_summarization_watchdog", fired_names)
                # No summarization worker was dispatched mid-crash.
                self.assertNotIn("summarize_chunks", fired_names)

    async def test_total_model_failure_on_short_message_still_errors(self):
        # The acknowledgment fallback is only for turns whose content was
        # diverted to storage; an ordinary failed turn keeps the error frame.
        mock_model = FailingMockModel()
        with _patched_backends(mock_model):
            user, chat = await _make_professional_chat("shortfail1", "9999900005")
            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user
            connected, _ = await communicator.connect()
            try:
                await communicator.send_json_to(
                    {"chat_id": str(chat.id), "content": "Why was my claim denied?"}
                )
                response = await _drain_to_content(communicator)
            finally:
                await communicator.disconnect()

            self.assertIn("error", response)


class LongPasteDedupTest(APITestCase):
    """Re-pasting the same long message (say, after a failed turn) must not
    create a second stored document, and the marker in history must keep
    referencing the document that actually exists."""

    async def test_repaste_reuses_document_and_marker_names_it(self):
        # ~20k chars: over the soft limit that triggers long-paste storage
        # (like the ~19k production failure) though under the hard cap.
        big = "Denied for lack of prior authorization on imaging. " * 400
        self.assertGreater(len(big), DIRECT_CHAT_SOFT_LIMIT_CHARS)

        mock_model = VaryingMockModel()
        with _patched_backends(mock_model):
            # Real document storage; only the summarization fan-out is mocked
            # (both the deferred kick in document_processor and any direct
            # fire) so no ML work runs.
            with patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ) as mock_fire:
                user, chat = await _make_professional_chat("longpaste4", "9999900006")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                try:
                    for _ in range(2):
                        await communicator.send_json_to(
                            {"chat_id": str(chat.id), "content": big}
                        )
                        await _drain_to_content(communicator)
                finally:
                    await communicator.disconnect()

                # One stored document, summarization kicked off (deferred to
                # after the turn, but still kicked).
                docs = [
                    d
                    async for d in ChatDocument.objects.filter(chat_id=chat.id).all()
                ]
                self.assertEqual(len(docs), 1)
                self.assertTrue(mock_fire.called)

                # Every marker in history references the document that exists.
                await chat.arefresh_from_db()
                user_msgs = [
                    m["content"]
                    for m in chat.chat_history
                    if m.get("role") == "user"
                ]
                self.assertEqual(len(user_msgs), 2)
                for msg in user_msgs:
                    self.assertIn(docs[0].document_name, msg)


class LongPasteNameAdoptionDoesNotCorruptContentTest(APITestCase):
    """Adopting a deduped document's name must not rewrite the user's own
    words. document_name is client-supplied and the truncated variant's text
    IS the raw paste, so a name that occurs in the pasted content (here the
    word "denied") must never be substituted inside the message we send to
    the model."""

    async def test_repaste_with_content_word_as_document_name(self):
        big = "The claim was denied because it was denied again. " * 500
        self.assertGreater(len(big), DIRECT_CHAT_SOFT_LIMIT_CHARS)

        mock_model = VaryingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ):
                user, chat = await _make_professional_chat("longpaste6", "9999900008")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    # First paste stores the document under its own name.
                    await communicator.send_json_to(
                        {
                            "chat_id": str(chat.id),
                            "content": big,
                            "document_name": "first_doc.txt",
                        }
                    )
                    await _drain_to_content(communicator)

                    # Re-paste of the SAME content, this time naming it with a
                    # word that appears throughout that content. It dedupes
                    # onto "first_doc.txt", triggering name adoption.
                    await communicator.send_json_to(
                        {
                            "chat_id": str(chat.id),
                            "content": big,
                            "document_name": "denied",
                        }
                    )
                    await _drain_to_content(communicator)
                finally:
                    await communicator.disconnect()

                docs = [
                    d async for d in ChatDocument.objects.filter(chat_id=chat.id).all()
                ]
                self.assertEqual(len(docs), 1)
                self.assertEqual(docs[0].document_name, "first_doc.txt")

                # The user's words survived intact: no message sent to the
                # model contains the substitution "first_doc.txt" where the
                # user wrote "denied".
                self.assertTrue(mock_model.received_messages)
                for msg in mock_model.received_messages:
                    self.assertNotIn("was first_doc.txt because", msg)


class NormalMessagePrimaryPathTest(APITestCase):
    async def test_short_message_stored_verbatim_and_not_routed_to_storage(self):
        msg = "Why was my physical therapy claim denied?"
        mock_model = RecordingMockModel()
        with _patched_backends(mock_model):
            with patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ) as mock_store:
                user, chat = await _make_professional_chat("normalmsg1", "9999900003")
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                communicator.scope["user"] = user
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
                try:
                    await communicator.send_json_to(
                        {"chat_id": str(chat.id), "content": msg}
                    )
                    response = await _drain_to_content(communicator)
                    self.assertIn("content", response)
                finally:
                    await communicator.disconnect()

                # No long-paste storage for a normal message.
                mock_store.assert_not_awaited()

                # The raw message is stored verbatim (primary path).
                await chat.arefresh_from_db()
                user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
                self.assertEqual(len(user_msgs), 1)
                self.assertEqual(user_msgs[0]["content"], msg)
