from fighthealthinsurance.actor_ref_config import FAX_POLLING_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.fax_polling_actor import FaxPollingActor


class FaxPollingActorRef(BaseActorRef):
    """A reference to the fax polling actor."""

    actor_class = FaxPollingActor
    config = FAX_POLLING_ACTOR_CONFIG


fax_polling_actor_ref = FaxPollingActorRef()
