from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.ucr_refresh_actor import UCRRefreshActor


class UCRRefreshActorRef(BaseActorRef):
    """A reference to the UCR refresh polling actor."""

    actor_class = UCRRefreshActor
    actor_name = "ucr_refresh_actor"
    has_run_method = True


ucr_refresh_actor_ref = UCRRefreshActorRef()
