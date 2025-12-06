from functools import cached_property
from fighthealthinsurance.fax_actor import FaxActor


class FaxActorRef:
    fax_actor = None

    @cached_property
    def get(self):
        # Shut down existing actor if needed.
        name = "FaxActor"
        if self.fax_actor is None:
            self.fax_actor = FaxActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi", get_if_exists=True
            ).remote()
        return self.fax_actor


fax_actor_ref = FaxActorRef()
