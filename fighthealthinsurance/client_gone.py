"""Telling "the user closed the tab" apart from "the server broke".

A websocket write to a peer that has already gone away raises out of the
consumer exactly like a genuine failure does, so something has to classify
the two. For a long time that was substring matching on ``str(exception)``:
uvloop raises a bare ``RuntimeError`` ("unable to perform operation on
<TCPTransport closed=True ...>; the handler is closed") when we write to a
closed socket, and the asyncio selector loop surfaces the same condition as
``ConnectionResetError`` / ``BrokenPipeError``. Every one of those carries
matchable text.

Uvicorn's sans-io websocket implementation
(``uvicorn.protocols.websockets.websockets_sansio_impl``) does not: it raises
``ClientDisconnected()`` with **no message at all**. Every string marker
missed it, and from that switch onwards each hangup mid-stream was logged at
ERROR with a traceback -- which is to say, reported to Sentry as a server
fault. One user closing a tab during a chat turn fanned out into five
separate issues (the send, the turn, the "failed to generate" log, the
reliability event, and the error frame that failed to send in turn).

So classify by TYPE, which is what actually separates the two cases, and keep
the message markers underneath for the callers that have nothing but a string
left and for uvloop's untyped ``RuntimeError``.

Where the classification runs matters as much as how. ``client_is_gone`` is
applied at ONE place: the consumers' socket-write wrapper
(``websockets.PerConnectionThreadSensitiveMixin.send``), which turns a write
that failed because the peer left into :class:`ClientGone`. Everything above
that boundary checks ``isinstance(e, ClientGone)`` and nothing else. Sniffing
an arbitrary exception higher up is how a Postgres "connection reset by peer"
raised inside generation, or a ``ConnectionResetError`` from a model backend,
would be misfiled as "the user left" -- a real outage downgraded to a
WARNING with no error frame sent to a user who is still there. Only a socket
write can be the client going away, so only the socket write gets to say so.

Deliberately conservative in one direction: this only ever DOWNGRADES a log,
so a false positive hides a real failure while a false negative merely leaves
noise. Everything matched here is raised by the transport once the peer is
already gone -- never by our own code, never by the ORM, never by a model
call.
"""

from typing import Optional, Tuple


class ClientGone(Exception):
    """A socket write failed because the peer had already left.

    Raised only by the consumers' send wrapper, with the transport's own
    exception as ``__cause__``. Being a distinct type is the point: a handler
    that catches it knows the failure was OUR write to THEIR closed socket,
    not something that merely looks like one, and can end the stream quietly
    (warning, no traceback, no error frame -- there is nobody to send it to).
    """


def _optional_disconnect_types() -> Tuple[type, ...]:
    """Transport-hangup classes from dependencies, if they are importable.

    A dependency that moved or renamed one of these must not break
    classification, and must not break import of this module either: a miss
    here falls through to the name check below.

    Deliberately NOT included: channels' ``StopConsumer``. It means "unwind
    this consumer", not "this write failed", and callers here respond to a
    hangup by swallowing the exception -- swallowing a ``StopConsumer`` would
    leave the consumer running.
    """
    found: Tuple[type, ...] = ()
    for module_name, symbol in (
        # What uvicorn raises today on a write to a departed websocket peer.
        ("uvicorn.protocols.utils", "ClientDisconnected"),
        # The websockets library's own close signalling, which reaches us when
        # a consumer talks to the protocol directly.
        ("websockets.exceptions", "ConnectionClosed"),
    ):
        try:
            candidate = getattr(__import__(module_name, fromlist=[symbol]), symbol)
        except Exception:  # pragma: no cover - see docstring
            continue
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found += (candidate,)
    return found


# Builtin transport errors. ``ClientDisconnected`` is NOT among them: it
# subclasses OSError directly, not ConnectionError, so listing ConnectionError
# alone would miss exactly the case this module exists for.
_DISCONNECT_TYPES: Tuple[type, ...] = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
) + _optional_disconnect_types()

# Fallback for the imports above: if a version bump moves one of those
# classes, matching its name keeps the noise suppressed instead of silently
# resurrecting it. Names only, never a bare substring of the message.
_DISCONNECT_TYPE_NAMES = frozenset(
    {
        "ClientDisconnected",
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "WebSocketDisconnect",
    }
)

# Substrings that mark an error as the CLIENT going away rather than a
# server/model failure, for the untyped cases: uvloop's closed-transport
# RuntimeError, and callers that kept only ``str(exception)``.
DISCONNECT_MESSAGE_MARKERS = (
    "the handler is closed",
    "unable to perform operation on",
    "tcptransport closed",
    "connection reset",
    "connection lost",
    "broken pipe",
    "connectionreseterror",
    "clientdisconnected",
)

# A ``__cause__`` chain is normally one or two links. Bounded, and with a
# seen-set, so a hand-built cycle can't turn a log call into a hang.
_MAX_CHAIN_DEPTH = 10


def message_means_client_gone(text: Optional[str]) -> bool:
    """True when error *text* names a transport that the peer already closed.

    The weaker half of the classification: available to callers holding only
    a string, and the only thing that catches uvloop's untyped RuntimeError.
    An empty string is not a match -- ``ClientDisconnected`` has no message,
    which is precisely why :func:`client_is_gone` exists.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in DISCONNECT_MESSAGE_MARKERS)


def _one_is_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, _DISCONNECT_TYPES):
        return True
    if type(exc).__name__ in _DISCONNECT_TYPE_NAMES:
        return True
    return message_means_client_gone(str(exc))


def client_is_gone(exc: Optional[BaseException]) -> bool:
    """True when *exc* means the peer hung up, not that we failed.

    For the send boundary (see the module docstring): call this on an
    exception raised by a socket write, not on one caught further up.

    A bare ``ClientDisconnected`` matches on the first link; ``__cause__`` is
    followed so a disconnect deliberately re-raised inside a wrapper
    (``raise ... from``) is still recognised.

    ``__context__`` is NOT followed. Python sets it implicitly on anything
    raised inside an ``except`` block, so a genuine bug that happens to be
    raised while handling a swallowed disconnect would inherit the
    classification and be quietly downgraded. Following only the explicit
    link keeps this in the direction the module docstring commits to.
    """
    seen: set = set()
    current: Optional[BaseException] = exc
    for _ in range(_MAX_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        try:
            if _one_is_disconnect(current):
                return True
        except Exception:  # pragma: no cover - an exotic __str__
            # Classification is a logging decision; never let it raise into
            # the error path it is being used to describe.
            return False
        current = current.__cause__
    return False
