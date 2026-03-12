"""Base class for Ray actor references to reduce code duplication."""

from functools import cached_property
from typing import Any, Optional

import ray

from fighthealthinsurance.actor_ref_config import ActorRefConfig, ActorStartMode


class BaseActorRef:
    """
    Base class for Ray actor references with common initialization logic.

    Subclasses should set:
    - actor_class: The actor class to instantiate
    - config: Typed actor-ref config
    """

    actor_class: type
    config: ActorRefConfig
    _actor_instance: Optional[Any] = None

    @cached_property
    def get(self) -> Any:
        """
        Get or create the actor instance.

        Returns:
            For actors with run_method: Tuple of (actor, remote_result)
            For actors without run_method: Just the actor instance
        """
        if self._actor_instance is None:
            self._actor_instance = self.actor_class.options(  # type: ignore
                name=self.config.name,
                lifetime=self.config.lifetime,
                namespace=self.config.namespace,
                get_if_exists=self.config.get_if_exists,
            ).remote()

        if self.config.start_mode == ActorStartMode.RUN_ON_GET:
            # Kick off the remote task
            remote_result = self._actor_instance.run.remote()
            print(f"Remote run of {self.config.name} actor {remote_result}")
            return (self._actor_instance, remote_result)

        return self._actor_instance


class FaxActorRefBase(BaseActorRef):
    """
    Base class for FaxActor reference with special initialization logic.

    FaxActor uses ray.get_actor for retrieval instead of standard initialization.
    """

    _actor_instance: Optional[Any] = None

    @cached_property
    def get(self) -> Any:
        """Get or create the fax actor instance with special initialization."""
        namespace = self.config.namespace
        if self._actor_instance is None:
            # First try to get existing actor to avoid race conditions
            try:
                self._actor_instance = ray.get_actor(
                    self.config.name, namespace=namespace
                )
            except ValueError:
                # Actor doesn't exist, create it
                self._actor_instance = self.actor_class.options(  # type: ignore
                    name=self.config.name,
                    lifetime=self.config.lifetime,
                    namespace=namespace,
                    get_if_exists=self.config.get_if_exists,
                ).remote()
        return self._actor_instance
