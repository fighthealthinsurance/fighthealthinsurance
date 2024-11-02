import os

import ray
from fighthealthinsurance.ray import *

name = "EmailPollingActor"


@ray.remote
class EmailPollingActor:
    def __init__(self):
        # This is a bit of a hack but we do this so we have the app configured
        from configurations.wsgi import get_wsgi_application

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        application = get_wsgi_application()
        # Now we can import the follow up e-mails logic
        from fighthealthinsurance.followup_emails import *

        self.sender = FollowUpEmailSender

    def run(self):
        self.running = True
        while self.running:
            try:
                print(self.sender.find_candidates)
            except Exception as e:
                print(f"Error {e} while checking messages.")
