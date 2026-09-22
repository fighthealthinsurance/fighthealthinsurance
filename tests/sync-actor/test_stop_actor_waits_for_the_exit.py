"""stop_actor returns only once the actor's worker process is gone.

ray.kill queues the kill; the GCS reports the actor dead a moment before
the worker process exits (2ms against 50ms, measured on Ray 2.53), and a
cleanup that moved on at the first signal could reach ray.shutdown with the
worker still running. This starts an actor in a loop, notes its worker's
PID, stops it, and checks both signals: a call raises RayActorError, and
the PID is no longer a running process.
"""

import os
import time

import ray
from django.test import SimpleTestCase
from ray.exceptions import RayActorError

from unittest.mock import patch

from tests import ray_actor_cleanup
from tests.ray_actor_cleanup import process_gone, stop_actor, worker_process


@ray.remote(max_restarts=-1)
class Looper:
    def hello(self):
        return "Hi"

    def pid(self):
        return os.getpid()

    def run(self):
        while True:
            time.sleep(0.05)


class StopActorTest(SimpleTestCase):
    def setUp(self):
        if not ray.is_initialized():
            environ = dict(os.environ)
            environ["DJANGO_CONFIGURATION"] = "TestActor"
            ray.init(
                namespace="fhi-test",
                ignore_reinit_error=True,
                runtime_env={"env_vars": environ},
                num_cpus=1,
            )
        self.addCleanup(ray.shutdown)

    def test_the_actor_and_its_worker_are_gone_when_stop_actor_returns(self):
        looper = Looper.remote()
        self.addCleanup(stop_actor, looper)
        self.assertEqual("Hi", ray.get(looper.hello.remote(), timeout=60))
        pid = ray.get(looper.pid.remote(), timeout=10)
        recorded = worker_process(looper)
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded[0], pid, "the GCS records a different worker")
        self.assertFalse(process_gone(recorded), "no worker process to begin with")
        looper.run.remote()
        time.sleep(0.5)

        stop_actor(looper)

        with self.assertRaises(RayActorError):
            ray.get(looper.hello.remote(), timeout=10)
        self.assertTrue(process_gone(recorded), f"worker {pid} is still running")

    def test_a_failed_lookup_is_an_error_not_evidence(self):
        """If the worker cannot be identified, the cleanup fails loudly
        rather than passing on the GCS's word alone."""
        looper = Looper.remote()
        self.addCleanup(stop_actor, looper)
        self.assertEqual("Hi", ray.get(looper.hello.remote(), timeout=60))
        with patch.object(
            ray_actor_cleanup, "worker_process", side_effect=RuntimeError("no table")
        ):
            with self.assertRaises(RuntimeError):
                stop_actor(looper)

    def test_a_reused_pid_is_not_mistaken_for_the_worker(self):
        """A process with the same PID but a different start time is a
        different process, so it counts as gone."""
        pid = os.getpid()
        self.assertFalse(process_gone((pid, ray_actor_cleanup._start_ticks(pid))))
        self.assertTrue(process_gone((pid, "not-our-start-time")))
