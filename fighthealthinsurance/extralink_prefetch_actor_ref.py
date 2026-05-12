"""
ExtraLink Pre-fetch Actor Reference.

Provides a cached reference to the extralink pre-fetch actor.
"""

from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.extralink_prefetch_actor import ExtraLinkPrefetchActor


class ExtraLinkPrefetchActorRef(BaseActorRef):
    """Reference to the extralink pre-fetch actor."""

    actor_class = ExtraLinkPrefetchActor
    actor_name = "extralink_prefetch_actor"
    has_run_method = False

    def start(self):
        """
        Get or create the extralink pre-fetch actor.

        Returns:
            Tuple of (actor_ref, task_ref)
        """
        # ``BaseActorRef.get`` is a ``cached_property``; access without parens.
        actor = self.get
        task = actor.prefetch_all.remote()
        print(f"Started extralink pre-fetch task: {task}")

        return (actor, task)


extralink_prefetch_actor_ref = ExtraLinkPrefetchActorRef()
