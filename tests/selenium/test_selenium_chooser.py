"""Selenium tests for the Chooser (Best-Of Selection) interface

Note: Due to SQLite database locking issues in Django's StaticLiveServerTestCase
when the API creates sessions, these tests are limited to page load tests that
don't trigger database writes. The full API functionality is tested in
test_chooser_api.py using Django's test client which doesn't have this issue.

For comprehensive Chooser testing:
- Page load/rendering: This file (Selenium tests)
- API endpoints (vote, skip, etc.): test_chooser_api.py (API tests)
"""

import time
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
)
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestChooserPageLoad(FHISeleniumBase, StaticLiveServerTestCase):
    """Test the Chooser interface page loads with Selenium.

    Note: These tests are limited to page loads that don't trigger API calls
    requiring database writes (session creation), due to SQLite locking issues.
    """

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/chooser_test_data.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        # Start patching prefill BEFORE the live server starts
        # This prevents async DB conflicts in SQLite during tests
        """
        Prepare the class-level test environment by patching chooser async tasks and initializing parent live-server setup.

        Patches the async task entrypoints used by the chooser ("fighthealthinsurance.chooser_tasks.trigger_prefill_async"
        and "fighthealthinsurance.chooser_tasks._generate_single_task") and starts those patchers to prevent asynchronous
        database writes during tests. Stores the patcher and mock objects on the class as `prefill_patcher`, `generate_patcher`,
        `mock_prefill`, and `mock_generate`. After starting the patches, invokes parent class setup for the live server and
        Selenium base classes.
        """
        cls.prefill_patcher = patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async"
        )
        cls.generate_patcher = patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task"
        )
        cls.mock_prefill = cls.prefill_patcher.start()
        cls.mock_generate = cls.generate_patcher.start()

        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        """
        Perform class-level teardown for Selenium tests and stop started patchers.

        This method invokes superclass class-teardown logic and stops the `prefill_patcher` and `generate_patcher` started in setUpClass to restore patched functions to their original state.
        """
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()
        # Stop patching
        cls.prefill_patcher.stop()
        cls.generate_patcher.stop()

    def test_chooser_page_loads(self):
        """Test that the chooser page loads with selection options."""
        self.open(f"{self.live_server_url}/chooser/")

        # Check page title/heading
        self.assert_text("Help Us Improve Our AI Responses", timeout=10)

        # Check that both buttons exist
        self.assert_text("Score Appeal Letters")
        self.assert_text("Score Chat Responses")

        # Check the warning about synthetic data
        self.assert_text("synthetic/anonymized for training purposes")

    def test_chooser_page_has_task_selection_buttons(self):
        """Test that the chooser page has both task type buttons."""
        self.open(f"{self.live_server_url}/chooser/")

        # Verify buttons are present
        self.assert_element("button:contains('Score Appeal Letters')")
        self.assert_element("button:contains('Score Chat Responses')")

    def test_chooser_page_shows_help_text(self):
        """
        Verify the chooser page displays the main help heading "Help Us Improve Our AI Responses".
        """
        self.open(f"{self.live_server_url}/chooser/")

        # Should explain what the user is doing
        self.assert_text("Help Us Improve Our AI Responses", timeout=10)

    def test_chooser_page_has_privacy_disclaimer(self):
        """
        Verify the chooser page displays the privacy disclaimer stating data is synthetic/anonymized for training purposes.
        """
        self.open(f"{self.live_server_url}/chooser/")

        # Should have privacy disclaimer
        self.assert_text("synthetic/anonymized for training purposes")
