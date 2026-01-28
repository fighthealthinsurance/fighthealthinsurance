import os
from datetime import datetime, timedelta
import ray
from django.test import TransactionTestCase
from django.utils import timezone
from django.db import connection

from fighthealthinsurance.fax_actor import FaxActor

runtime_env = dict(os.environ)


# We can unify the DB stuff and remove the test remote creation if and only if
# we switch from sqlite since otherwise the transaction test wraps each one
# and the regular testcase leaves the db locked.
class TestFaxActor(TransactionTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        if not ray.is_initialized():
            ray.init(
                namespace="fhi",
                ignore_reinit_error=True,
                # We need this to point to the same testing DB but then no async
                # local_mode=True,
                runtime_env=runtime_env,
            )
        self.fax_actor = FaxActor.remote()
        self.maxDiff = None

    def tearDown(self):
        # Clean up Ray
        if ray.is_initialized():
            ray.shutdown()

    def test_init(self):
        """Test that the actor initializes correctly."""
        result = ray.get(self.fax_actor.hi.remote())
        self.assertEqual(result, "ok")

    def disabled_test_same_db(self):
        result = ray.get(self.fax_actor.db_settings.remote())
        self.assertEqual(result, str(dict(connection.settings_dict)))

    def test_send_delayed_faxes_success(self):
        """Test successful sending of delayed faxes."""
        # Create a delayed fax that should be sent
        delayed_time = timezone.now() - timedelta(hours=4)
        fax = None
        try:
            ray.get(self.fax_actor.test_migrate.remote())
            fax = ray.get(
                self.fax_actor.test_create_fax_object.remote(
                    hashed_email="test_hash",
                    email="test@example.com",
                    destination="1234567890",
                    name="Test",
                    should_send=True,
                    sent=False,
                    paid=False,
                    date=delayed_time,
                )
            )

            # Call the method and verify results
            t, f = ray.get(self.fax_actor.send_delayed_faxes.remote())
            self.assertEqual(f, 0)
            self.assertEqual(t, 1)
        finally:
            if fax is not None:
                ray.get(self.fax_actor.test_delete.remote(fax))

    def test_send_delayed_faxes_no_delayed_faxes(self):
        """Test behavior when there are no delayed faxes to send."""
        # Create a recent fax that should not be sent yet
        ray.get(self.fax_actor.test_migrate.remote())
        recent_time = datetime.now() - timedelta(hours=0)
        fax = ray.get(
            self.fax_actor.test_create_fax_object.remote(
                hashed_email="test_hash",
                email="test@example.com",
                destination="1234567890",
                name="Test",
                should_send=True,
                sent=False,
                paid=False,
            )
        )

        # Call the method and verify results
        t, f = ray.get(self.fax_actor.send_delayed_faxes.remote())
        self.assertEqual(t, 0)
