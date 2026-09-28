"""The chat WebSocket's side-by-side answer-feedback frame.

A lightweight ``{"answer_feedback": {"preferred": ...}}`` frame records
which of the two side-by-side answers the user preferred. It must not start
an LLM turn, must not require message content, and must collapse arbitrary
client-supplied values into a bounded metric label set.

Feedback only counts for the chat THIS socket has open (the real client
sends it on the connection the answer arrived on): without that gate any
anonymous connection could loop forged "alternate preferred" frames and
skew the model-preference signal before any identity/chat validation ran.
"""

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from prometheus_client import REGISTRY

from fighthealthinsurance.models import ChatTurn, OngoingChat
from fighthealthinsurance.websockets import OngoingChatConsumer

OPEN_CHAT_ID = "feedback-chat-1"


class _ConsumerWithOpenChat(OngoingChatConsumer):
    """Consumer that already resolved a chat on this connection.

    ``chat_id`` is a class-level default on OngoingChatConsumer, normally
    assigned per-instance once the chat is created/replayed; presetting it
    here stands in for that handshake without running an LLM turn.
    """

    chat_id = OPEN_CHAT_ID


def _feedback_metric(preferred):
    return (
        REGISTRY.get_sample_value(
            "fhi_chat_answer_feedback_total", {"preferred": preferred}
        )
        or 0.0
    )


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_answer_feedback_records_metric_without_llm_turn():
    before = _feedback_metric("alternate")
    communicator = WebsocketCommunicator(
        _ConsumerWithOpenChat.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "alternate"},
                "chat_id": OPEN_CHAT_ID,
                "session_key": "feedback-test",
            }
        )
        # The branch returns silently: no reply frame and no error frame
        # ("Message content is required") may be produced.
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        await communicator.disconnect()

    assert _feedback_metric("alternate") == before + 1


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_answer_feedback_bounds_unexpected_values():
    before = _feedback_metric("other")
    communicator = WebsocketCommunicator(
        _ConsumerWithOpenChat.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "x" * 500},
                "chat_id": OPEN_CHAT_ID,
                "session_key": "feedback-test-2",
            }
        )
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        await communicator.disconnect()

    assert _feedback_metric("other") == before + 1


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_answer_feedback_without_open_chat_is_ignored():
    """A connection that never resolved a chat cannot pump the metric."""
    before = _feedback_metric("alternate")
    communicator = WebsocketCommunicator(
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "alternate"},
                "session_key": "feedback-test-3",
            }
        )
        # Still silent -- ignored, not an error.
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        await communicator.disconnect()

    assert _feedback_metric("alternate") == before


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_answer_feedback_for_foreign_chat_is_ignored():
    """Feedback naming a chat other than this socket's does not count."""
    before = _feedback_metric("alternate")
    communicator = WebsocketCommunicator(
        _ConsumerWithOpenChat.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "alternate"},
                "chat_id": "some-other-chat",
                "session_key": "feedback-test-4",
            }
        )
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        await communicator.disconnect()

    assert _feedback_metric("alternate") == before


# --- The pick stored on the turn's ChatTurn row ----------------------------
#
# Frames that carry the turn_id from the answer frame store the pick on that
# turn's row, alongside the metric above (which counts every accepted frame).


async def _chat_with_offered_turn():
    chat = await sync_to_async(OngoingChat.objects.create)(
        chat_history=[], summary_for_next_call=[]
    )
    turn = await sync_to_async(ChatTurn.objects.create)(
        chat=chat,
        outcome="ok",
        use_external=True,
        winner_model="model-a",
        alternate_offered=True,
        alternate_model="model-b",
        alternate_cross_model=True,
    )
    return chat, turn


async def _send_feedback(socket_chat_id, frames):
    """Send each frame on one socket whose open chat is socket_chat_id."""
    consumer = type(
        "_ConsumerForThisChat", (OngoingChatConsumer,), {"chat_id": socket_chat_id}
    )
    communicator = WebsocketCommunicator(consumer.as_asgi(), "/ws/ongoing-chat/")
    connected, _ = await communicator.connect()
    assert connected
    try:
        for frame in frames:
            await communicator.send_json_to(frame)
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        # Waits for the consumer to finish handling every frame.
        await communicator.disconnect()


def _frame(chat_id, preferred, turn_id):
    return {
        "answer_feedback": {"preferred": preferred, "turn_id": turn_id},
        "chat_id": chat_id,
        "session_key": "feedback-turn-test",
    }


async def _preferred(turn):
    return (await sync_to_async(ChatTurn.objects.get)(pk=turn.pk)).preferred


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pick_with_its_turn_id_lands_on_the_turn():
    chat, turn = await _chat_with_offered_turn()
    before = _feedback_metric("alternate")
    await _send_feedback(
        str(chat.id), [_frame(str(chat.id), "alternate", str(turn.id))]
    )
    assert await _preferred(turn) == "alternate"
    # The metric is counted exactly as before.
    assert _feedback_metric("alternate") == before + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_second_pick_for_the_same_turn_is_ignored():
    chat, turn = await _chat_with_offered_turn()
    await _send_feedback(
        str(chat.id),
        [
            _frame(str(chat.id), "primary", str(turn.id)),
            _frame(str(chat.id), "alternate", str(turn.id)),
        ],
    )
    assert await _preferred(turn) == "primary"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pick_for_another_chats_turn_is_ignored():
    """The socket's own chat decides, whatever turn_id the frame names."""
    mine, _my_turn = await _chat_with_offered_turn()
    _theirs, their_turn = await _chat_with_offered_turn()
    await _send_feedback(
        str(mine.id), [_frame(str(mine.id), "alternate", str(their_turn.id))]
    )
    assert await _preferred(their_turn) == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pick_naming_a_foreign_chat_is_ignored():
    chat, turn = await _chat_with_offered_turn()
    other, _ = await _chat_with_offered_turn()
    await _send_feedback(
        str(other.id), [_frame(str(chat.id), "alternate", str(turn.id))]
    )
    assert await _preferred(turn) == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_bad_turn_id_is_ignored_but_still_counted():
    chat, turn = await _chat_with_offered_turn()
    before = _feedback_metric("primary")
    await _send_feedback(
        str(chat.id),
        [
            _frame(str(chat.id), "primary", "not-a-uuid"),
            _frame(str(chat.id), "primary", {"id": str(turn.id)}),
        ],
    )
    assert await _preferred(turn) == ""
    assert _feedback_metric("primary") == before + 2
