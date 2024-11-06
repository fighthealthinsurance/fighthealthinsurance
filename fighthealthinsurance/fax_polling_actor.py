import os

import ray
from fighthealthinsurance.fax_actor import FaxActor
import time
import asyncio


@ray.remote
class FaxPollingActor:
    def __init__(self, i=60):
        # This is seperate from the global one
        name = "fpa-worker"
        self.fax_actor = FaxActor.options(  # type: ignore
            name=name, namespace="fhi"
        ).remote()
        self.interval = i
        self.c = 0
        self.e = 0
        self.aec = 0

    async def hello(self) -> str:
        return "Hi"

    async def run(self) -> bool:
        self.running = True
        while self.running:
            # Like yield
            await asyncio.sleep(1)
            try:
                (c, f) = await self.fax_actor.send_delayed_faxes.remote()
                self.e += f
                self.c += c
            except Exception as e:
                print(f"Error {e} while checking outbound faxes")
                self.aec += 1
            finally:
                # Success or failure we wait.
                await asyncio.sleep(1)
        return True

    async def count(self) -> int:
        return self.c

    async def error_count(self) -> int:
        return self.e

    async def actor_error_count(self) -> int:
        return self.aec
