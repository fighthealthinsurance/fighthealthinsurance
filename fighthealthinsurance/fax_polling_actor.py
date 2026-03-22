import asyncio
import time

import ray
from loguru import logger

from fighthealthinsurance.fax_actor import FaxActor


@ray.remote(max_restarts=-1, max_task_retries=-1)
class FaxPollingActor:
    def __init__(self, i=60):
        # This is seperate from the global one
        name = "fpa-worker"
        logger.info("Starting fax polling actor")
        time.sleep(1)
        self.fax_actor = FaxActor.options(  # type: ignore
            name=name, namespace="fhi", get_if_exists=True
        ).remote()
        logger.info(f"Created fpa-worker {self.fax_actor}")
        self.interval = i
        self.c = 0
        self.e = 0
        self.aec = 0

    async def hello(self) -> str:
        return "Hi"

    async def health_check(self) -> bool:
        """Check if the actor is healthy and running."""
        return getattr(self, "running", False)

    async def run(self) -> bool:
        logger.info("Starting run")
        self.running = True
        while self.running:
            # Like yield
            await asyncio.sleep(1)
            try:
                logger.debug("Checked for delayed remote faxes")
                c, f = await self.fax_actor.send_delayed_faxes.remote()
                self.e += f
                self.c += c
            except Exception as e:
                logger.opt(exception=True).error(f"Error while checking outbound faxes")
                self.aec += 1
            finally:
                # Success or failure we wait.
                logger.debug("Waiting for next run")
                await asyncio.sleep(60 * 60)
        logger.warning("Done running? what?")
        return True

    async def count(self) -> int:
        return self.c

    async def error_count(self) -> int:
        return self.e

    async def actor_error_count(self) -> int:
        return self.aec
