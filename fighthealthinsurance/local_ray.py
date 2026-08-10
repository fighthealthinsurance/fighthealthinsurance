"""Deliberate in-process Ray boot so dev runs the same code paths as prod.

In production ``RAY_ADDRESS`` points at the k8s RayCluster, so every per-task
dispatch site (UCR refresh, speculative appeals, denied-items analysis, fax
send, staff mailing) takes the Ray-actor path. Without a cluster those sites
either fall back to inline/thread work or skip entirely (see
``base_actor_ref.ray_cluster_available``), which means local development
exercises different code than production.

``maybe_init_local_ray()`` closes that gap: called once at server startup
(``asgi.py`` / ``wsgi.py``), it boots a small in-process local Ray cluster --
or eagerly attaches when ``RAY_ADDRESS`` names a real cluster -- so
``ray_cluster_available()`` passes and the production dispatch paths run
locally. This is the *deliberate, once-per-process* version of the accidental
per-dispatch auto-init that gate exists to prevent.

Gating:
  * ``settings.RAY_LOCAL_DEV_CLUSTER`` -- True in Dev, False in Test*/Prod.
    The Test* configs must stay off: TestCase-transaction teardown races
    background actor DB writes, and the Selenium suite historically died to
    exactly this kind of cluster boot.
  * ``FHI_LOCAL_RAY`` env var -- explicit "true"/"false" overrides the
    setting either way (kill switch for slow reload cycles). It only takes
    effect where the entrypoints call this function; a ``manage.py shell``
    never imports asgi/wsgi, so there you call ``maybe_init_local_ray()``
    yourself.

The dedicated polling-actor FLEET launch (email/fax/chooser/IMR/UCR/PA --
production runs it via ``launch_polling_actors``, never from web processes)
is opt-in here: set ``FHI_LOCAL_RAY_POLLING=true``, or hit the staff
"relaunch actors" endpoint -- both go through
``actor_health_status.relaunch_actors``. Note that individual actors can
still start their own loops without the fleet launch, exactly as in
production: ``BaseActorRef`` fires ``run.remote()`` on first use for actors
that have one, so e.g. the first UCR-eligible denial starts the UCR
actor's continuous source-refresh loop (which in Dev downloads real CMS
data).

Dev-lifecycle notes: the cluster lives inside the server process, so each
autoreload restart reboots it and detached actors restart lazily on next use.
For a cluster that survives reloads, run ``ray start --head`` once and export
``RAY_ADDRESS`` -- this module then just attaches, same as production.
"""

import os
import sys

import ray
from loguru import logger

LOCAL_RAY_ENV_VAR = "FHI_LOCAL_RAY"
LOCAL_RAY_POLLING_ENV_VAR = "FHI_LOCAL_RAY_POLLING"

_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")

# The dispatch sites ship ids and small strings, not datasets, so Ray's
# default object store (a percentage of system RAM) would be pure waste on a
# dev machine. Ray's floor is ~75MB; 200MB leaves comfortable headroom.
LOCAL_RAY_OBJECT_STORE_BYTES = 200 * 1024 * 1024


def local_ray_enabled() -> bool:
    """Should this process deliberately bring up Ray at startup?

    ``FHI_LOCAL_RAY`` wins when it says anything recognizable; otherwise the
    ``RAY_LOCAL_DEV_CLUSTER`` setting decides (Dev on, Test*/Prod off).
    """
    env = (os.environ.get(LOCAL_RAY_ENV_VAR) or "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    if env:
        # A mistyped kill switch (FHI_LOCAL_RAY=n, =flase, ...) must not be
        # silently ignored -- the dev who set it believes the boot is off.
        logger.warning(
            f"Unrecognized {LOCAL_RAY_ENV_VAR}={env!r}; expected one of "
            f"{_TRUTHY} or {_FALSY}. Falling back to "
            "settings.RAY_LOCAL_DEV_CLUSTER."
        )

    from django.conf import settings

    return bool(getattr(settings, "RAY_LOCAL_DEV_CLUSTER", False))


def maybe_init_local_ray() -> bool:
    """Boot (or attach) Ray once at startup when enabled; never raise.

    Returns True when Ray is initialized in this process afterwards --
    whether this call did it or it already was. A failed boot logs and
    returns False so the server still starts; the dispatch sites then fall
    back exactly as they do today without a cluster.
    """
    try:
        if ray.is_initialized():
            return True
    except Exception:
        logger.opt(exception=True).debug("ray.is_initialized() check failed")

    if not local_ray_enabled():
        return False

    if _in_reloader_watcher_process():
        logger.debug(
            "Reloader watcher process detected; leaving the Ray boot to the "
            "serving child"
        )
        return False

    address = (os.environ.get("RAY_ADDRESS") or "").strip()
    # A real RAY_ADDRESS means "attach to that cluster" (ray.init reads the
    # env var itself); sizing kwargs are only legal when starting a new local
    # cluster. The boot branch passes address="local" EXPLICITLY: with the
    # address left unset, ray.init auto-attaches to whatever address a past
    # `ray start` recorded in /tmp/ray/ray_current_cluster (no liveness
    # check), and on that path the sizing kwargs raise -- so a stale file
    # from a crashed `ray start` would silently kill the boot. "local"
    # always means a fresh cluster (ray services.canonicalize_bootstrap_
    # address). Attaching to a `ray start` cluster is spelled RAY_ADDRESS,
    # as the module docstring says.
    attaching = bool(address) and address.lower() != "local"
    try:
        if attaching:
            ray.init(namespace="fhi", ignore_reinit_error=True)
            logger.info(f"Attached to Ray cluster at RAY_ADDRESS={address} at startup")
        else:
            ray.init(
                address="local",
                namespace="fhi",
                ignore_reinit_error=True,
                include_dashboard=False,
                object_store_memory=LOCAL_RAY_OBJECT_STORE_BYTES,
            )
            logger.info(
                "Booted in-process local Ray cluster for dev/prod code-path "
                f"parity (disable with {LOCAL_RAY_ENV_VAR}=false)"
            )
    except Exception:
        logger.opt(exception=True).warning(
            "Local Ray boot failed; continuing without a cluster (dispatch "
            "sites will use their non-Ray fallbacks)"
        )
        return False

    _maybe_launch_polling_actors()
    return True


def _in_reloader_watcher_process() -> bool:
    """True in runserver_plus's werkzeug watcher parent.

    django-extensions' runserver_plus builds the WSGI handler (importing
    wsgi.py) BEFORE werkzeug forks the serving child, and the watcher parent
    then only watches files -- booting Ray there would leave a second,
    permanently idle cluster running for the whole session. The serving
    child re-executes with WERKZEUG_RUN_MAIN=true, and --noreload serves
    directly from the first process, so both of those must boot. Plain
    `manage.py runserver`'s autoreload parent never imports the handler, so
    it needs no guard.
    """
    if "runserver_plus" not in sys.argv:
        return False
    if "--noreload" in sys.argv:
        return False
    return os.environ.get("WERKZEUG_RUN_MAIN") != "true"


def _maybe_launch_polling_actors() -> None:
    """Opt-in launch of the polling-actor fleet into the local cluster.

    Off by default: in production these are launched by the dedicated
    ``launch_polling_actors`` command, not by web processes, and locally they
    poll external services (IMAP, fax backends) that usually aren't
    configured -- so starting them unasked would just spam error logs.
    """
    env = (os.environ.get(LOCAL_RAY_POLLING_ENV_VAR) or "").strip().lower()
    if env not in _TRUTHY:
        return
    try:
        from fighthealthinsurance.actor_health_status import relaunch_actors

        results = relaunch_actors(force=False)
        statuses = {name: info.get("status") for name, info in results.items()}
        logger.info(f"Local Ray polling actors launched: {statuses}")
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to launch polling actors on the local Ray cluster"
        )
