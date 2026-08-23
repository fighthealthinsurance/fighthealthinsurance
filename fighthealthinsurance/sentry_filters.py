"""Sentry ``before_send`` / ``before_send_transaction`` hooks.

These live here rather than in ``asgi.py`` so tests can import them. The
obstacle is not the ``if should_enable_sentry(...)`` block -- the filters
reference nothing from it -- but ``asgi.py``'s module-level side effects:
importing it runs ``os.environ.setdefault`` for the settings module, builds
the ASGI application, and constructs the ``ProtocolTypeRouter``.

Keep this module Django-free at import time. ``asgi.py`` imports it inside
the Sentry block, after Django is configured, but a stray ``from django.conf
import settings`` here would still be a boot hazard if that ever moves.

**Nothing in this module may raise.** sentry-sdk wraps both hooks in
``capture_internal_exceptions()``, so an exception here does not surface
anywhere -- it silently DISCARDS the event. A malformed payload would
therefore stop real errors from reaching Sentry with no signal at all. Every
field is treated as untrusted shape, not just untrusted content: check types
before traversing, and coerce to text with :func:`as_text` before matching.

**What does NOT belong here: unrouted-404 suppression.** Django answers
``Http404`` (and its ``Resolver404`` subclass) itself, without sending
``got_request_exception`` -- the only exception signal ``DjangoIntegration``
hooks -- so scanner probes never arrive as error events in the first place.
Suppressing them belongs in ``sentry_sdk.init(ignore_errors=...)``, which
matches the real exception class rather than a serialized type string. The
noise those probes *do* generate is one transaction per novel path, handled
by :func:`before_send_transaction_filter`.
"""

from typing import Any, Dict, List

# Ray client connection failures are transient infrastructure noise: Ray
# reconnects on its own, so there is nothing to action in Sentry. They are
# still logged locally. (Ray's *logger names* are suppressed upstream via
# sentry_sdk.integrations.logging.ignore_logger in asgi.py; what remains here
# is the gRPC failure text, which arrives under other loggers.)
RAY_MESSAGE_MARKERS = (
    ("Logstream proxy failed to connect", "Ray logstream proxy connection failed"),
    ("Unrecoverable error in data channel", "Ray data channel error"),
)

# Channels raises this for a websocket path that matches no route. It escapes
# the ASGI app, uvicorn logs it at ERROR with exc_info, and the default
# LoggingIntegration turns that into an event -- one new issue per probe path,
# with the client-supplied path embedded in the message. Same background-noise
# class as an HTTP scanner probe, and the same reasoning applies: a websocket
# route regression must be caught by the connection-failure rate, not by
# waiting for a scanner-shaped error to look different.
UNROUTED_WEBSOCKET_MARKER = "No route found for path"

# sentry-sdk's DjangoIntegration names a transaction after the matched route
# pattern (source "route"). It falls back to the raw, client-controlled
# request path with source "url" only when URL resolution failed -- so this
# value is exactly "this request matched nothing".
UNROUTED_TRANSACTION_SOURCE = "url"


# sentry's logentry/Message interface renders text into "formatted" and keeps
# the pre-substitution template in "message". Everything else it carries --
# "params" above all -- is metadata, not message text.
MESSAGE_TEXT_KEYS = ("formatted", "message")


def as_text(value: Any) -> str:
    """The *message text* of a payload field, for substring matching.

    Deliberately narrow. Stringifying the whole object and matching that
    would drop a legitimate error whose metadata merely quotes a marker --
    ``{"message": "Failed to process %s", "params": ["Logstream proxy failed
    to connect"]}`` is an application failure, not Ray noise, and so is any
    event that happens to carry a marker in a dict *key*. Dropping is the
    destructive outcome, so matching here stays as narrow as the rest of this
    module: only the rendered text, never params or other metadata.

    ``"marker" in value`` cannot be used directly either: it raises
    ``TypeError`` on a non-iterable (an int message) and silently tests
    *keys* on a dict.

    Invoking no user-supplied code -- no ``__str__``, ``__bool__`` or
    ``__len__`` -- is what makes this unable to raise, which the module
    docstring explains is mandatory. That is a stronger guarantee than
    catching the raise after the fact.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            for key in MESSAGE_TEXT_KEYS:
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
        except Exception:  # pragma: no cover - an exotic dict subclass
            return ""
    return ""


def exception_values(event: Any) -> List[Dict[str, Any]]:
    """The event's exception entries, defensively.

    ``event.get("exception", {})`` is not enough at any level. The key can be
    present with a ``None`` value (``None.get`` raises); ``exception`` can be
    a truthy non-dict -- serialization can substitute a scrubbed string --
    where ``str.get`` raises; ``values`` can be a non-iterable, where the
    comprehension raises; and an individual entry can be a scrubbed string,
    where ``str.get`` raises again. Each of those would discard the event
    rather than surface, so every level is type-checked and a malformed
    payload yields "no exceptions found", which keeps the event.
    """
    if not isinstance(event, dict):
        return []
    exception = event.get("exception")
    if not isinstance(exception, dict):
        return []
    values = exception.get("values")
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if isinstance(value, dict)]


def before_send_filter(event: Any, hint: Any) -> Any:
    """Drop known-noise error events. Returns the event, or None to discard.

    ``event``/``hint`` are ``Any`` on purpose. sentry-sdk types these hooks
    against its own ``Event`` TypedDict, which lives behind ``TYPE_CHECKING``
    and under a private module path -- and the SDK is unpinned. Claiming a
    concrete dict shape here would also contradict the point of the function:
    it exists because the payload cannot be trusted to have one.
    """
    from loguru import logger

    if not isinstance(event, dict):
        return event

    message = as_text(event.get("message"))
    for marker, description in RAY_MESSAGE_MARKERS:
        if marker in message:
            logger.warning(f"{description} (filtered from Sentry)")
            return None

    for exc in exception_values(event):
        exc_value = as_text(exc.get("value"))
        if "Logstream proxy failed to connect" in exc_value:
            logger.warning(
                f"Ray gRPC logstream error (filtered from Sentry): {exc_value[:200]}"
            )
            return None
        if "grpc_status:5" in exc_value and "Channel for client" in exc_value:
            logger.warning(
                f"Ray gRPC channel error (filtered from Sentry): {exc_value[:200]}"
            )
            return None
        # Narrow on purpose: only channels' own "nothing matched" ValueError,
        # never a ValueError raised inside a consumer.
        if exc.get("type") == "ValueError" and UNROUTED_WEBSOCKET_MARKER in exc_value:
            # debug, not info: the LogAnalyticsHandler sink is attached at
            # INFO and ships one un-batched POST per record through a bounded
            # queue, so an INFO line here would hand a scanner the fill rate
            # of the log pipeline. stderr keeps it visible locally.
            logger.debug("Unrouted websocket path (filtered from Sentry)")
            return None

    if UNROUTED_WEBSOCKET_MARKER in message:
        logger.debug("Unrouted websocket path (filtered from Sentry)")
        return None

    return event


def before_send_transaction_filter(event: Any, hint: Any) -> Any:
    """Drop transactions for requests that matched no URL route.

    This is the half of the scanner-noise problem that ``before_send`` cannot
    reach: sentry-sdk calls ``before_send`` only for error events, so at
    ``traces_sample_rate=1.0`` every probe still shipped a transaction named
    after the raw path. That both fingerprinted each novel path as its own
    entry and wrote a client-controlled string -- a mistyped follow-up link
    still carries its token and hashed email -- into Sentry.

    Routed requests carry source "route" and are kept; see
    :data:`UNROUTED_TRANSACTION_SOURCE`. Anything whose shape we cannot read
    is also kept: dropping is the destructive outcome, so it needs a positive
    match, never a failure to parse.
    """
    if not isinstance(event, dict):
        return event
    transaction_info = event.get("transaction_info")
    if not isinstance(transaction_info, dict):
        return event
    if transaction_info.get("source") == UNROUTED_TRANSACTION_SOURCE:
        return None
    return event
