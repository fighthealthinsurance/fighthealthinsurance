"""Chat session integrity: policy-doc handoff keys, fork visibility, and the
transactional summary helpers.

- PolicyDocument rows are stored under the DJANGO session key by the upload
  view, while chat.session_key is a client-generated localStorage string.
  _handle_policy_analysis must find documents via the server session key.
- When a supplied chat_id resolves to a NEW chat (fork), the client gets an
  explicit {chat_forked, chat_id} frame instead of a silent re-key.
- Microsite context and background-summary placeholder swaps go through
  transactional helpers that merge against fresh row state.
"""

import typing
import uuid
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.chat_persistence import (
    apersist_microsite_context,
    areplace_summary_placeholder,
)
from fighthealthinsurance.chat.context_manager import make_placeholder
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import (
    OngoingChat,
    PolicyDocument,
    ProfessionalUser,
)
from fighthealthinsurance.websockets import OngoingChatConsumer

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class _FrameRecorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)


async def _make_professional_chat(username, npi, **chat_kwargs):
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
        **chat_kwargs,
    )
    return user, chat


class PolicyHandoffSessionKeyTest(APITestCase):
    async def test_doc_found_via_server_session_key(self):
        """The doc lives under the Django session key; the chat's own
        session_key is a random client string. The handoff must still work."""
        django_session_key = f"django-session-{uuid.uuid4().hex[:12]}"
        client_session_key = f"client-random-{uuid.uuid4().hex[:12]}"
        doc = await sync_to_async(PolicyDocument.objects.create)(
            session_key=django_session_key,
            document_type="summary_of_benefits",
            filename="policy.pdf",
        )
        user, chat = await _make_professional_chat(
            "policyhandoff1", "9999930001", session_key=client_session_key
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            server_session_key=django_session_key,
        )

        fake_analysis = {"summary": "Covers MRI with prior auth."}
        with patch(
            "fighthealthinsurance.chat_interface.MLPolicyDocHelper.get_or_create_analysis",
            new=AsyncMock(return_value=fake_analysis),
        ) as get_analysis, patch(
            "fighthealthinsurance.chat_interface.MLPolicyDocHelper.format_analysis_for_chat",
            return_value="Formatted analysis text",
        ):
            await interface._handle_policy_analysis(
                chat,
                "I've uploaded my insurance summary of benefits.\n"
                "Please analyze this document",
            )

        get_analysis.assert_awaited_once()
        analyzed_doc = get_analysis.await_args.args[0]
        assert analyzed_doc.id == doc.id
        # The turn was persisted (user message + analysis).
        fresh = await OngoingChat.objects.aget(id=chat.id)
        roles = [m.get("role") for m in fresh.chat_history]
        self.assertEqual(roles, ["user", "assistant"])
        # No error frame went out.
        self.assertFalse([f for f in recorder.frames if "error" in f])

    async def test_missing_doc_still_reports_cleanly(self):
        user, chat = await _make_professional_chat(
            "policyhandoff2", "9999930002", session_key="no-doc-here"
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            server_session_key=None,
        )
        await interface._handle_policy_analysis(chat, "Please analyze")
        self.assertTrue([f for f in recorder.frames if "error" in f])


class ChatForkVisibilityTest(APITestCase):
    async def test_mismatched_chat_id_gets_fork_frame(self):
        user, chat = await _make_professional_chat("forkvis1", "9999930003")
        bogus_chat_id = str(uuid.uuid4())

        from tests.sync.mock_chat_model import MockChatModel

        mock_model = MockChatModel()
        mock_model.set_persistent_response("A reply.", "Summary.")
        with patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
        ) as gb, patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
        ) as gf, patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            gb.return_value = [mock_model]
            gf.return_value = ([mock_model], [])
            communicator = WebsocketCommunicator(
                OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
            )
            communicator.scope["user"] = user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            try:
                await communicator.send_json_to(
                    {"chat_id": bogus_chat_id, "content": "Hello there"}
                )
                frames = []
                for _ in range(10):
                    frame = await communicator.receive_json_from(timeout=20)
                    frames.append(frame)
                    if "content" in frame or "error" in frame:
                        break
            finally:
                await communicator.disconnect()

        fork_frames = [f for f in frames if f.get("chat_forked")]
        self.assertTrue(fork_frames, f"expected a chat_forked frame, got: {frames}")
        # The fork frame carries the NEW chat id, not the bogus one.
        self.assertNotEqual(fork_frames[0].get("chat_id"), bogus_chat_id)


class TransactionalSummaryHelpersTest(APITestCase):
    async def test_microsite_context_is_placeholder_safe(self):
        user, chat = await _make_professional_chat("micrositem1", "9999930004")
        placeholder = make_placeholder(3)
        chat.summary_for_next_call = [placeholder]
        await chat.asave()

        await apersist_microsite_context(chat, "Microsite context:\nStuff")
        fresh = await OngoingChat.objects.aget(id=chat.id)
        # Placeholder untouched; microsite entry stored separately.
        self.assertEqual(fresh.summary_for_next_call[0], placeholder)
        self.assertIn("Microsite context:", fresh.summary_for_next_call[1])

    async def test_microsite_context_is_idempotent(self):
        user, chat = await _make_professional_chat("micrositem2", "9999930005")
        await apersist_microsite_context(chat, "Microsite context:\nStuff")
        await apersist_microsite_context(chat, "Microsite context:\nStuff")
        fresh = await OngoingChat.objects.aget(id=chat.id)
        joined = "\n".join(str(e) for e in fresh.summary_for_next_call)
        self.assertEqual(joined.count("Microsite context:"), 1)

    async def test_placeholder_swap_only_replaces_its_own_tag(self):
        user, chat = await _make_professional_chat("swaptag1", "9999930006")
        tag_a = make_placeholder(2)
        tag_b = make_placeholder(4)
        chat.summary_for_next_call = [tag_a, tag_b]
        await chat.asave()

        replaced = await areplace_summary_placeholder(chat.id, tag_a, "real summary A")
        self.assertTrue(replaced)
        fresh = await OngoingChat.objects.aget(id=chat.id)
        self.assertEqual(fresh.summary_for_next_call, ["real summary A", tag_b])

    async def test_placeholder_swap_noop_when_superseded(self):
        user, chat = await _make_professional_chat("swaptag2", "9999930007")
        chat.summary_for_next_call = ["a real summary already"]
        await chat.asave()
        replaced = await areplace_summary_placeholder(
            chat.id, make_placeholder(9), "late summary"
        )
        self.assertFalse(replaced)
        fresh = await OngoingChat.objects.aget(id=chat.id)
        self.assertEqual(fresh.summary_for_next_call, ["a real summary already"])
