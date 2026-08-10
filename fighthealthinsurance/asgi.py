"""
ASGI config for fighthealthinsurance project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/asgi/
"""

import os
import re
import sys

from fighthealthinsurance.env_utils import get_env_variable, should_enable_sentry

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
from fighthealthinsurance.ws_origin import allowed_hosts_browser_origin_validator

_ws_stack = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
# Cross-site WebSocket protection: browsers send an Origin header on WS
# handshakes, and without validation any site a user visits can open an
# authenticated socket to these consumers. Supplied origins are enforced
# against ALLOWED_HOSTS; a MISSING Origin (non-browser clients, probes) is
# allowed -- see ws_origin.BrowserOriginValidator for why that loses no
# protection (channels' stock validator would reject those). Env-gated
# (default ON) so an Origin-rewriting proxy can be unblocked without a
# deploy while it gets fixed.
if get_env_variable("FHI_WS_ENFORCE_ORIGIN", "true").lower() not in (
    "false",
    "0",
    "no",
):
    _ws_stack = allowed_hosts_browser_origin_validator(_ws_stack)

application = ProtocolTypeRouter(
    {
        "http": get_asgi_application(),
        "websocket": _ws_stack,
    }
)

# Intentional import after the get_asgi_application is called.

from django.conf import settings

# Sentry only fires from real (non-local) deployments. "endpoint set and
# DEBUG off" is not enough: dev machines routinely carry the production
# SENTRY_ENDPOINT plus a Prod DJANGO_CONFIGURATION (copied .env files, and
# editors like Cursor export .env into the process env), which used to tag
# every local hiccup as a production error. should_enable_sentry additionally
# requires a deployment marker: KUBERNETES_SERVICE_HOST (present in every
# k8s pod) or an explicit FHI_DEPLOYED=1.
if should_enable_sentry(settings.SENTRY_ENDPOINT, settings.DEBUG):
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    def before_send_filter(event, hint):
        """
        Filter out Ray client connection errors that are transient infrastructure
        issues. Ray handles reconnection automatically, so these are noisy but
        not actionable in Sentry. They're logged locally for debugging.
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

        # Drop URL-resolution 404s. These are internet background noise:
        # scanners probing for exposed secrets (.bashrc, api/.env, key.pem,
        # wp-login.php...). Django refuses them correctly, so there is nothing
        # to action -- but each novel probe path is a NEW Sentry issue, which
        # means a fresh alert for something nobody can or should fix.
        #
        # Deliberately narrow: only the resolver's "no route matched" error.
        # A 404 raised deliberately inside a view (a missing appeal, an expired
        # link) is a different class and still reports, because that one can
        # indicate a real bug. IGNORABLE_404_URLS in settings covers Django's
        # own 404 mail; this is the equivalent for Sentry.
        for exc in exception_values:
            if exc.get("type") == "Resolver404":
                path = ""
                match = re.search(r"'path':\s*'([^']*)'", exc.get("value", "") or "")
                if match:
                    path = match.group(1)
                logger.info(f"Unrouted path (filtered from Sentry): {path[:100]!r}")
                return None

        return event

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
elif settings.SENTRY_ENDPOINT:
    # Startup-visible breadcrumb for "why aren't my errors in Sentry?"
    print(
        "SENTRY_ENDPOINT is set but Sentry stays off: "
        + (
            "DEBUG is on"
            if settings.DEBUG
            else "not a deployed environment "
            "(no KUBERNETES_SERVICE_HOST; set FHI_DEPLOYED=1 to override)"
        ),
        file=sys.stderr,
    )

# Optional Azure Log Analytics shipping. Activates only when both workspace
# ID and shared key are configured; safe to leave installed otherwise.
from fighthealthinsurance import log_analytics as _log_analytics

if _log_analytics.is_log_analytics_enabled():
    import logging as _logging

    from loguru import logger as _loguru_logger

    # Attach only to loguru: dj_easy_log's load_loguru() installs an
    # InterceptHandler on the root stdlib logger that forwards stdlib records
    # into loguru, so registering the handler on both sinks would double-ship.
    _la_handler = _log_analytics.LogAnalyticsHandler(level=_logging.INFO)
    # backtrace/diagnose off, same as the Prod stderr sink (settings.py):
    # loguru's defaults annotate every traceback frame with local variable
    # values, which would ship prompts/PHI/API tokens off-box to Log
    # Analytics on any logger.opt(exception=True) call.
    _loguru_logger.add(_la_handler, level="INFO", backtrace=False, diagnose=False)
