"""
ASGI config for fighthealthinsurance project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/asgi/
"""

import os
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
    from django.urls import Resolver404
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import ignore_logger

    from fighthealthinsurance.sentry_filters import (
        before_send_filter,
        before_send_transaction_filter,
    )

    # Ray client internals chatter on these two loggers while reconnecting.
    # ignore_logger drops them inside LoggingIntegration, before an event is
    # built at all -- cheaper and less brittle than matching event["logger"]
    # in before_send.
    ignore_logger("ray.util.client.logsclient")
    ignore_logger("ray.util.client.dataclient")

    sentry_sdk.init(
        dsn=settings.SENTRY_ENDPOINT,
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for tracing.
        traces_sample_rate=1.0,
        integrations=[DjangoIntegration()],
        environment=get_env_variable("DJANGO_CONFIGURATION", "production-ish"),
        release=get_env_variable("RELEASE", "unset"),
        # Scanner probes (.bashrc, api/.env, key.pem, wp-login.php...) are
        # internet background noise. ignore_errors matches the real exception
        # class off hint["exc_info"], so a deliberate Http404 from a view --
        # a missing appeal, an expired link -- is untouched.
        #
        # Be clear about what this does NOT buy: as configured, an unrouted
        # 404 never reaches Sentry as an error in the first place. Django
        # answers Http404 (and its Resolver404 subclass) itself without
        # sending got_request_exception, which is the only exception signal
        # DjangoIntegration hooks, and DjangoIntegration()'s
        # failed_request_status_codes defaults to the 5xx range. This is a
        # guard for if that ever changes. The probe noise that DOES reach
        # Sentry is one transaction per novel path, which
        # before_send_transaction removes below.
        #
        # Consequence worth knowing: a mass-404 outage (a dropped include(),
        # a changed prefix) is invisible to Sentry either way, so it has to be
        # alerted on from the django_prometheus 404 rate -- django_prometheus
        # is already in MIDDLEWARE -- and never from the absence of errors.
        ignore_errors=[Resolver404],
        before_send=before_send_filter,
        before_send_transaction=before_send_transaction_filter,
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
