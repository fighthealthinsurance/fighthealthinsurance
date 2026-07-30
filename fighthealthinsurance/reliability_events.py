"""Sentry capture for total-failure reliability events.

The transport/generation layers log richly, but log lines don't page anyone.
This helper reports the small set of "a user got NOTHING" events to Sentry as
messages with structured context, so total failures alert instead of waiting
for someone to read the zero-appeal diagnostics.

No-op (with a debug log) when sentry_sdk is unavailable or not configured --
the helper must be safe to call from every environment, including tests.
Never pass PHI in ``context``: ids, counts, model names and timings only.
"""

from typing import Any

from loguru import logger


def capture_reliability_event(kind: str, **context: Any) -> None:
    """Report a total-failure event to Sentry. Never raises."""
    try:
        import sentry_sdk

        client = sentry_sdk.get_client()
        if client is None or not client.is_active():
            logger.debug(f"Sentry inactive; skipping reliability event {kind}")
            return
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("reliability_event", kind)
            scope.set_context("reliability", dict(context))
            sentry_sdk.capture_message(
                f"reliability: {kind}",
                level="error",
            )
    except Exception:
        logger.opt(exception=True).debug(f"Failed to capture reliability event {kind}")
