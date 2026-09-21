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


def clear_poisoned_client_class(actor_class: Any) -> None:
    """Undo Ray's half-finished export of an actor class, if that happened.

    Ray 2.53 ``ClientActorClass._ensure_ref`` sets ``self._ref`` to an
    ``InProgressSentinel`` BEFORE it pickles the class, so that a class which
    refers to itself can be encoded without recursing forever. If the pickle
    raises, that sentinel is what is left behind, and the retry guard is
    ``if self._ref is None``, so nothing ever tries again: every later
    ``.remote()`` reads ``self._ref.id`` and raises

        AttributeError: 'InProgressSentinel' object has no attribute 'id'

    which is exactly what this deployment saw after the first failed
    disconnect. That state lives on Ray's own cached client stub, which is
    reached through an attribute Ray sets on the decorated class, so nothing
    a caller clears on its own reference touches it.

    Clearing ``_ref`` lets the next call export the class again. Taken under
    the stub's own lock, because a sentinel is also what a legitimately
    in-flight export looks like, and that export holds this lock for its
    whole duration.

    Everything here is Ray's private surface, so every step is guarded: if a
    future Ray moves or renames any of it, this does nothing at all and the
    caller is no worse off than before.
    """
    try:
        from ray._private.client_mode_hook import RAY_CLIENT_MODE_ATTR
        from ray.util.client import ray as client_ray

        key = getattr(actor_class, RAY_CLIENT_MODE_ATTR, None)
        if key is None or not client_ray._converted_key_exists(key):
            return
        stub = client_ray._get_converted(key)
        lock = getattr(stub, "_lock", None)
        if lock is None:
            return
        with lock:
            ref = getattr(stub, "_ref", None)
            if ref is not None and type(ref).__name__ == "InProgressSentinel":
                stub._ref = None
                logger.info(
                    "Cleared a half-finished Ray class export so the next "
                    "call can send it again"
                )
    except Exception as e:
        # Ray's internals, reached on purpose and cheaply abandoned.
        logger.debug(f"Could not inspect Ray's client class cache: {e}")


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
        # Ray keeps its own cache of the exported class, and a failed export
        # poisons it in a way nothing here would otherwise reach.
        clear_poisoned_client_class(self.actor_class)

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
                clear_poisoned_client_class(self.actor_class)
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
                clear_poisoned_client_class(self.actor_class)
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
