"""Ray actor that runs denied-items analysis off the WebSocket disconnect path.

``OngoingChatConsumer.disconnect`` enqueues one job per (chat, disconnect
event); this actor performs the actual analysis so the disconnect handler
stays non-blocking.
"""

import os
import time

import ray
from asgiref.sync import async_to_sync
from loguru import logger


@ray.remote(max_restarts=-1, max_task_retries=2)
class DeniedItemsAnalysisActor:
    """Ray actor to process denied-items analysis jobs asynchronously."""

    def __init__(self) -> None:
        logger.info("Starting DeniedItemsAnalysisActor")
        time.sleep(1)
        # Initialize Django inside the actor (same pattern as the other
        # actors, e.g. ExtraLinkPrefetchActor).
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        from configurations.wsgi import get_wsgi_application

        get_wsgi_application()

    def run_analysis(
        self, *, job_key: str, chat_id: str, disconnect_event_ts: str
    ) -> None:
        from fighthealthinsurance.websockets import OngoingChatConsumer

        async_to_sync(OngoingChatConsumer._run_denied_items_analysis_job)(
            job_key=job_key,
            chat_id=chat_id,
            disconnect_event_ts=disconnect_event_ts,
        )
