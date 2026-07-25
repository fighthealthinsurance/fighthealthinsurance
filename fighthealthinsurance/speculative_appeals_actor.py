"""Detached Ray actor that precomputes bare, internal-only candidate appeals.

Dispatched fire-and-forget with just ``denial_id`` from denial creation (see
ml_speculative_appeals_helper.dispatch_speculative_appeals). The actual work
lives in the plain, unit-testable ``SpeculativeAppealsHelper`` so it can run
either here (in this actor's process) or, when Ray is unavailable, in a daemon
thread -- this class is only the Django-booting Ray wrapper, mirroring the
other detached actors (e.g. UCRRefreshActor).
"""

import os
import time
from typing import Any

import ray
from channels.db import database_sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "SpeculativeAppealsActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class SpeculativeAppealsActor:
    def __init__(self):
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._logger = logger
        logger.info("SpeculativeAppealsActor initialized")

    async def hello(self) -> str:
        return "Hi"

    async def prefetch_for_denial(self, denial_id: Any, force: bool = False) -> int:
        """Generate + persist speculative candidate appeals for a denial.

        The helper is synchronous (make_appeals is a blocking iterator), so run
        it in a thread via database_sync_to_async, which also closes the
        thread's DB connections around the call.

        thread_sensitive=False: this is an async Ray actor, so a burst of denial
        creations dispatches overlapping prefetches. The default
        (thread_sensitive=True) funnels every one of them onto a single shared
        executor thread, and since a generation can occupy it for minutes, later
        precomputes would not be ready by the time their live flows need them --
        defeating the point of precomputing. A pool thread per call restores the
        concurrency. DatabaseSyncToAsync.thread_handler still wraps each call in
        close_old_connections() either way, so per-call connection isolation and
        cleanup are unchanged.
        """
        from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
            SpeculativeAppealsHelper,
        )

        count: int = await database_sync_to_async(
            SpeculativeAppealsHelper.generate_for_denial_sync,
            thread_sensitive=False,
        )(denial_id, force=force)
        return count
