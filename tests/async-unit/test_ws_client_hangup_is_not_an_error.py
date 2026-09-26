"""A user closing their tab must not page anybody -- and only that.

Uvicorn raises ``ClientDisconnected()`` -- with no message -- when a consumer
writes to a peer that has already gone. The chat consumer's catch-all logged
that at ERROR with a traceback and then tried to send an apology down the same
dead socket, which raised again.

Sentry's LoggingIntegration turns an ERROR into an issue, so one closed tab
mid-turn produced a fan-out of them: the send itself (PYTHON-DJANGO-00-M5,
-MT), the "chat generation failed" log, the "failed to generate response" log
fingerprinted by whatever the user had typed (-MH, -MV), and a
``chat_turn_total_failure`` reliability event (-M8).

The classification lives at ONE place, the consumers' send wrapper, which
raises ``ClientGone``. The second half of these tests pins the boundary: an
exception that merely looks like a hangup but did not come from a socket
write (a DB or model-backend "connection reset") is still a server error,
still logged, still answered.
"""

from unittest.mock import AsyncMock, patch

import pytest
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.testing import WebsocketCommunicator
from uvicorn.protocols.utils import ClientDisconnected

from fighthealthinsurance.client_gone import ClientGone
from fighthealthinsurance.websockets import (
    OngoingChatConsumer,
    PerConnectionThreadSensitiveMixin,
)


class TestSendWrapperIsTheBoundary:
    """PerConnectionThreadSensitiveMixin.send converts a failed write."""

    class _Consumer(PerConnectionThreadSensitiveMixin, AsyncWebsocketConsumer):
        pass

    @pytest.mark.asyncio
    async def test_a_write_to_a_departed_peer_raises_client_gone_with_cause(self):
        transport_error = ClientDisconnected()
        with patch.object(
            AsyncWebsocketConsumer, "send", AsyncMock(side_effect=transport_error)
        ):
            with pytest.raises(ClientGone) as raised:
                await self._Consumer().send(text_data="hello")
        assert raised.value.__cause__ is transport_error

    @pytest.mark.asyncio
    async def test_any_other_write_failure_passes_through_untouched(self):
        with patch.object(
            AsyncWebsocketConsumer,
            "send",
            AsyncMock(side_effect=ValueError("frame too large")),
        ):
            with pytest.raises(ValueError):
                await self._Consumer().send(text_data="hello")


async def _drive_chat_turn(payload, *, resolve_side_effect=None):
    """Send one frame to the chat consumer.

    Returns the frame the consumer sent back, or None when it sent nothing.
    ``resolve_side_effect`` makes the chat-type lookup -- work that is NOT a
    socket write -- raise.
    """
    communicator = WebsocketCommunicator(
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        if resolve_side_effect is not None:
            resolve = patch(
                "fighthealthinsurance.websockets.resolve_chat_type",
                side_effect=resolve_side_effect,
            )
        else:
            resolve = patch("builtins.id", side_effect=id)  # no-op patch
        with resolve:
            await communicator.send_json_to(payload)
            if await communicator.receive_nothing(timeout=3):
                return None
            return await communicator.receive_json_from(timeout=3)
    finally:
        await communicator.disconnect()


def _peer_gone_on_write():
    """The consumer's next text frame finds the socket closed."""
    return patch.object(
        AsyncWebsocketConsumer, "send", AsyncMock(side_effect=ClientDisconnected())
    )


# django_db: the consumer's dispatch sweeps DB connections on disconnect
# (aclose_old_connections), and the chat-type lookup reads ChatLeads.
@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_hangup_on_the_reply_is_not_logged_at_error(log_capture):
    # An empty message makes the consumer answer with a validation frame --
    # the first socket write of the turn -- and that write finds the peer gone.
    with log_capture() as cap, _peer_gone_on_write():
        await _drive_chat_turn({"content": "", "session_key": "hangup-not-error"})

    assert cap.messages("ERROR") == []
    assert any(
        "client hung up" in message for message in cap.messages("WARNING")
    ), f"expected a WARNING naming the hangup, got: {cap.messages('WARNING')}"


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_real_server_failure_is_still_reported(log_capture):
    """The downgrade must be narrow: an ordinary bug still logs and answers."""
    detail = "chat-hangup-test-genuine-failure"
    with log_capture() as cap:
        frame = await _drive_chat_turn(
            {"content": "hello", "session_key": "genuine-failure"},
            resolve_side_effect=RuntimeError(detail),
        )

    assert frame is not None and "ref " in frame["error"]
    assert any(
        detail in message for message in cap.messages("ERROR")
    ), f"expected an ERROR for a genuine failure, got: {cap.messages('ERROR')}"


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_reset_that_did_not_come_from_the_socket_is_still_an_error(
    log_capture,
):
    """A ConnectionResetError out of the chat lookup is Postgres or a model
    backend dropping US -- the user is still here and must get the error
    frame. Sniffing the exception type up here would have filed it as a
    departure (review)."""
    with log_capture() as cap:
        frame = await _drive_chat_turn(
            {"content": "hello", "session_key": "reset-from-db"},
            resolve_side_effect=ConnectionResetError("connection reset by peer"),
        )

    assert frame is not None and "ref " in frame["error"]
    assert cap.messages("ERROR"), "a DB-side reset must still be logged at ERROR"


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_hangup_on_a_send_no_handler_covers_is_not_an_error(log_capture):
    """The replay-without-chat-id frame is written before the consumer's own
    try. Before the send wrapper, uvicorn silently swallowed its
    ClientDisconnected; ClientGone is not one, so without the mixin's
    backstop it would reach uvicorn as "Exception in ASGI application" at
    ERROR (review). The communicator surfaces an escaped exception as a
    failure of the app future, so a clean exchange also proves it stayed in.
    """
    communicator = WebsocketCommunicator(
        OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
    )
    connected, _ = await communicator.connect()
    assert connected
    try:
        with log_capture() as cap, _peer_gone_on_write():
            await communicator.send_json_to(
                {"replay": True, "session_key": "backstop-replay"}
            )
            assert await communicator.receive_nothing(timeout=3)
    finally:
        await communicator.disconnect()

    assert cap.messages("ERROR") == []
    assert any(
        "client hung up" in message for message in cap.messages("WARNING")
    ), f"expected the backstop's WARNING, got: {cap.messages('WARNING')}"
