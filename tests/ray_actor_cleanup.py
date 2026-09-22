"""Stopping a Ray actor so that its worker process is actually gone.

ray.kill() queues the kill and returns. The GCS then marks the actor dead
and the driver's calls start failing with RayActorError, and the worker
process exits some milliseconds after that: measured here on Ray 2.53,
RayActorError after 2ms, the process gone after 50ms. A cleanup that moved
on at the first signal could reach ray.shutdown() with the worker still
holding the shared SQLite file's write lock, which is the race the
sync-actor suite exists to avoid. So this waits for both: the actor
reported dead, and its worker process no longer running.

The worker is identified by PID and start time, read from the GCS actor
table, and read again after the kill: with restarts switched off, whatever
the table records then is the last worker there will be. A lookup that
fails is an error, not evidence that nothing is running. Every Ray call on
the way is held to the one deadline, so a stalled GCS fails the cleanup
rather than hanging it.
"""

import os
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Optional, Tuple, TypeVar

import ray
from ray.exceptions import GetTimeoutError, RayActorError

T = TypeVar("T")

# A running process, as (pid, start time in clock ticks from /proc/<pid>/stat).
# The start time is what tells this PID from a later process that reused it.
Process = Tuple[int, str]


def _bounded(call: Callable[[], T], deadline: float, what: str) -> T:
    """Run a blocking Ray call on a helper thread and give up at the deadline.

    Ray's synchronous lookups wait on a C++ future that nothing here can
    interrupt, so the helper thread is left to it and the cleanup fails
    instead of stalling the whole run.
    """
    outcome: "Future[T]" = Future()

    def run() -> None:
        try:
            outcome.set_result(call())
        except BaseException as e:  # noqa: BLE001 - handed to the caller below
            outcome.set_exception(e)

    threading.Thread(target=run, name=f"stop-actor:{what}", daemon=True).start()
    try:
        return outcome.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout:
        raise TimeoutError(f"{what} did not return before the deadline") from None


def _start_ticks(pid: int) -> str:
    """The process's start time from /proc, or "" if it is not there."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            # Field 22, after the parenthesised command name, which may
            # itself contain spaces.
            return stat.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def worker_process(handle: Any) -> Optional[Process]:
    """The actor's worker as the GCS records it, or None when it has none.

    None means the actor is dead without ever having had a worker (its
    constructor failed, say), so there is nothing to wait for. A lookup that
    fails, or a live actor with no PID recorded, raises: it must not pass
    for "gone". ray.util.state carries the same number but needs the
    dashboard, which a test cluster does not always have; the GCS table
    does not.
    """
    from ray._private.state import actors as actor_table  # pinned Ray 2.53

    info = actor_table(handle._actor_id.hex())
    if not isinstance(info, dict) or not info:
        raise RuntimeError(f"no actor table entry for {handle}")
    pid = int(info.get("Pid") or 0)
    if not pid:
        if info.get("State") == "DEAD":
            return None
        raise RuntimeError(f"{handle} is {info.get('State')} with no worker pid")
    return (pid, _start_ticks(pid))


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


def process_gone(process: Optional[Process]) -> bool:
    """True once this exact process is no longer running.

    Signal 0 asks the kernel whether the PID exists without touching it. A
    PID that exists but with a different start time belongs to a later
    process. One that has exited but not yet been reaped still answers, so
    /proc is read for that case: a zombie holds no locks.
    """
    if process is None:
        return True
    pid, started = process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if started and _start_ticks(pid) != started:
        return True
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
    deadline = time.monotonic() + timeout
    before = _bounded(lambda: worker_process(handle), deadline, "actor lookup")
    _bounded(lambda: ray.kill(handle, no_restart=True), deadline, "ray.kill")
    # Restarts are off now, so this is the last worker there will be. It may
    # differ from `before` if the actor restarted in between; both are waited on.
    after = _bounded(lambda: worker_process(handle), deadline, "actor lookup")
    workers = {worker for worker in (before, after) if worker is not None}
    dead = False
    while True:
        dead = dead or _reported_dead(handle)
        if dead and all(process_gone(worker) for worker in workers):
            return
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{handle}: reported dead={dead}, workers still running="
                f"{[w for w in workers if not process_gone(w)]} after {timeout}s"
            )
        time.sleep(0.05)


def stop_named_actor(name: str, namespace: str, timeout: float = 30.0) -> None:
    """stop_actor for an actor that another actor made, found by name.

    Nothing to do if it was never made: a child appears only once the
    parent has run.
    """
    try:
        handle = ray.get_actor(name, namespace=namespace)
    except ValueError:
        return
    stop_actor(handle, timeout)
