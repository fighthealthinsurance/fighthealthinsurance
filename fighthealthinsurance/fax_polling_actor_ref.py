from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.fax_polling_actor import FaxPollingActor


class FaxPollingActorRef(BaseActorRef):
    """A reference to the fax polling actor."""

    actor_class = FaxPollingActor
    actor_name = "fax_polling_actor"
    has_run_method = True


fax_polling_actor_ref = FaxPollingActorRef()
