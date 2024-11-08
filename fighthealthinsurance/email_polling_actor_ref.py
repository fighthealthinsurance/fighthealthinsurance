import time
from functools import cached_property

import ray
from fighthealthinsurance.email_polling_actor import EmailPollingActor


class EmailPollingActorRef:
    """A reference to the email polling actor."""

    @cached_property
    def get(self):
        name = "email_polling_actor"
        # Shut down existing actor
        try:
            a = ray.get_actor(name, namespace="fhi")
            if a is not None:
                ray.kill(a)
                # This sleep is kind of a "code smell" but Ray's actor tracking has some
                # race conditions inside it we are unlikely to be the people to fix.
                time.sleep(10)
        except Exception as e:
            print(f"No existing email actor to stop {e}")

        email_polling_actor = EmailPollingActor.options(  # type: ignore
            name=name, lifetime="detached", namespace="fhi"
        ).remote()
        # Kick of the remote task
        rr = email_polling_actor.run.remote()
        print(f"Remote run of email actor {rr}")
        return (email_polling_actor, rr)


email_polling_actor_ref = EmailPollingActorRef()
