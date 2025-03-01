import os

import ray
import time
import asyncio

from asgiref.sync import sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "EmailPollingActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class EmailPollingActor:
    def __init__(self):
        print(f"Starting actor")
        time.sleep(1)
        
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"))
        
        from configurations.wsgi import get_wsgi_application
        _application = get_wsgi_application()
        print(f"wsgi started")
        # Now we can import the follow up e-mails logic
        from fighthealthinsurance.followup_emails import (
            ThankyouEmailSender,
            FollowUpEmailSender,
        )

        self.followup_sender = FollowUpEmailSender()
        self.thankyou_sender = ThankyouEmailSender()
        print(f"Senders started")

    async def run(self) -> None:
        print(f"Starting run")
        self.running = True
        while self.running:
            try:
                followup_candidates = await sync_to_async(
                    self.followup_sender.find_candidates
                )()
                print(f"Top follow up candidates: {followup_candidates[0:4]}")
                thankyou_candidates = await sync_to_async(
                    self.thankyou_sender.find_candidates
                )()
                print(f"Top follow up candidates: {thankyou_candidates[0:4]}")
                await asyncio.sleep(10)
            except Exception as e:
                print(f"Error {e} while checking messages.")

        print(f"Done running? what?")
        return None
