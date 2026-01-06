from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.chooser_refill_actor import ChooserRefillActor


class ChooserRefillActorRef(BaseActorRef):
    """A reference to the chooser refill actor."""

    actor_class = ChooserRefillActor
    actor_name = "chooser_refill_actor"
    has_run_method = True


chooser_refill_actor_ref = ChooserRefillActorRef()
