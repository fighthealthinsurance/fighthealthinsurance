from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.pa_refresh_actor import PaRefreshActor


class PaRefreshActorRef(BaseActorRef):
    """A reference to the PA-requirement refresh actor."""

    actor_class = PaRefreshActor
    actor_name = "pa_refresh_actor"
    has_run_method = True


pa_refresh_actor_ref = PaRefreshActorRef()
