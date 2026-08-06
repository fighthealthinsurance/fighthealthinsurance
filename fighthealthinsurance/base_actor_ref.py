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

    Dev servers pass this gate the same way production does: ``local_ray.
    maybe_init_local_ray()`` deliberately boots an in-process cluster once at
    startup (Dev config only), so ``ray.is_initialized()`` is True and the
    dispatch sites take their Ray paths locally too. That startup boot is the
    sanctioned counterpart of the per-dispatch auto-init this guard blocks;
    the fallbacks below each gate remain for Test* configs and for dev runs
    with ``FHI_LOCAL_RAY=false``.
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
