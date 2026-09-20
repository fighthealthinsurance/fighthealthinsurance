"""The client hanging up is not a server error.

Uvicorn's sans-io websocket implementation raises ``ClientDisconnected()``
with an EMPTY message when we write to a peer that has gone away. The
codebase used to classify disconnects by matching substrings of
``str(exception)``, so from that switch onwards every hangup mid-stream was
filed at ERROR with a traceback -- a Sentry issue per tab anybody closed
(PYTHON-DJANGO-00-M4, -M5, -MT, and the chat fan-out behind -MH/-MV/-M8).

These tests pin the type-based classification that replaced it, including the
two directions that matter: a real transport hangup is recognised even with no
message, and a server-side failure that merely *mentions* a reset connection
is not.
"""

import pytest

from fighthealthinsurance.client_gone import (
    client_is_gone,
    message_means_client_gone,
)


class TestClientIsGone:
    def test_uvicorn_client_disconnected_with_no_message_is_a_hangup(self):
        """The whole point: ClientDisconnected carries no text to match."""
        from uvicorn.protocols.utils import ClientDisconnected

        exc = ClientDisconnected()
        assert str(exc) == ""
        assert client_is_gone(exc)

    def test_a_class_named_like_a_disconnect_is_a_hangup(self):
        """A dependency that moves the class still gets classified."""

        class ClientDisconnected(Exception):
            pass

        assert client_is_gone(ClientDisconnected())

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError(),
            BrokenPipeError(),
            ConnectionAbortedError(),
        ],
    )
    def test_builtin_transport_errors_are_hangups(self, exc):
        assert client_is_gone(exc)

    def test_uvloops_untyped_closed_transport_runtimeerror_is_a_hangup(self):
        """uvloop reports a write to a closed socket as a bare RuntimeError."""
        exc = RuntimeError(
            "unable to perform operation on <TCPTransport closed=True "
            "reading=False 0x55d2>; the handler is closed"
        )
        assert client_is_gone(exc)

    def test_a_disconnect_wrapped_by_another_exception_is_a_hangup(self):
        from uvicorn.protocols.utils import ClientDisconnected

        try:
            try:
                raise ClientDisconnected()
            except ClientDisconnected as inner:
                raise RuntimeError("sending the status frame failed") from inner
        except RuntimeError as outer:
            assert client_is_gone(outer)

    def test_an_unrelated_failure_raised_while_handling_one_is_not_a_hangup(self):
        """Python chains ``__context__`` implicitly; following it would
        downgrade a real bug that merely happened after a swallowed hangup."""
        from uvicorn.protocols.utils import ClientDisconnected

        try:
            try:
                raise ClientDisconnected()
            except ClientDisconnected:
                raise ValueError("the appeal row was missing a denial")
        except ValueError as outer:
            assert outer.__context__ is not None
            assert not client_is_gone(outer)

    def test_an_ordinary_server_failure_is_not_a_hangup(self):
        assert not client_is_gone(ValueError("the model returned nothing"))
        assert not client_is_gone(TimeoutError("backend took too long"))

    def test_none_is_not_a_hangup(self):
        assert not client_is_gone(None)

    def test_a_cyclic_cause_chain_terminates(self):
        """Bounded walk: a self-referencing chain must not hang the logger."""
        first = RuntimeError("model failure")
        second = RuntimeError("wrapped")
        first.__cause__ = second
        second.__cause__ = first
        assert not client_is_gone(first)


class TestMessageMeansClientGone:
    def test_an_empty_message_matches_nothing(self):
        """Why type matching had to exist -- see the module docstring."""
        assert not message_means_client_gone("")
        assert not message_means_client_gone(None)

    def test_transport_text_matches(self):
        assert message_means_client_gone("[Errno 104] Connection reset by peer")
        assert message_means_client_gone("BrokenPipeError: Broken pipe")

    def test_unrelated_text_does_not_match(self):
        assert not message_means_client_gone("the model returned nothing")
