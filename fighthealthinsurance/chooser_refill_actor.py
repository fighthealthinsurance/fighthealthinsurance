import os

import ray
import time
import asyncio

from asgiref.sync import sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "ChooserRefillActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class ChooserRefillActor:
    def __init__(self):
        print(f"Starting ChooserRefillActor")
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        print(f"wsgi started for ChooserRefillActor")

    async def run(self) -> None:
        print(f"Starting ChooserRefillActor run")
        self.running = True

        from fighthealthinsurance.chooser_tasks import check_and_refill_task_pool

        while self.running:
            try:
                # Check and refill the task pool
                await check_and_refill_task_pool()

                # Sleep for 5 minutes between checks
                await asyncio.sleep(300)
            except Exception as e:
                print(f"Error {e} while checking/refilling chooser task pool.")
                # On error, wait a bit longer before retrying
                await asyncio.sleep(60)

        print(f"ChooserRefillActor done running")
        return None

    def stop(self) -> None:
        """Stop the actor."""
        self.running = False
