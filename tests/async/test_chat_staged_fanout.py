"""End-to-end chat turns under the chat routing policy ("ours first").

With a policy delay the outside models' calls are held back while ours
answer: never sent when one of ours answers usably first, sent at once when
ours all fail. Without a policy every model is asked together, as before.
Each ChatTurn row says which of these happened and the delay used.
"""

import asyncio
import typing
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.ml.chat_policy import ChatPolicy
from fighthealthinsurance.models import ChatTurn, OngoingChat, ProfessionalUser
from tests.chat_fixtures import FRESH_REPLY, SECOND_OPINION_REPLY, RecordingChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

_POLICY = ChatPolicy(external_delay_seconds=5.0, reason="ok")


class _OutsideModel(RecordingChatModel):
    external = True


class _OursModel(RecordingChatModel):
    external = False


class _FailingOursModel(_OursModel):
    async def generate_chat_response(self, *args, **kwargs):
        raise RuntimeError("backend down")


class _Frames:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)

    def last_content(self):
        return [f for f in self.frames if "content" in f][-1]["content"]


async def _make_chat(username, npi):
    user = await User.objects.acreate_user(
        username=username, password="testpass", email=f"{username}@example.com"
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number=npi
    )
    chat = await OngoingChat.objects.acreate(
        professional_user=professional, chat_history=[], summary_for_next_call=[]
    )
    return user, chat


def _router_returning(models):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
        return_value=(models, []),
    )


def _policy(policy):
    return patch(
        "fighthealthinsurance.chat_interface.aget_chat_policy",
        new=AsyncMock(return_value=policy),
    )


def _ours_selectable(selectable=True):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter._healthy_general_internal",
        return_value=["fhi-local"] if selectable else [],
    )


_PATCH_FIRE_AND_FORGET = patch(
    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
    new_callable=AsyncMock,
)


async def _only_row(chat):
    return await ChatTurn.objects.filter(chat=chat).aget()


def _statuses(row):
    return sorted({(c["model"], c["status"]) for c in row.calls})


class ChatStagedFanoutTest(APITestCase):
    async def test_a_usable_answer_of_ours_means_no_outside_call_is_sent(self):
        user, chat = await _make_chat("staged1", "9999930201")
        frames = _Frames()
        interface = ChatInterface(send_json_message_func=frames, chat=chat, user=user)
        ours = _OursModel(always_reply=FRESH_REPLY, model_quality=110, name="fhi-local")
        outside = _OutsideModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
        )
        loop = asyncio.get_running_loop()
        started = loop.time()

        with (
            _policy(_POLICY),
            _ours_selectable(),
            _router_returning([ours, outside]) as router,
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("What is an appeal?")

        router.assert_called_once_with(use_external=True, policy=_POLICY)
        assert frames.last_content() == FRESH_REPLY
        assert outside.calls == []
        # It did not sit out the delay.
        assert loop.time() - started < 4.0
        row = await _only_row(chat)
        assert row.external_start == "skipped"
        assert row.external_delay_seconds == 5.0
        assert _statuses(row) == [("claude", "skipped"), ("fhi-local", "scored")]
        assert row.winner_model == "fhi-local"

    async def test_the_outside_model_is_sent_at_once_when_ours_all_fail(self):
        user, chat = await _make_chat("staged2", "9999930202")
        frames = _Frames()
        interface = ChatInterface(send_json_message_func=frames, chat=chat, user=user)
        ours = _FailingOursModel(name="fhi-local")
        outside = _OutsideModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
        )
        loop = asyncio.get_running_loop()
        started = loop.time()

        with (
            _policy(_POLICY),
            _ours_selectable(),
            _router_returning([ours, outside]),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("What is an appeal?")

        assert frames.last_content() == SECOND_OPINION_REPLY
        assert outside.calls
        assert loop.time() - started < 4.0
        row = await _only_row(chat)
        assert row.external_start == "early"
        assert row.external_delay_seconds == 5.0
        assert row.winner_model == "claude"
        assert row.winner_external is True

    async def test_without_a_policy_every_model_is_asked_together(self):
        user, chat = await _make_chat("staged3", "9999930203")
        frames = _Frames()
        interface = ChatInterface(send_json_message_func=frames, chat=chat, user=user)
        ours = _OursModel(always_reply=FRESH_REPLY, model_quality=110, name="fhi-local")
        outside = _OutsideModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
        )

        # The switch is off under test, so the real reader gives the default.
        with _router_returning([ours, outside]) as router, _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("What is an appeal?")

        router.assert_called_once_with(use_external=True)
        assert frames.last_content() == FRESH_REPLY
        assert outside.calls
        row = await _only_row(chat)
        assert row.external_start == "immediate"
        assert row.external_delay_seconds == 0.0
        assert ("claude", "scored") in _statuses(row)

    async def test_the_policy_is_set_aside_when_none_of_ours_is_selectable(self):
        user, chat = await _make_chat("staged4", "9999930204")
        frames = _Frames()
        interface = ChatInterface(send_json_message_func=frames, chat=chat, user=user)
        ours = _OursModel(always_reply=FRESH_REPLY, model_quality=110, name="fhi-local")
        outside = _OutsideModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
        )

        with (
            _policy(_POLICY),
            _ours_selectable(False),
            _router_returning([ours, outside]),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("What is an appeal?")

        assert outside.calls
        row = await _only_row(chat)
        assert row.external_start == "immediate"
        assert row.external_delay_seconds == 0.0

    async def test_no_delay_or_outside_call_without_consent(self):
        user, chat = await _make_chat("staged5", "9999930205")
        frames = _Frames()
        interface = ChatInterface(
            send_json_message_func=frames,
            chat=chat,
            user=user,
            use_external_models=False,
        )
        ours = _OursModel(always_reply=FRESH_REPLY, model_quality=110, name="fhi-local")

        with (
            _policy(_POLICY),
            _ours_selectable(),
            _router_returning([ours]) as router,
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("What is an appeal?")

        router.assert_called_once_with(use_external=False, policy=_POLICY)
        assert interface._external_delay_seconds == 0.0
        row = await _only_row(chat)
        assert row.use_external is False
        assert row.external_start == ""
        assert row.external_delay_seconds is None
