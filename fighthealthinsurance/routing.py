from django.urls import path
from fighthealthinsurance.websockets import (
    StreamingAppealsBackend,
    StreamingEntityBackend,
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
]
