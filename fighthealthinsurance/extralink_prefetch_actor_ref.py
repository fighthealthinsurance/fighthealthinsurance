"""
ExtraLink Pre-fetch Actor Reference.

Provides a cached reference to the extralink pre-fetch actor.
"""

from fighthealthinsurance.actor_ref_config import EXTRALINK_PREFETCH_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.extralink_prefetch_actor import ExtraLinkPrefetchActor


class ExtraLinkPrefetchActorRef(BaseActorRef):
    """Reference to the extralink pre-fetch actor."""

    actor_class = ExtraLinkPrefetchActor
    config = EXTRALINK_PREFETCH_ACTOR_CONFIG

    def start(self):
        """
        Get or create the extralink pre-fetch actor.

        Returns:
            Tuple of (actor_ref, task_ref)
        """
        actor = self.get
        task = actor.prefetch_all.remote()
        print(f"Started extralink pre-fetch task: {task}")

        return (actor, task)


extralink_prefetch_actor_ref = ExtraLinkPrefetchActorRef()
