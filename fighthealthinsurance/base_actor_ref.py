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


# What a loop actor's ``run`` returns when it is asked to start a loop it is
# already running. The launch job reads a finished run task as a failure,
# except for this.
RUN_ALREADY_STARTED = "run-already-started"


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

    def _existing_actor(self) -> Tuple[Optional[Any], bool]:
        """``(handle, loop_is_live)`` for the named actor when it is already
        alive, else ``(None, False)``.

        Only asked when a cluster is attached: ``ray.get_actor`` auto-starts
        a local cluster otherwise, and there is nothing to attach to in that
        case anyway. For a loop actor the handle only counts as existing when
        it answers a health check at all; a dead or hung actor is treated as
        absent so the creation path (``get_if_exists``) decides.
        """
        if not ray_cluster_available():
            return None, False
        try:
            handle = ray.get_actor(self.actor_name, namespace="fhi")
        except ValueError:
            return None, False
        except Exception as e:
            logger.debug(f"Could not look up existing actor {self.actor_name}: {e}")
            return None, False
        if not self.has_run_method:
            return handle, False
        try:
            live = bool(ray.get(handle.health_check.remote(), timeout=10))
        except Exception as e:
            logger.info(
                f"Actor {self.actor_name} exists but did not answer a health "
                f"check ({e}); letting Ray create or reuse it"
            )
            return None, False
        return handle, live

    @cached_property
    def get(self) -> Any:
        """
        Get or create the actor instance.

        Returns:
            For actors with run_method: Tuple of (actor, remote_result). The
            remote_result is None when the actor already existed with its run
            loop live: every evaluation of ``get`` in a fresh process (each
            deploy's launch job, each reconcile run) used to call
            ``run.remote()`` regardless, and these are async actors, so each
            call started one more concurrent loop inside the same actor that
            nothing could see or stop.
            For actors without run_method: Just the actor instance
        """
        loop_live = False
        if self._actor_instance is None:
            existing, loop_live = self._existing_actor()
            if existing is not None:
                self._actor_instance = existing
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
            if loop_live:
                logger.info(
                    f"Attached to the running {self.actor_name} actor; its run "
                    "loop is already live, not starting another"
                )
                return (self._actor_instance, None)
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
