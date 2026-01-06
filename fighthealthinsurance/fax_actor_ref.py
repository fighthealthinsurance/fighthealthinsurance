from fighthealthinsurance.base_actor_ref import FaxActorRefBase
from fighthealthinsurance.fax_actor import FaxActor


class FaxActorRef(FaxActorRefBase):
    """A reference to the fax actor."""

    actor_class = FaxActor
    actor_name = "FaxActor"


fax_actor_ref = FaxActorRef()
