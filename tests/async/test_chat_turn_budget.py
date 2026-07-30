"""The chat turn wall-clock budget and heartbeat.

A wedged backend chain must not hold a chat turn open indefinitely: the turn
is bounded by FHI_CHAT_TURN_BUDGET, the timeout lands in the failure branch
(user message persisted + error frame, from Phase 1), and while the turn runs
the client receives heartbeat status frames so idle-reaping proxies see
traffic.
"""

import asyncio
import typing
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import OngoingChat, ProfessionalUser
from tests.sync.mock_chat_model import MockChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class _FrameRecorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)


async def _make_chat(username, npi):
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


class ChatTurnBudgetTest(APITestCase):
    async def _run_stalled_turn(self, username, npi, monkey_env):
        user, chat = await _make_chat(username, npi)
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        async def stalled(*args, **kwargs):
            await asyncio.sleep(30)
            return ("should never be seen", None)

        mock_model = MockChatModel()
        with (
            patch.dict("os.environ", monkey_env),
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends"
            ) as get_backends,
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback"
            ) as get_fallback,
            patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ),
            patch.object(
                ChatInterface,
                "_call_llm_with_actions",
                new=AsyncMock(side_effect=stalled),
            ),
        ):
            get_backends.return_value = [mock_model]
            get_fallback.return_value = ([mock_model], [])
            await asyncio.wait_for(
                interface.handle_chat_message("What are my appeal options?"),
                timeout=20,
            )
        return chat, recorder

    async def test_stalled_turn_hits_budget_and_fails_cleanly(self):
        chat, recorder = await self._run_stalled_turn(
            "turnbudget1",
            "9999920001",
            {"FHI_CHAT_TURN_BUDGET": "1", "FHI_CHAT_HEARTBEAT_SECONDS": "600"},
        )
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertTrue(
            error_frames, f"expected an error frame, got: {recorder.frames}"
        )
        # The user's message survived the timed-out turn.
        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [m for m in (fresh.chat_history or []) if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)

    async def test_heartbeat_frames_flow_during_long_turn(self):
        chat, recorder = await self._run_stalled_turn(
            "turnbudget2",
            "9999920002",
            {"FHI_CHAT_TURN_BUDGET": "1", "FHI_CHAT_HEARTBEAT_SECONDS": "0.2"},
        )
        heartbeats = [
            f
            for f in recorder.frames
            if "status" in f and "Still working" in str(f.get("status"))
        ]
        self.assertTrue(
            heartbeats,
            f"expected at least one heartbeat status frame, got: {recorder.frames}",
        )
