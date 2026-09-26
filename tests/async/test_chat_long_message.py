"""
Integration tests for long pasted messages in the ongoing chat flow.

Verifies end-to-end (through the WebSocket consumer) that:
1. A huge paste is preserved in document storage and replaced in chat history
   with a compact marker (history stays bounded).
2. The full text is never fanned out to the model backends.
3. A turn whose content was stored answers with an acknowledgment -- never
   an error frame, and never its own marker echoed back -- when the models
   produce no real reply.
4. Re-pasting identical content reuses the stored document.
5. A normal short message still takes the original/primary path unchanged.
"""

import itertools
import typing
from itertools import pairwise
from unittest.mock import AsyncMock, patch

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from prometheus_client import REGISTRY
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.message_preprocessor import (
    DIRECT_CHAT_HARD_LIMIT_CHARS,
    DIRECT_CHAT_SOFT_LIMIT_CHARS,
    build_long_paste_marker,
)
from fighthealthinsurance.models import ChatDocument, OngoingChat, ProfessionalUser
from fighthealthinsurance.websockets import OngoingChatConsumer
from tests.chat_fixtures import RecordingChatModel
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


GUIDANCE_REPLY = "Here is some guidance about your denial and next steps."


class VaryingChatModel(RecordingChatModel):
    """Answers something different on every call, so multi-turn tests don't
    trip the repeated-reply rejection ladder."""

    async def generate_chat_response(self, current_message_for_llm, **kwargs):
        await super().generate_chat_response(current_message_for_llm, **kwargs)
        n = len(self.calls)
        return (
            f"Reply number {n} with fresh guidance about the denial.",
            f"Summary: turn {n}.",
        )


def _failing_model() -> MockChatModel:
    """A backend whose every generation attempt fails (returns nothing)."""
    model = MockChatModel()
    model.set_persistent_response(None, None)
    return model


def _messages_sent(model: RecordingChatModel) -> list[str]:
    """Every message the model was asked to generate a reply to."""
    return [call["message"] for call in model.calls]


def _unsaved_document(chat, document_name, full_text, **_):
    """Stand-in for process_uploaded_document: the document storage would
    return, without touching the database."""
    return ChatDocument(
        chat=chat,
        document_name=document_name,
        full_text=full_text,
        char_count=len(full_text),
    )


async def _discard_background_task(coro):
    """Stand-in for fire_and_forget_in_new_threadpool: drop the background
    coroutine instead of running it (closed, so it can't warn that it was
    never awaited)."""
    coro.close()


def _counter(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


_user_numbers = itertools.count(1)


async def _make_professional_chat():
    n = next(_user_numbers)
    user = await User.objects.acreate_user(
        username=f"longpaste{n}",
        password="testpass",
        email=f"longpaste{n}@example.com",
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number=f"99999{n:05d}"
    )
    chat = await OngoingChat.objects.acreate(
        professional_user=professional,
        chat_history=[],
        summary_for_next_call=[],
    )
    return user, chat


async def _drain_to_content(communicator):
    response = await communicator.receive_json_from(timeout=20)
    while "status" in response:
        response = await communicator.receive_json_from(timeout=20)
    return response


class ChatTurnTestCase(APITestCase):
    """Drives chat turns through the WebSocket consumer against
    ``self.model`` (reassign it before the first turn to swap the backend).
    Background work -- turn summaries, document analysis -- is handed to
    mocks that discard it, so no ML work ever runs."""

    def setUp(self):
        super().setUp()
        self.model = RecordingChatModel(always_reply=GUIDANCE_REPLY)
        self.enterContext(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends",
                side_effect=lambda *args, **kwargs: [self.model],
            )
        )
        self.enterContext(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
                side_effect=lambda *args, **kwargs: ([self.model], []),
            )
        )
        self.enterContext(
            patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                new=AsyncMock(side_effect=_discard_background_task),
            )
        )
        self.summarization_dispatch = self.enterContext(
            patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new=AsyncMock(side_effect=_discard_background_task),
            )
        )

    def mock_document_storage(self) -> AsyncMock:
        """Replace the turn's document storage with an in-memory stand-in."""
        return self.enterContext(
            patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new=AsyncMock(side_effect=_unsaved_document),
            )
        )

    def dispatched_background_tasks(self) -> list[str]:
        """Names of the document-analysis coroutines dispatched so far."""
        return [
            call.args[0].__name__ for call in self.summarization_dispatch.call_args_list
        ]

    async def run_turns(self, user, *payloads) -> list[dict]:
        """Send each payload over one connection, returning the first
        non-status frame received after each."""
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        try:
            responses = []
            for payload in payloads:
                await communicator.send_json_to(payload)
                responses.append(await _drain_to_content(communicator))
            return responses
        finally:
            await communicator.disconnect()

    async def send(self, user, chat, content, **extra) -> dict:
        """Run a single turn sending ``content``; return its response frame."""
        (response,) = await self.run_turns(
            user, {"chat_id": str(chat.id), "content": content, **extra}
        )
        return response


class LongPasteChatTest(ChatTurnTestCase):
    """One huge paste, storage mocked out: what reaches storage, chat
    history and the model."""

    BIG = (
        "This claim was denied because the requested service is considered "
        "not medically necessary. "
    ) * 700  # ~63k chars, well over the hard limit

    def setUp(self):
        super().setUp()
        self.mock_store = self.mock_document_storage()

    async def paste_big(self):
        self.assertGreater(len(self.BIG), DIRECT_CHAT_HARD_LIMIT_CHARS)
        user, chat = await _make_professional_chat()
        response = await self.send(user, chat, self.BIG)
        await chat.arefresh_from_db()
        return chat, response

    async def test_paste_turn_delivers_the_model_reply(self):
        _, response = await self.paste_big()
        self.assertEqual(response.get("content"), GUIDANCE_REPLY)

    async def test_full_text_is_stored_under_a_generated_name(self):
        await self.paste_big()
        self.mock_store.assert_awaited_once()
        store_kwargs = self.mock_store.await_args.kwargs
        self.assertEqual(store_kwargs["full_text"], self.BIG)
        self.assertTrue(store_kwargs["document_name"].startswith("pasted_message_"))

    async def test_history_stores_a_compact_marker(self):
        chat, _ = await self.paste_big()
        user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        stored = user_msgs[0]["content"]
        self.assertLess(len(stored), 500)
        self.assertIn("stored for reference", stored.lower())
        self.assertNotIn(self.BIG, stored)

    async def test_full_text_is_never_sent_to_the_model(self):
        await self.paste_big()
        sent = _messages_sent(self.model)
        self.assertTrue(sent)
        for msg in sent:
            self.assertNotIn(self.BIG, msg)
            self.assertLess(len(msg), len(self.BIG))

    async def test_history_alternates_after_paste(self):
        chat, _ = await self.paste_big()
        roles = [m.get("role") for m in chat.chat_history]
        # Alternation: no two consecutive messages share a role.
        for prev, nxt in pairwise(roles):
            self.assertNotEqual(prev, nxt)
        self.assertEqual(roles[-1], "assistant")


class LongPasteAllModelsFailTest(ChatTurnTestCase):
    """When every model fails on a long-paste turn, the user must get a
    useful acknowledgment (content stored, how to proceed) instead of the
    generic all-models-down error frame -- the paste IS stored and queued
    for analysis, so 'try again' would only duplicate the failure. Storage
    is real here: the acknowledgment must only ever claim "stored" when the
    content truly is."""

    BIG = "Coverage denied: intensive outpatient program not authorized. " * 400

    def setUp(self):
        super().setUp()
        self.model = _failing_model()

    async def paste_big(self):
        user, chat = await _make_professional_chat()
        response = await self.send(user, chat, self.BIG)
        await chat.arefresh_from_db()
        return chat, response

    async def test_total_model_failure_yields_acknowledgment_not_error(self):
        _, response = await self.paste_big()
        self.assertNotIn("error", response)
        self.assertIn("paste it again", response.get("content", ""))

    async def test_acknowledgment_names_the_stored_document(self):
        chat, response = await self.paste_big()
        docs = [d async for d in ChatDocument.objects.filter(chat_id=chat.id)]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].full_text, self.BIG)
        self.assertIn(docs[0].document_name, response.get("content", ""))

    async def test_history_pairs_the_marker_with_the_acknowledgment(self):
        chat, response = await self.paste_big()
        roles = [m.get("role") for m in chat.chat_history]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertIn("stored for reference", chat.chat_history[0]["content"])
        self.assertEqual(chat.chat_history[1]["content"], response.get("content"))

    async def test_total_model_failure_on_short_message_still_errors(self):
        # The acknowledgment fallback is only for turns whose content was
        # diverted to storage; an ordinary failed turn keeps the error frame.
        user, chat = await _make_professional_chat()
        response = await self.send(user, chat, "Why was my claim denied?")
        self.assertIn("error", response)


class LongPasteMarkerEchoTest(ChatTurnTestCase):
    """A model that can only echo the paste's marker back. The ladder's
    last resort delivers a repeat because a repeat beats an error frame; a
    stored-content turn has something better than either -- its
    acknowledgment -- so the echo must never reach the user."""

    DOCUMENT_NAME = "denial_letter.txt"
    BIG = " ".join(["Denied: out-of-network emergency transport not covered."] * 400)

    def setUp(self):
        super().setUp()
        self.model = RecordingChatModel(
            always_reply=build_long_paste_marker(len(self.BIG), self.DOCUMENT_NAME)
        )

    async def paste_big(self):
        user, chat = await _make_professional_chat()
        return await self.send(user, chat, self.BIG, document_name=self.DOCUMENT_NAME)

    async def test_marker_echo_is_replaced_by_acknowledgment(self):
        response = await self.paste_big()
        self.assertIn("paste it again", response.get("content", ""))

    async def test_replacement_is_counted_apart_from_delivered_repeats(self):
        replaced = {"action": "replaced_by_stored_content_ack"}
        before = _counter("fhi_chat_repeated_responses_total", **replaced)
        await self.paste_big()
        self.assertEqual(
            _counter("fhi_chat_repeated_responses_total", **replaced), before + 1
        )

    async def test_replacement_is_not_counted_as_a_failed_turn(self):
        # The models DID answer (only with repeats): alerting on it as a
        # failed turn would page on a turn that worked.
        failed_before = _counter("fhi_chat_turns_total", outcome="failed")
        await self.paste_big()
        self.assertEqual(
            _counter("fhi_chat_turns_total", outcome="failed"), failed_before
        )

    async def test_replacement_raises_no_total_failure_event(self):
        with patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as capture:
            await self.paste_big()
        events = [call.args[0] for call in capture.call_args_list]
        self.assertNotIn("chat_turn_total_failure", events)


class LongPasteCrashedTurnTest(ChatTurnTestCase):
    async def test_setup_failure_after_storage_still_arms_summarization(self):
        # A raise between storage and the LLM pass (here: history prep) exits
        # the turn before the deferred kickoff in its finally block. The
        # document must survive as PENDING with the storage-time watchdog
        # armed to rescue it -- not be stranded unanalyzed.
        big = "Denial letter contents pasted just before a setup crash. " * 400
        self.enterContext(
            patch(
                "fighthealthinsurance.chat_interface.prepare_history_for_llm",
                side_effect=RuntimeError("boom after storage"),
            )
        )
        user, chat = await _make_professional_chat()
        response = await self.send(user, chat, big)

        # The turn itself failed (consumer-level error frame)...
        self.assertIn("error", response)

        # ...but the document was stored, is still PENDING, and the watchdog
        # was armed at storage time to start summarization -- with no
        # summarization worker dispatched mid-crash.
        docs = [d async for d in ChatDocument.objects.filter(chat_id=chat.id)]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].processing_status, ChatDocument.Status.PENDING)
        self.assertEqual(
            self.dispatched_background_tasks(), ["_deferred_summarization_watchdog"]
        )


class LongPasteDedupTest(ChatTurnTestCase):
    """Re-pasting the same long message (say, after a failed turn) must not
    create a second stored document or a second analysis, and the marker in
    history must keep referencing the document that actually exists."""

    # ~20k chars: over the soft limit that triggers long-paste storage (like
    # the ~19k production failure) though under the hard cap.
    BIG = "Denied for lack of prior authorization on imaging. " * 400

    def setUp(self):
        super().setUp()
        self.model = VaryingChatModel()

    async def paste_big_twice(self):
        self.assertGreater(len(self.BIG), DIRECT_CHAT_SOFT_LIMIT_CHARS)
        user, chat = await _make_professional_chat()
        payload = {"chat_id": str(chat.id), "content": self.BIG}
        await self.run_turns(user, payload, payload)
        await chat.arefresh_from_db()
        return chat

    async def test_repaste_reuses_the_stored_document(self):
        chat = await self.paste_big_twice()
        self.assertEqual(await ChatDocument.objects.filter(chat=chat).acount(), 1)

    async def test_repaste_does_not_reanalyze(self):
        await self.paste_big_twice()
        self.assertEqual(
            self.dispatched_background_tasks().count("summarize_chunks"), 1
        )

    async def test_every_marker_names_the_stored_document(self):
        chat = await self.paste_big_twice()
        doc = await ChatDocument.objects.aget(chat=chat)
        user_msgs = [m["content"] for m in chat.chat_history if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 2)
        for msg in user_msgs:
            self.assertIn(doc.document_name, msg)


class LongPasteNameAdoptionDoesNotCorruptContentTest(ChatTurnTestCase):
    """Adopting a deduped document's name must not rewrite the user's own
    words. document_name is client-supplied and the truncated variant's text
    IS the raw paste, so a name that occurs in the pasted content (here the
    word "denied") must never be substituted inside the message we send to
    the model."""

    async def test_repaste_with_content_word_as_document_name(self):
        big = "The claim was denied because it was denied again. " * 500
        self.assertGreater(len(big), DIRECT_CHAT_SOFT_LIMIT_CHARS)
        self.model = VaryingChatModel()
        user, chat = await _make_professional_chat()
        await self.run_turns(
            user,
            # First paste stores the document under its own name.
            {"chat_id": str(chat.id), "content": big, "document_name": "first_doc.txt"},
            # Re-paste of the SAME content, this time naming it with a word
            # that appears throughout that content. It dedupes onto
            # "first_doc.txt", triggering name adoption.
            {"chat_id": str(chat.id), "content": big, "document_name": "denied"},
        )

        doc = await ChatDocument.objects.aget(chat=chat)
        self.assertEqual(doc.document_name, "first_doc.txt")

        # The user's words survived intact: no message sent to the model
        # contains the substitution "first_doc.txt" where the user wrote
        # "denied".
        sent = _messages_sent(self.model)
        self.assertTrue(sent)
        for msg in sent:
            self.assertNotIn("was first_doc.txt because", msg)


class NormalMessagePrimaryPathTest(ChatTurnTestCase):
    MESSAGE = "Why was my physical therapy claim denied?"

    def setUp(self):
        super().setUp()
        self.mock_store = self.mock_document_storage()

    async def test_short_message_is_not_routed_to_storage(self):
        user, chat = await _make_professional_chat()
        await self.send(user, chat, self.MESSAGE)
        self.mock_store.assert_not_awaited()

    async def test_short_message_is_stored_verbatim(self):
        user, chat = await _make_professional_chat()
        response = await self.send(user, chat, self.MESSAGE)
        self.assertIn("content", response)
        await chat.arefresh_from_db()
        user_msgs = [m for m in chat.chat_history if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], self.MESSAGE)
