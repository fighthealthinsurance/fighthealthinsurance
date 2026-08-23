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
from channels.testing import WebsocketCommunicator
from prometheus_client import REGISTRY

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
