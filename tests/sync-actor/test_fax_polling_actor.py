import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import ray
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.fax_polling_actor import FaxPollingActor
from fighthealthinsurance.models import Denial, FaxesToSend


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
        """Test that the fax polling actor starts."""

        fax_polling_actor = FaxPollingActor.remote()

        # Say "hi" -- mostly make sure the actor started OK
        r = fax_polling_actor.hello.remote()
        self.assertEqual("Hi", ray.get(r))
        # Start the actor running
        r = fax_polling_actor.run.remote()

        # Give it some time to process
        import time

        time.sleep(4)

        # Note: for local testing since they're all getting different DBs we're
        # really just checking that it's able to start.

        self.assertEqual(0, ray.get(fax_polling_actor.error_count.remote()))
