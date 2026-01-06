"""Base class for Ray actor references to reduce code duplication."""

from functools import cached_property
from typing import Any, Optional, Tuple

import ray


class BaseActorRef:
    """
    Base class for Ray actor references with common initialization logic.

    Subclasses should set:
    - actor_class: The actor class to instantiate
    - actor_name: The name for the actor
    - has_run_method: Whether the actor has a run() method that should be called
    """

    actor_class: type
    actor_name: str
    has_run_method: bool = False
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
                name=self.actor_name,
                lifetime="detached",
                namespace="fhi",
                get_if_exists=True,
            ).remote()

        if self.has_run_method:
            # Kick off the remote task
            remote_result = self._actor_instance.run.remote()
            print(f"Remote run of {self.actor_name} actor {remote_result}")
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
        namespace = "fhi"
        if self._actor_instance is None:
            # First try to get existing actor to avoid race conditions
            try:
                self._actor_instance = ray.get_actor(
                    self.actor_name, namespace=namespace
                )
            except ValueError:
                # Actor doesn't exist, create it
                self._actor_instance = self.actor_class.options(  # type: ignore
                    name=self.actor_name,
                    lifetime="detached",
                    namespace=namespace,
                    get_if_exists=True,
                ).remote()
        return self._actor_instance
