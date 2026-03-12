from fighthealthinsurance.actor_ref_config import CHOOSER_REFILL_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.chooser_refill_actor import ChooserRefillActor


class ChooserRefillActorRef(BaseActorRef):
    """A reference to the chooser refill actor."""

    actor_class = ChooserRefillActor
    config = CHOOSER_REFILL_ACTOR_CONFIG


chooser_refill_actor_ref = ChooserRefillActorRef()
