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

# We make sure we have the env variables configured first
from configurations.asgi import get_asgi_application
from fighthealthinsurance.routing import websocket_urlpatterns
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

application = ProtocolTypeRouter(
    {
        "http": get_asgi_application(),
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)

# Intentional import after the get_asgi_application is called.

from django.conf import settings

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
        _experiments={
            # Set continuous_profiling_auto_start to True
            # to automatically start the profiler on when
            # possible.
            "continuous_profiling_auto_start": True,
        },
    )
