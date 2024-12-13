import time
from functools import cached_property

import ray
from fighthealthinsurance.email_polling_actor import EmailPollingActor


class EmailPollingActorRef:
    """A reference to the email polling actor."""

    email_polling_actor = None

    @cached_property
    def get(self):
        name = "email_polling_actor"
        if self.email_polling_actor is None:
            self.email_polling_actor = EmailPollingActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi", get_if_exists=True
            ).remote()
        # Kick of the remote task
        rr = self.email_polling_actor.run.remote()
        print(f"Remote run of email actor {rr}")
        return (self.email_polling_actor, rr)


email_polling_actor_ref = EmailPollingActorRef()
