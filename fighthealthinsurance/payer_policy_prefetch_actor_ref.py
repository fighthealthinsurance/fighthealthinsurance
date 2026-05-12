"""
Payer-Policy Pre-fetch Actor Reference.

Provides a cached reference to the payer-policy pre-fetch actor.
"""

from loguru import logger

from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.payer_policy_prefetch_actor import PayerPolicyPrefetchActor


class PayerPolicyPrefetchActorRef(BaseActorRef):
    """Reference to the payer-policy pre-fetch actor."""

    actor_class = PayerPolicyPrefetchActor
    actor_name = "payer_policy_prefetch_actor"
    has_run_method = False

    def start(self):
        """
        Get or create the payer-policy pre-fetch actor and kick off ingest.

        Returns:
            Tuple of (actor_ref, task_ref)
        """
        actor = self.get
        task = actor.prefetch_all.remote()
        logger.info(f"Started payer-policy pre-fetch task: {task}")

        return (actor, task)


payer_policy_prefetch_actor_ref = PayerPolicyPrefetchActorRef()
