from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.denied_items_analysis_actor import DeniedItemsAnalysisActor


class DeniedItemsAnalysisActorRef(BaseActorRef):
    actor_class = DeniedItemsAnalysisActor
    actor_name = "denied_items_analysis_actor"
    has_run_method = False


denied_items_analysis_actor_ref = DeniedItemsAnalysisActorRef()
