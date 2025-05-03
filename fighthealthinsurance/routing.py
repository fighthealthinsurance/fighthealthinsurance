from django.urls import path
from fighthealthinsurance.websockets import (
    StreamingAppealsBackend,
    StreamingEntityBackend,
    PriorAuthConsumer,
    OngoingChatConsumer,
)

websocket_urlpatterns = [
    path(
        "ws/streaming-entity-backend/",
        StreamingEntityBackend.as_asgi(),
        name="streamingentity_json_backend",
    ),
    path(
        "ws/streaming-appeals-backend/",
        StreamingAppealsBackend.as_asgi(),
        name="streamingentity_json_backend",
    ),
    path(
        "ws/prior-auth/",
        PriorAuthConsumer.as_asgi(),
        name="prior_auth_consumer",
    ),
    path(
        "ws/ongoing-chat/",
        OngoingChatConsumer.as_asgi(),
        name="ongoing_chat_consumer",
    ),
]
