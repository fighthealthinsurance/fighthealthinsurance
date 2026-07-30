"""The chat WebSocket's catch-all error frame must not leak internals.

A server-side exception used to be echoed to the client verbatim
(``{"error": f"Server error: {str(e)}"}``), which can leak hostnames, SQL,
and model names. Now the client gets a generic message with a short
correlation ref, and the full exception goes to the server log at ERROR.
"""

from unittest.mock import patch

import pytest
from channels.testing import WebsocketCommunicator

from fighthealthinsurance.websockets import OngoingChatConsumer

SECRET_DETAIL = "secret-internal-hostname-db-1 OperationalError"


# django_db: the consumer's dispatch sweeps DB connections on disconnect
# (aclose_old_connections), which needs the test database available when an
# earlier test on the same xdist worker initialized the connection wrapper.
@pytest.mark.django_db
@pytest.mark.asyncio
async def test_chat_ws_error_frame_hides_exception_details():
    with patch(
        "fighthealthinsurance.websockets.resolve_chat_type",
        side_effect=RuntimeError(SECRET_DETAIL),
    ):
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        connected, _ = await communicator.connect()
        assert connected
        try:
            await communicator.send_json_to(
                {"content": "hello", "session_key": "err-shape-test"}
            )
            frame = await communicator.receive_json_from(timeout=10)
        finally:
            await communicator.disconnect()

    assert "error" in frame
    # No exception internals on the wire...
    assert SECRET_DETAIL not in frame["error"]
    assert "RuntimeError" not in frame["error"]
    # ...but a correlation ref the user can report.
    assert "ref " in frame["error"]


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_chat_ws_error_logged_at_error_level(log_capture):
    with patch(
        "fighthealthinsurance.websockets.resolve_chat_type",
        side_effect=RuntimeError(SECRET_DETAIL),
    ):
        communicator = WebsocketCommunicator(
            OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
        )
        connected, _ = await communicator.connect()
        assert connected
        try:
            with log_capture() as cap:
                await communicator.send_json_to(
                    {"content": "hello", "session_key": "err-shape-test-2"}
                )
                await communicator.receive_json_from(timeout=10)
        finally:
            await communicator.disconnect()

    error_messages = cap.messages("ERROR")
    assert any(
        SECRET_DETAIL in m for m in error_messages
    ), f"expected the exception detail in an ERROR log, got: {error_messages}"
