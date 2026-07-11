from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.worst_insurance_refresh_actor import (
    WorstInsuranceRefreshActor,
)


class WorstInsuranceRefreshActorRef(BaseActorRef):
    """A reference to the worst-insurance rankings refresh actor."""

    actor_class = WorstInsuranceRefreshActor
    actor_name = "worst_insurance_refresh_actor"
    has_run_method = True


worst_insurance_refresh_actor_ref = WorstInsuranceRefreshActorRef()
