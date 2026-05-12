from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.pa_requirement_refresh_actor import PARequirementRefreshActor


class PARequirementRefreshActorRef(BaseActorRef):
    """Reference to the PA requirement refresh actor."""

    actor_class = PARequirementRefreshActor
    actor_name = "pa_requirement_refresh_actor"
    has_run_method = True


pa_requirement_refresh_actor_ref = PARequirementRefreshActorRef()
