"""
ASGI config for fighthealthinsurance project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/asgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
os.environ.setdefault("DJANGO_CONFIGURATION", "Dev")

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
        environment=os.getenv("DJANGO_CONFIGURATION", "production-ish"),
        release=os.getenv("RELEASE", "unset"),
        _experiments={
            # Set continuous_profiling_auto_start to True
            # to automatically start the profiler on when
            # possible.
            "continuous_profiling_auto_start": True,
        },
    )
