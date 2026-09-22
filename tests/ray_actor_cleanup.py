"""Stopping a Ray actor so that it is actually gone.

ray.kill() queues the kill and returns; it does not wait for the worker to
exit. A test that registers ray.kill as one cleanup and ray.shutdown as the
next can reach the shutdown while the actor is still running its loop
against the shared SQLite file, which is the race the sync-actor suite
exists to avoid. This waits, bounded, until the actor answers with
RayActorError, which is what a dead actor says.
"""

import time

import ray
from ray.exceptions import GetTimeoutError, RayActorError


def stop_actor(handle, timeout: float = 30.0) -> None:
    """Kill the actor and return only once it is dead."""
    ray.kill(handle, no_restart=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            # Every actor has __ray_ready__. While the actor is alive and busy
            # in a loop this waits behind that loop and times out; once the
            # actor is dead it raises straight away.
            ray.get(handle.__ray_ready__.remote(), timeout=1.0)
        except RayActorError:
            return
        except GetTimeoutError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"{handle} did not exit within {timeout}s of ray.kill")
        time.sleep(0.1)
