"""How a cancelled chat turn is recorded (chat/turn_record.py).

A ChatTurn row follows fhi_chat_turns_total: a turn cancelled before the
metric counted it gets no row, and one cancelled after it was counted keeps
its row with the counted outcome, written from a thread of its own. These
run with real transactions because that write uses its own connection.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY

from fighthealthinsurance.chat import turn_record
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import ChatTurn, OngoingChat
from tests.chat_fixtures import FRESH_REPLY, RecordingChatModel, seeded_history

OUTCOMES = ("ok", "failed", "timeout")


def _counted():
    return {
        outcome: REGISTRY.get_sample_value("fhi_chat_turns_total", {"outcome": outcome})
        or 0.0
        for outcome in OUTCOMES
    }


def _router(models):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
        return_value=(models, []),
    )


def _no_background_tasks():
    return patch(
        "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )


class _Frames:
    """The socket. With ``stall_on_content`` the reply frame never goes out,
    as when the connection drops while it is being sent."""

    def __init__(self, stall_on_content=False):
        self.frames = []
        self.sending_reply = asyncio.Event()
        self._stall = stall_on_content

    async def __call__(self, frame):
        self.frames.append(frame)
        if "content" in frame:
            self.sending_reply.set()
            if self._stall:
                await asyncio.sleep(30)


async def _chat():
    return await OngoingChat.objects.acreate(
        chat_history=seeded_history(), summary_for_next_call=[]
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_cancelled_while_the_models_answer_gets_no_row_and_no_count():
    """The metric does not count a turn cancelled mid-generation, so it has
    no outcome to record and gets no row."""
    chat = await _chat()
    asked = asyncio.Event()

    class _Stalled(RecordingChatModel):
        async def generate_chat_response(self, *args, **kwargs):
            asked.set()
            await asyncio.sleep(30)

    interface = ChatInterface(send_json_message_func=_Frames(), chat=chat, user=None)
    before = _counted()

    with _router([_Stalled(name="stalled-backend")]), _no_background_tasks():
        task = asyncio.create_task(interface.handle_chat_message("CA"))
        await asyncio.wait_for(asked.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    assert await ChatTurn.objects.filter(chat=chat).acount() == 0
    assert _counted() == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_cancelled_while_its_reply_goes_out_keeps_its_counted_row():
    """The metric counted the turn "ok" before the reply frame went out, so
    the row is written with that outcome, by the isolated writer."""
    chat = await _chat()
    frames = _Frames(stall_on_content=True)
    interface = ChatInterface(send_json_message_func=frames, chat=chat, user=None)
    model = RecordingChatModel(always_reply=FRESH_REPLY, name="answering-backend")
    isolated_writes = []
    real_isolated = turn_record.arecord_chat_turn_isolated

    async def isolated(chat_id, turn, outcome, *args, **kwargs):
        isolated_writes.append(outcome)
        return await real_isolated(chat_id, turn, outcome, *args, **kwargs)

    before = _counted()
    with (
        _router([model]),
        _no_background_tasks(),
        patch(
            "fighthealthinsurance.chat_interface.arecord_chat_turn_isolated",
            isolated,
        ),
    ):
        task = asyncio.create_task(interface.handle_chat_message("CA"))
        await asyncio.wait_for(frames.sending_reply.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    rows = [row async for row in ChatTurn.objects.filter(chat=chat)]
    assert [(row.outcome, row.winner_model) for row in rows] == [
        ("ok", "answering-backend")
    ]
    assert isolated_writes == ["ok"]
    after = _counted()
    assert after["ok"] == before["ok"] + 1
    assert (after["failed"], after["timeout"]) == (before["failed"], before["timeout"])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_cancelled_while_its_row_is_written_keeps_its_row():
    """The turn is cancelled while its ordinary row write still waits for
    the chat's executor, so that write never runs. The counted row is
    written by the isolated writer instead."""
    chat = await _chat()
    interface = ChatInterface(send_json_message_func=_Frames(), chat=chat, user=None)
    model = RecordingChatModel(always_reply=FRESH_REPLY, name="answering-backend")
    writing = asyncio.Event()

    async def queued_write(*args, **kwargs):
        writing.set()
        await asyncio.sleep(30)

    with (
        _router([model]),
        _no_background_tasks(),
        patch("fighthealthinsurance.chat_interface.arecord_chat_turn", queued_write),
    ):
        task = asyncio.create_task(interface.handle_chat_message("CA"))
        await asyncio.wait_for(writing.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    rows = [row async for row in ChatTurn.objects.filter(chat=chat)]
    assert [(row.outcome, row.winner_model) for row in rows] == [
        ("ok", "answering-backend")
    ]
