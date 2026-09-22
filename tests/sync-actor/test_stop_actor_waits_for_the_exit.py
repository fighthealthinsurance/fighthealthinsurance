"""stop_actor returns only once the actor is dead.

ray.kill queues the kill; a cleanup that moves straight on to ray.shutdown
can do so with the actor still running. This starts an actor in a loop,
stops it, and asks it something: a dead actor answers with RayActorError.
"""

import os
import time

import ray
from django.test import SimpleTestCase
from ray.exceptions import RayActorError

from tests.ray_actor_cleanup import stop_actor


@ray.remote(max_restarts=-1)
class Looper:
    def hello(self):
        return "Hi"

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

    def test_the_actor_is_dead_when_stop_actor_returns(self):
        looper = Looper.remote()
        self.assertEqual("Hi", ray.get(looper.hello.remote(), timeout=60))
        looper.run.remote()
        time.sleep(0.5)

        stop_actor(looper)

        with self.assertRaises(RayActorError):
            ray.get(looper.hello.remote(), timeout=10)
