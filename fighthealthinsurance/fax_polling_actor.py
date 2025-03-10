import ray
from fighthealthinsurance.fax_actor import FaxActor
import asyncio
import logging

@ray.remote(max_restarts=3, max_task_retries=3)
class FaxPollingActor:
    """
    Actor that polls for delayed remote faxes and processes them.
    """

    def __init__(self, interval: int = 60):
        """
        Initialize the FaxPollingActor with a specified polling interval.
        """
        self.fax_actor = FaxActor.options(name="fpa-worker", namespace="fhi").remote()
        self.interval = interval
        self.c = 0
        self.e = 0
        self.aec = 0
        self.running = False

    async def hello(self) -> str:
        """
        Return a greeting.
        """
        return "Hi"

    async def run(self) -> bool:
        """
        Continuously poll for delayed remote faxes and process them until stopped.
        Detailed error logging is provided for debugging.
        """
        self.running = True
        while self.running:
            await asyncio.sleep(1)
            try:
                logging.info("Checked for delayed remote faxes")
                c, f = await self.fax_actor.send_delayed_faxes.remote()
                self.c += c
                self.e += f
            except Exception as exc:
                logging.exception("Error while checking outbound faxes")
                self.aec += 1
            finally:
                print("Waiting for next run")
                await asyncio.sleep(self.interval)
        return True

    async def stop(self) -> None:
        """
        Stop the polling loop.
        """
        self.running = False

    async def count(self) -> int:
        """
        Return the count of processed faxes.
        """
        return self.c

    async def error_count(self) -> int:
        """
        Return the count of fax processing errors.
        """
        return self.e

    async def actor_error_count(self) -> int:
        """
        Return the count of actor-level errors.
        """
        return self.aec
