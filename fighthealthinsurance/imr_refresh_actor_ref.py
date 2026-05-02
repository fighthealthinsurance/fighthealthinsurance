from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.imr_refresh_actor import IMRRefreshActor


class IMRRefreshActorRef(BaseActorRef):
    """A reference to the IMR refresh actor."""

    actor_class = IMRRefreshActor
    actor_name = "imr_refresh_actor"
    has_run_method = True


imr_refresh_actor_ref = IMRRefreshActorRef()
