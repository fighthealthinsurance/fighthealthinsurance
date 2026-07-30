"""Cross-site WebSocket protection and ChatViewSet auth.

- The ASGI websocket stack wraps consumers in the browser-origin validator
  (env-gated, default on): a hostile Origin is rejected at the handshake,
  a matching Origin connects, and a MISSING Origin (non-browser clients,
  probes) also connects -- browsers always send Origin, so an originless
  handshake cannot be cross-site WebSocket hijacking.
- /api/chats requires authentication; anonymous requests used to 500 on
  ProfessionalUser.objects.get(user=AnonymousUser).
"""

import typing

from asgiref.sync import sync_to_async
from channels.auth import AuthMiddlewareStack
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import path, reverse
from rest_framework.test import APITestCase

from fighthealthinsurance.websockets import OngoingChatConsumer
from fighthealthinsurance.ws_origin import allowed_hosts_browser_origin_validator

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def _origin_guarded_app():
    return allowed_hosts_browser_origin_validator(
        AuthMiddlewareStack(
            URLRouter([path("ws/ongoing-chat/", OngoingChatConsumer.as_asgi())])
        )
    )


@override_settings(ALLOWED_HOSTS=["testserver"])
class WsOriginValidationTest(APITestCase):
    async def test_hostile_origin_is_rejected(self):
        communicator = WebsocketCommunicator(
            _origin_guarded_app(),
            "/ws/ongoing-chat/",
            headers=[
                (b"origin", b"https://evil.example.com"),
                (b"host", b"testserver"),
            ],
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_matching_origin_connects(self):
        communicator = WebsocketCommunicator(
            _origin_guarded_app(),
            "/ws/ongoing-chat/",
            headers=[
                (b"origin", b"http://testserver"),
                (b"host", b"testserver"),
            ],
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_missing_origin_connects(self):
        """Non-browser clients (curl, native apps, probes) send no Origin and
        must not be locked out: only browsers can be CSWSH vectors, and
        browsers always send Origin."""
        communicator = WebsocketCommunicator(
            _origin_guarded_app(),
            "/ws/ongoing-chat/",
            headers=[(b"host", b"testserver")],
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()


class ChatViewSetAuthTest(APITestCase):
    async def test_anonymous_list_is_rejected_not_500(self):
        url = reverse("chats-list")
        response = await sync_to_async(self.client.get)(url)
        self.assertIn(response.status_code, (401, 403))

    async def test_authenticated_professional_can_list(self):
        from fighthealthinsurance.models import ProfessionalUser

        user = await sync_to_async(User.objects.create_user)(
            username="chatperm1",
            password="testpass",
            email="chatperm1@example.com",
        )
        await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="9999940001"
        )
        await sync_to_async(self.client.login)(
            username="chatperm1", password="testpass"
        )
        url = reverse("chats-list")
        response = await sync_to_async(self.client.get)(url)
        self.assertEqual(response.status_code, 200)
