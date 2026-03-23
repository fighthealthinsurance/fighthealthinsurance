import os
import time

import pytest
import ray
from django.test import TestCase

from fighthealthinsurance.fax_polling_actor import FaxPollingActor


@pytest.mark.django_db
class TestFaxPollingActor(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Initialize Ray for testing
        if not ray.is_initialized():
            # Make sure we use the test DB
            environ = dict(os.environ)
            environ["DJANGO_CONFIGURATION"] = "Test"
            ray.init(
                namespace="fhi-test",
                ignore_reinit_error=True,
                # We can't use local mode because of async
                runtime_env={"env_vars": environ},
                num_cpus=1,
            )

    def tearDown(self):
        # Clean up Ray
        if ray.is_initialized():
            ray.shutdown()

    def test_run_method_handles_errors(self):
        """Test that the fax polling actor starts and runs."""

        fax_polling_actor = FaxPollingActor.remote()

        # Say "hi" -- mostly make sure the actor started OK
        r = fax_polling_actor.hello.remote()
        self.assertEqual("Hi", ray.get(r))
        # Start the actor running
        r = fax_polling_actor.run.remote()

        # Give run() time to start executing before polling
        time.sleep(0.5)

        # Poll until run() has started and loop body executed
        max_wait = 30  # seconds
        start = time.time()
        running = False
        loop_executed = False
        while time.time() - start < max_wait:
            running = ray.get(fax_polling_actor.health_check.remote())
            if running:
                # Also verify the loop body ran at least once
                aec = ray.get(fax_polling_actor.actor_error_count.remote())
                c = ray.get(fax_polling_actor.count.remote())
                if aec > 0 or c > 0:
                    loop_executed = True
                    break
            time.sleep(0.5)

        # For local testing since they're all getting different DBs we're
        # really just checking that it's able to start and run.
        self.assertTrue(running, "run() should have started")
        self.assertTrue(loop_executed, "run() loop body should have executed")
