from fighthealthinsurance.actor_ref_config import FAX_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import FaxActorRefBase
from fighthealthinsurance.fax_actor import FaxActor


class FaxActorRef(FaxActorRefBase):
    """A reference to the fax actor."""

    actor_class = FaxActor
    config = FAX_ACTOR_CONFIG


fax_actor_ref = FaxActorRef()
