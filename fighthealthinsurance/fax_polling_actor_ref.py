from functools import cached_property
from fighthealthinsurance.fax_polling_actor import FaxPollingActor


class FaxPollingActorRef:
    """A reference to the email polling actor."""

    fax_polling_actor = None

    @cached_property
    def get(self):
        name = "fax_polling_actor"
        if self.fax_polling_actor is None:
            self.fax_polling_actor = FaxPollingActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi", get_if_exists=True
            ).remote()
        # Kick of the remote task
        rr = self.fax_polling_actor.run.remote()
        print(f"Remote run of email actor {rr}")
        return (self.fax_polling_actor, rr)


fax_polling_actor_ref = FaxPollingActorRef()
