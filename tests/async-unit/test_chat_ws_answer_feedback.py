"""The chat WebSocket's side-by-side answer-feedback frame.

A lightweight ``{"answer_feedback": {"preferred": ...}}`` frame records
which of the two side-by-side answers the user preferred. It must not start
an LLM turn, must not require message content, and must collapse arbitrary
client-supplied values into a bounded metric label set.
"""

import pytest
from channels.testing import WebsocketCommunicator
from prometheus_client import REGISTRY

from fighthealthinsurance.websockets import OngoingChatConsumer


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
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "alternate"},
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
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        await communicator.send_json_to(
            {
                "answer_feedback": {"preferred": "x" * 500},
                "session_key": "feedback-test-2",
            }
        )
        assert await communicator.receive_nothing(timeout=0.5)
    finally:
        await communicator.disconnect()

    assert _feedback_metric("other") == before + 1
