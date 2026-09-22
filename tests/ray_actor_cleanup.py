"""Stopping a Ray actor so that its worker process is actually gone.

ray.kill() queues the kill and returns. The GCS then marks the actor dead
and the driver's calls start failing with RayActorError, and the worker
process exits some milliseconds after that: measured here on Ray 2.53,
RayActorError after 2ms, the process gone after 50ms. A cleanup that moved
on at the first signal could reach ray.shutdown() with the worker still
holding the shared SQLite file's write lock, which is the race the
sync-actor suite exists to avoid. So this waits for both: the actor
reported dead, and its worker PID no longer running.
"""

import os
import time
from typing import Any, Optional

import ray
from ray.exceptions import GetTimeoutError, RayActorError


def worker_pid(handle: Any) -> Optional[int]:
    """The actor's worker PID as the GCS records it, or None if it has none.

    ray.util.state.get_actor carries the same number but needs the dashboard,
    which a test cluster does not always have; the GCS table does not.
    """
    try:
        from ray._private.state import actors as actor_table

        info = actor_table(handle._actor_id.hex())
    except Exception:
        return None
    pid = info.get("Pid") if isinstance(info, dict) else None
    return int(pid) if pid else None


def _reported_dead(handle: Any) -> bool:
    try:
        # Every actor has __ray_ready__. While the actor is alive and busy in
        # a loop this waits behind that loop and times out; once the GCS has
        # it dead it raises straight away.
        ray.get(handle.__ray_ready__.remote(), timeout=1.0)
    except RayActorError:
        return True
    except GetTimeoutError:
        return False
    return False


def process_gone(pid: Optional[int]) -> bool:
    """True once no running process has this PID.

    Signal 0 asks the kernel whether the process exists without touching it.
    One that has exited but not yet been reaped still answers, so /proc is
    read for that case: a zombie holds no locks.
    """
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/status") as status:
            for line in status:
                if line.startswith("State:"):
                    return line.split()[1] == "Z"
    except OSError:
        pass
    return False


def stop_actor(handle: Any, timeout: float = 30.0) -> None:
    """Kill the actor and return only once it is dead and its worker has exited."""
    pid = worker_pid(handle)
    ray.kill(handle, no_restart=True)
    deadline = time.monotonic() + timeout
    dead = False
    while True:
        dead = dead or _reported_dead(handle)
        gone = process_gone(pid)
        if dead and gone:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{handle}: reported dead={dead}, worker pid {pid} gone={gone} "
                f"after {timeout}s"
            )
        time.sleep(0.05)


def stop_named_actor(name: str, namespace: str, timeout: float = 30.0) -> None:
    """stop_actor for an actor that another actor made, found by name.

    Nothing to do if it was never made: a child appears only once the
    parent's loop has run.
    """
    try:
        handle = ray.get_actor(name, namespace=namespace)
    except ValueError:
        return
    stop_actor(handle, timeout)
