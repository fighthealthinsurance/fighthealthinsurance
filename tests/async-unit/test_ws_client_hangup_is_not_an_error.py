"""A user closing their tab must not page anybody.

Uvicorn raises ``ClientDisconnected()`` -- with no message -- when a consumer
writes to a peer that has already gone. The chat consumer's catch-all logged
that at ERROR with a traceback and then tried to send an apology down the same
dead socket, which raised again.

Sentry's LoggingIntegration turns an ERROR into an issue, so one closed tab
mid-turn produced a fan-out of them: the send itself (PYTHON-DJANGO-00-M5,
-MT), the "chat generation failed" log, the "failed to generate response" log
fingerprinted by whatever the user had typed (-MH, -MV), and a
``chat_turn_total_failure`` reliability event (-M8).
"""

from unittest.mock import patch

import pytest
from channels.testing import WebsocketCommunicator
from uvicorn.protocols.utils import ClientDisconnected

from fighthealthinsurance.websockets import OngoingChatConsumer


async def _drive_chat_turn_raising(exc, session_key):
    """Run one chat turn whose work raises ``exc``.

    Returns the frame the consumer sent back, or None when it sent nothing.
    """
    communicator = WebsocketCommunicator(
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        with patch(
            "fighthealthinsurance.websockets.resolve_chat_type", side_effect=exc
        ):
            await communicator.send_json_to(
                {"content": "hello", "session_key": session_key}
            )
            if await communicator.receive_nothing(timeout=3):
                return None
            return await communicator.receive_json_from(timeout=3)
    finally:
        await communicator.disconnect()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_hangup_is_not_logged_at_error(log_capture):
    with log_capture() as cap:
        await _drive_chat_turn_raising(ClientDisconnected(), "hangup-not-error")

    assert cap.messages("ERROR") == []
    assert any(
        "client hung up" in message for message in cap.messages("WARNING")
    ), f"expected a WARNING naming the hangup, got: {cap.messages('WARNING')}"


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_hangup_sends_no_error_frame():
    """Writing to the socket that just died raises the same exception again."""
    frame = await _drive_chat_turn_raising(ClientDisconnected(), "hangup-no-frame")
    assert frame is None


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_real_server_failure_is_still_reported(log_capture):
    """The downgrade must be narrow: an ordinary bug still logs and answers."""
    detail = "chat-hangup-test-genuine-failure"
    with log_capture() as cap:
        frame = await _drive_chat_turn_raising(RuntimeError(detail), "genuine-failure")

    assert frame is not None and "ref " in frame["error"]
    assert any(
        detail in message for message in cap.messages("ERROR")
    ), f"expected an ERROR for a genuine failure, got: {cap.messages('ERROR')}"
