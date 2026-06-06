import asyncio
import os
import time

import ray
from asgiref.sync import async_to_sync
from loguru import logger

from fighthealthinsurance.utils import get_env_variable


@ray.remote(max_restarts=-1, max_task_retries=2)
class DeniedItemsAnalysisActor:
    """Ray actor to process denied-items analysis jobs asynchronously."""

    def __init__(self) -> None:
        logger.info("Starting DeniedItemsAnalysisActor")
        time.sleep(1)
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )
        from configurations.wsgi import get_wsgi_application

        get_wsgi_application()

    def run_analysis(self, *, job_key: str, chat_id: str, disconnect_event_ts: str) -> None:
        from fighthealthinsurance.websockets import OngoingChatConsumer, run_denied_items_analysis_job

        async_to_sync(run_denied_items_analysis_job)(
            analyzer=OngoingChatConsumer()._analyze_denied_items,
            job_key=job_key,
            chat_id=chat_id,
            disconnect_event_ts=disconnect_event_ts,
        )
