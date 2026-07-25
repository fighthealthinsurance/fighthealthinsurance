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

        The helper is natively async, so it is awaited directly: it bridges only
        the blocking generation itself to a thread (non-thread-sensitive, so a
        burst of denial creations doesn't serialize onto one executor thread --
        see generate_for_denial) and keeps every query on the async ORM.
        """
        from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
            SpeculativeAppealsHelper,
        )

        return await SpeculativeAppealsHelper.generate_for_denial(
            denial_id, force=force
        )
