"""
ExtraLink Pre-fetch Actor Reference.

Provides a cached reference to the extralink pre-fetch actor.
"""

from functools import cached_property
from loguru import logger
from fighthealthinsurance.extralink_prefetch_actor import ExtraLinkPrefetchActor


class ExtraLinkPrefetchActorRef:
    """Reference to the extralink pre-fetch actor."""

    extralink_prefetch_actor = None

    @cached_property
    def get(self):
        """
        Get or create the extralink pre-fetch actor.

        Returns:
            Tuple of (actor_ref, task_ref)
        """
        name = "extralink_prefetch_actor"
        if self.extralink_prefetch_actor is None:
            self.extralink_prefetch_actor = ExtraLinkPrefetchActor.options(
                name=name,
                lifetime="detached",  # Survives creator process
                namespace="fhi",
                get_if_exists=True,  # Reuse if already exists
            ).remote()

        # Start the pre-fetch task (one-time execution)
        task = self.extralink_prefetch_actor.prefetch_all.remote()
        logger.info(f"Started extralink pre-fetch task: {task}")

        return (self.extralink_prefetch_actor, task)


extralink_prefetch_actor_ref = ExtraLinkPrefetchActorRef()
