from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.speculative_appeals_actor import SpeculativeAppealsActor


class SpeculativeAppealsActorRef(BaseActorRef):
    """A reference to the speculative candidate-appeals precompute actor.

    ``has_run_method=False``: this actor has no polling loop -- it only handles
    per-denial ``prefetch_for_denial`` dispatches -- so ``.get`` returns the
    actor instance directly (not a (actor, run_task) tuple).
    """

    actor_class = SpeculativeAppealsActor
    actor_name = "speculative_appeals_actor"
    has_run_method = False


speculative_appeals_actor_ref = SpeculativeAppealsActorRef()
