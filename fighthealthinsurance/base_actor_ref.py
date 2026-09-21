"""Base class for Ray actor references to reduce code duplication."""

import os
from functools import cached_property
from typing import Any, Optional, Tuple

import ray
from loguru import logger


def ray_cluster_available() -> bool:
    """True only when dispatching to Ray ATTACHES to an existing cluster.

    Ray auto-initializes on the first ``.remote()`` call, and that is not a
    no-op when no cluster is configured: it silently STARTS A BRAND-NEW LOCAL
    one -- a GCS, an object store, and then any detached actor booting its own
    Django app -- inside whatever process touched it. In production
    ``RAY_ADDRESS`` points at the k8s RayCluster so auto-init connects to it,
    but in a dev server or a test process the same call stands up a cluster
    per dispatch (this is what killed the Selenium suite: the create-time
    speculative dispatch booted one per denial submitted through the form).

    Per-task dispatch sites that run on a REQUEST path should gate on this and
    take their fallback instead. Polling actors don't need it: they're launched
    once by the dedicated ``launch_polling_actors`` command, which runs on the
    cluster and waits for Ray to come up.
    """
    try:
        if ray.is_initialized():
            return True
    except Exception:
        return False
    address = (os.environ.get("RAY_ADDRESS") or "").strip()
    # "local" is Ray's own spelling of "start a brand-new local cluster"
    # (ray._private.services treats it exactly like an unset address), and it is
    # the value a developer is most likely to set by hand -- so honoring it here
    # would reintroduce the boot this guard exists to prevent.
    if address.lower() == "local":
        return False
    return bool(address)


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

    def invalidate(self) -> None:
        """Forget the handle, so the next ``get`` builds a new one.

        There are two places to clear: ``_actor_instance`` holds the handle
        and ``cached_property`` keeps whatever ``get`` last returned in the
        instance ``__dict__``. Clearing one and not the other hands the dead
        handle straight back.

        Callers need this because ``get`` cannot see their first use of the
        handle. In client mode ``.remote()`` returns before the server has
        confirmed the actor exists, so a creation that failed part way looks
        fine here and only surfaces when somebody calls a method on it, which
        happens in the caller for every ref without a run method. Without
        this, that one failure is remembered for the life of the process.
        """
        self._actor_instance = None
        self.__dict__.pop("get", None)

    @cached_property
    def get(self) -> Any:
        """
        Get or create the actor instance.

        Returns:
            For actors with run_method: Tuple of (actor, remote_result)
            For actors without run_method: Just the actor instance
        """
        if self._actor_instance is None:
            try:
                self._actor_instance = self.actor_class.options(  # type: ignore
                    name=self.actor_name,
                    lifetime="detached",
                    namespace="fhi",
                    get_if_exists=True,
                ).remote()
            except Exception:
                # Nothing usable is kept. In client mode this call returns
                # before the server has confirmed the actor exists, so a
                # creation that fails part way can otherwise leave the
                # process holding a handle to an actor that was never made;
                # every later call then raises "'InProgressSentinel' object
                # has no attribute 'id'" rather than trying again, and one
                # bad disconnect disables the feature for the life of the
                # pod. Clearing here means the next caller retries.
                self._actor_instance = None
                self.__dict__.pop("get", None)
                raise

        if self.has_run_method:
            try:
                # Kick off the remote task
                remote_result = self._actor_instance.run.remote()
            except Exception:
                # Same reasoning: a handle whose first use fails is no good
                # to the next caller either.
                self._actor_instance = None
                self.__dict__.pop("get", None)
                raise
            logger.info(f"Remote run of {self.actor_name} actor {remote_result}")
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
