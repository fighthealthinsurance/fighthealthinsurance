"""Tests for the launch_polling_actors management command."""

import os
from io import StringIO
from unittest import mock

import pytest
import ray
from django.core.management import call_command
from django.test import TestCase


@pytest.mark.django_db
class TestLaunchPollingActorsCommand(TestCase):
    """Test the launch_polling_actors management command."""

    def setUp(self):
        """Set up Ray for testing."""
        if not ray.is_initialized():
            environ = dict(os.environ)
            environ["DJANGO_CONFIGURATION"] = "TestActor"
            ray.init(
                namespace="fhi-test",
                ignore_reinit_error=True,
                runtime_env={"env_vars": environ},
                num_cpus=1,
            )

    def tearDown(self):
        """Clean up Ray."""
        if ray.is_initialized():
            ray.shutdown()

    @mock.patch(
        "fighthealthinsurance.actor_health_status.relaunch_actors"
    )
    def test_command_with_force(self, mock_relaunch):
        """Test the command with --force flag."""
        # Mock the relaunch_actors function
        mock_relaunch.return_value = {
            "email_polling_actor": {"status": "launched"},
            "fax_polling_actor": {"status": "launched"},
            "chooser_refill_actor": {"status": "launched"},
        }

        out = StringIO()
        call_command("launch_polling_actors", "--force", stdout=out)
        output = out.getvalue()

        # Should call relaunch_actors with force=True
        mock_relaunch.assert_called_once_with(force=True)

        # Should show success messages
        assert "email_polling_actor" in output
        assert "fax_polling_actor" in output
        assert "chooser_refill_actor" in output

    @mock.patch(
        "fighthealthinsurance.management.commands.launch_polling_actors.relaunch_actors"
    )
    def test_command_with_force_handles_errors(self, mock_relaunch):
        """Test the command with --force flag when there are errors."""
        # Mock relaunch_actors to return errors
        mock_relaunch.return_value = {
            "email_polling_actor": {"status": "launched"},
            "fax_polling_actor": {"status": "error", "error": "Connection failed"},
            "chooser_refill_actor": {"status": "launched"},
        }

        out = StringIO()
        call_command("launch_polling_actors", "--force", stdout=out)
        output = out.getvalue()

        # Should show both success and error messages
        assert "email_polling_actor" in output
        assert "fax_polling_actor" in output
        assert "error" in output.lower() or "Connection failed" in output
