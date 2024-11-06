import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import ray
from django.test import TestCase
from django.utils import timezone
from django.db import connection

from fighthealthinsurance.fax_actor import FaxActor
from fighthealthinsurance.models import Denial, FaxesToSend


class TestFaxActor(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        if not ray.is_initialized():
            ray.init(
                namespace="fhi",
                ignore_reinit_error=True,
                # We need this to point to the same testing DB
                local_mode=True,
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

    def test_same_db(self):
        result = ray.get(self.fax_actor.db_settings.remote())
        self.assertEqual(result, str(dict(connection.settings_dict)))

    def test_send_delayed_faxes_success(self):
        """Test successful sending of delayed faxes."""
        # Create a delayed fax that should be sent
        delayed_time = timezone.now() - timedelta(hours=4)
        fax = FaxesToSend.objects.create(
            hashed_email="test_hash",
            email="test@example.com",
            destination="1234567890",
            should_send=True,
            sent=False,
            paid=False,
        )
        try:
            fax.date = delayed_time
            fax.save()
            os.sync()

            # Call the method and verify results
            (t, f) = ray.get(self.fax_actor.send_delayed_faxes.remote())
            self.assertEqual(f, 0)
            self.assertEqual(t, 1)
        finally:
            fax.delete()

    def test_send_delayed_faxes_no_delayed_faxes(self):
        """Test behavior when there are no delayed faxes to send."""
        # Create a recent fax that should not be sent yet
        recent_time = datetime.now() - timedelta(hours=0)
        fax = FaxesToSend.objects.create(
            hashed_email="test_hash",
            email="test@example.com",
            destination="1234567890",
            should_send=True,
            sent=False,
            paid=False,
        )
        fax.date = recent_time

        # Call the method and verify results
        (t, f) = ray.get(self.fax_actor.send_delayed_faxes.remote())
        self.assertEqual(t, 0)
