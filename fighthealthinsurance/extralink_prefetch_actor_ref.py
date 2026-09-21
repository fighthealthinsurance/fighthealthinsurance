"""
ExtraLink Pre-fetch Actor Reference.

Provides a cached reference to the extralink pre-fetch actor.
"""

from loguru import logger

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
        try:
            task = actor.prefetch_all.remote()
        except Exception:
            # The dispatch is the first use of the handle, so it is where a
            # creation that only half succeeded surfaces. Forget it rather
            # than keeping a dead handle for the life of the process.
            self.invalidate()
            raise
        logger.info(f"Started extralink pre-fetch task: {task}")

        return (actor, task)


extralink_prefetch_actor_ref = ExtraLinkPrefetchActorRef()
