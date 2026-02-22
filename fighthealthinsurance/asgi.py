"""
ASGI config for fighthealthinsurance project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/asgi/
"""

import os
import sys

from fighthealthinsurance.env_utils import get_env_variable

# Use stderr for startup messages since logging may not be configured yet
print("Setting default envs", file=sys.stderr)
env = get_env_variable("DJANGO_CONFIGURATION", get_env_variable("ENVIRONMENT", "Dev"))
print(f"Using env {env}", file=sys.stderr)

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
)
os.environ.setdefault("DJANGO_CONFIGURATION", env)

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

# We make sure we have the env variables configured first
from configurations.asgi import get_asgi_application

from fighthealthinsurance.routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": get_asgi_application(),
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)

# Intentional import after the get_asgi_application is called.

from django.conf import settings

def before_send_filter(event, hint):
    """
    Filter out noisy infrastructure errors from Sentry:
    - Ray client connection errors (transient, auto-recovered)
    - Fuzz guard events (logged locally for investigation)
    """
    from loguru import logger

    # Check logger name for Ray client internal loggers
    logger_name = event.get("logger", "")
    if logger_name in (
        "ray.util.client.logsclient",
        "ray.util.client.dataclient",
    ):
        logger.warning(
            f"Ray client connection issue (filtered from Sentry): {event.get('message', 'unknown')}"
        )
        return None

    # Filter fuzz guard module events (they're logged locally)
    if "fuzz_guard" in logger_name.lower() or "fuzzguard" in logger_name.lower():
        return None

    # Check for fuzz_guard tag set by middleware
    tags = event.get("tags", {})
    if tags.get("fuzz_guard") is True:
        return None

    # Check transaction name for fuzz guard middleware
    transaction = event.get("transaction", "")
    if "FuzzGuardMiddleware" in transaction:
        return None

    # Check for specific gRPC error messages from Ray
    message = event.get("message", "") or ""
    if "Logstream proxy failed to connect" in message:
        logger.warning(
            f"Ray logstream proxy connection failed (filtered from Sentry)"
        )
        return None
    if "Unrecoverable error in data channel" in message:
        logger.warning(f"Ray data channel error (filtered from Sentry)")
        return None

    # Check exception values for Ray gRPC errors
    exception_values = event.get("exception", {}).get("values", [])
    for exc in exception_values:
        exc_value = exc.get("value", "") or ""
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

    return event


if settings.SENTRY_ENDPOINT and not settings.DEBUG:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_ENDPOINT,
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for tracing.
        traces_sample_rate=1.0,
        integrations=[DjangoIntegration()],
        environment=get_env_variable("DJANGO_CONFIGURATION", "production-ish"),
        release=get_env_variable("RELEASE", "unset"),
        before_send=before_send_filter,
        _experiments={
            # Set continuous_profiling_auto_start to True
            # to automatically start the profiler on when
            # possible.
            "continuous_profiling_auto_start": True,
        },
    )
