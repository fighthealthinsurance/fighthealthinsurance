import asyncio
import os
import time
from typing import Optional

import ray

from fighthealthinsurance.base_actor_ref import RUN_ALREADY_STARTED
from fighthealthinsurance.utils import get_env_variable

# Consecutive failed ticks after which the actor stops reporting healthy.
UNHEALTHY_AFTER_FAILURES = 3

name = "ChooserRefillActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class ChooserRefillActor:
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
        self.running = False
        self._consecutive_failures = 0
        self._logger.info("ChooserRefillActor initialized")

    async def health_check(self) -> bool:
        """Running, and not failing every tick.

        The flag alone was true from the first ``run`` onward, so a loop
        whose every tick raised (rotated database credentials, say) reported
        healthy forever and the reconciler never touched it.
        """
        failures = getattr(self, "_consecutive_failures", 0)
        return bool(getattr(self, "running", False)) and (
            failures < UNHEALTHY_AFTER_FAILURES
        )

    async def run(self) -> Optional[str]:
        if getattr(self, "running", False):
            # A second run() lands here when a fresh process attaches to this
            # actor (see BaseActorRef.get). Async actors run calls
            # concurrently, so without this it became a second loop.
            self._logger.warning(
                "ChooserRefillActor.run called while its loop is running; "
                "not starting a second loop"
            )
            return RUN_ALREADY_STARTED
        self._logger.info("Starting ChooserRefillActor run")
        self.running = True

        from fighthealthinsurance.chooser_tasks import check_and_refill_task_pool

        while self.running:
            try:
                # Check and refill the task pool
                await check_and_refill_task_pool()
                self._consecutive_failures = 0

                # Sleep for 5 minutes between checks
                await asyncio.sleep(300)
            except Exception:
                self._consecutive_failures = (
                    getattr(self, "_consecutive_failures", 0) + 1
                )
                self._logger.opt(exception=True).error(
                    "Error while checking/refilling chooser task pool "
                    f"(consecutive failures: {self._consecutive_failures})"
                )
                # On error, wait a bit longer before retrying
                await asyncio.sleep(60)

        self._logger.warning("ChooserRefillActor stopped running")
        return None

    def stop(self) -> None:
        """Stop the actor."""
        self.running = False
