"""Ray actor that runs denied-items analysis off the WebSocket disconnect path.

``OngoingChatConsumer.disconnect`` hands the chat id to this actor, which runs
the (idempotent) analysis so the disconnect handler stays non-blocking.
"""

import os
import time

import ray
from asgiref.sync import async_to_sync

# loguru is imported inside the methods below, not at module scope. Ray
# pickles an actor class by value to send it to the cluster, so a module-level
# ``logger`` travels with it, carrying whatever sinks the process has attached.
# A stdlib logging.Handler owns a threading RLock, which does not pickle, and
# the actor then cannot be created at all. See
# tests/sync/test_actor_classes_survive_pickling.py.


@ray.remote(max_restarts=-1, max_task_retries=2)
class DeniedItemsAnalysisActor:
    """Ray actor to process denied-items analysis jobs asynchronously."""

    def __init__(self) -> None:
        from loguru import logger

        logger.info("Starting DeniedItemsAnalysisActor")
        time.sleep(1)
        # Initialize Django inside the actor (same pattern as the other
        # actors, e.g. ExtraLinkPrefetchActor).
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        from configurations.wsgi import get_wsgi_application

        get_wsgi_application()

    def run_analysis(self, *, chat_id: str) -> None:
        from fighthealthinsurance.websockets import OngoingChatConsumer

        async_to_sync(OngoingChatConsumer.run_denied_items_analysis)(chat_id=chat_id)
