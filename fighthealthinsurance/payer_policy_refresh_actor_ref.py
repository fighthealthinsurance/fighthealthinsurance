from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.payer_policy_refresh_actor import PayerPolicyRefreshActor


class PayerPolicyRefreshActorRef(BaseActorRef):
    """Reference to the payer policy refresh actor."""

    actor_class = PayerPolicyRefreshActor
    actor_name = "payer_policy_refresh_actor"
    has_run_method = True


payer_policy_refresh_actor_ref = PayerPolicyRefreshActorRef()
