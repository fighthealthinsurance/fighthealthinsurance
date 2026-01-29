"""
Tests for state help views and functionality.
"""

import json
from unittest.mock import patch

from django.test import TestCase, Client, override_settings
from django.urls import reverse

from fighthealthinsurance.state_help import (
    load_state_help,
    get_state_help,
    get_state_help_by_abbreviation,
    get_states_sorted_by_name,
    StateHelp,
    StateHelpValidationError,
    InsuranceDepartment,
    ConsumerAssistance,
    MedicaidInfo,
)

# Sample state help data for testing
SAMPLE_STATE_DATA = {
    "california": {
        "slug": "california",
        "name": "California",
        "abbreviation": "CA",
        "insurance_department": {
            "name": "California Department of Insurance",
            "url": "https://www.insurance.ca.gov/",
            "phone": "916-492-3500",
            "consumer_line": "800-927-4357",
        },
        "consumer_assistance": {
            "cap_name": "California Health Consumer Alliance",
            "cap_url": "https://healthconsumer.org/",
            "cap_phone": "888-804-3536",
            "ship_name": "HICAP",
            "ship_url": "https://www.aging.ca.gov/",
            "ship_phone": "800-434-0222",
        },
        "medicaid": {
            "agency_name": "California Department of Health Care Services",
            "agency_url": "https://www.dhcs.ca.gov/",
            "agency_phone": "800-541-5555",
            "managed_care_ombudsman": {
                "name": "Medi-Cal Managed Care Office of the Ombudsman",
                "phone": "888-452-8609",
            },
        },
        "external_review": {"available": True, "info_url": "https://example.com"},
        "additional_resources": [
            {"name": "Health Consumer Alliance", "url": "https://healthconsumer.org/"}
        ],
    },
    "new-york": {
        "slug": "new-york",
        "name": "New York",
        "abbreviation": "NY",
        "insurance_department": {
            "name": "New York State Department of Financial Services",
            "url": "https://www.dfs.ny.gov/",
        },
        "consumer_assistance": {
            "cap_name": "Community Health Advocates",
        },
        "medicaid": {
            "agency_name": "New York State Department of Health",
        },
    },
}


class StateHelpDataClassTest(TestCase):
    """Tests for StateHelp data classes."""

    def test_state_help_from_valid_data(self):
        """Test creating StateHelp from valid data."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        self.assertEqual(state.slug, "california")
        self.assertEqual(state.name, "California")
        self.assertEqual(state.abbreviation, "CA")

    def test_state_help_requires_keys(self):
        """Test that StateHelp validates required keys."""
        invalid_data = {"slug": "test"}  # Missing required keys
        with self.assertRaises(StateHelpValidationError):
            StateHelp(invalid_data)

    def test_insurance_department_fields(self):
        """Test InsuranceDepartment data class."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        dept = state.insurance_department
        self.assertIsInstance(dept, InsuranceDepartment)
        self.assertEqual(dept.name, "California Department of Insurance")
        self.assertIsNotNone(dept.url)

    def test_consumer_assistance_fields(self):
        """Test ConsumerAssistance data class."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        ca = state.consumer_assistance
        self.assertIsInstance(ca, ConsumerAssistance)
        self.assertIsNotNone(ca.cap_name)
        self.assertIsNotNone(ca.ship_name)

    def test_medicaid_info_fields(self):
        """Test MedicaidInfo data class."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        medicaid = state.medicaid
        self.assertIsInstance(medicaid, MedicaidInfo)
        self.assertIsNotNone(medicaid.agency_name)
        self.assertIsNotNone(medicaid.managed_care_ombudsman)

    def test_external_review_field(self):
        """Test external review info."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        self.assertIsNotNone(state.external_review)
        self.assertTrue(state.external_review.available)

    def test_additional_resources(self):
        """Test additional resources list."""
        state = StateHelp(SAMPLE_STATE_DATA["california"])
        self.assertIsInstance(state.additional_resources, list)
        self.assertEqual(len(state.additional_resources), 1)


def mock_load_state_help():
    """Return sample state data for testing."""
    return {slug: StateHelp(data) for slug, data in SAMPLE_STATE_DATA.items()}


class StateHelpIndexViewTest(TestCase):
    """Tests for the state help index view."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("state_help_index")

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_state_help_index_page_loads(self, mock_get_states):
        """Test that the state help index page loads successfully."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_state_help_index_uses_correct_template(self, mock_get_states):
        """Test that the index uses the correct template."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "state_help_index.html")

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_state_help_index_has_title(self, mock_get_states):
        """Test that the page has appropriate title content."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get(self.url)
        self.assertContains(response, "State")

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_state_help_index_lists_states(self, mock_get_states):
        """Test that the index page lists states."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get(self.url)
        # Should contain our sample state names
        self.assertContains(response, "California")
        self.assertContains(response, "New York")

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_state_help_index_has_state_links(self, mock_get_states):
        """Test that index page has links to individual state pages."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get(self.url)
        # Check for link to California
        self.assertContains(
            response, reverse("state_help", kwargs={"slug": "california"})
        )


class StateHelpViewTest(TestCase):
    """Tests for individual state help pages."""

    def setUp(self):
        self.client = Client()

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_page_loads(self, mock_get_state):
        """Test that a state help page loads successfully."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_uses_correct_template(self, mock_get_state):
        """Test that the state page uses the correct template."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertTemplateUsed(response, "state_help.html")

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_displays_state_name(self, mock_get_state):
        """Test that the page displays the state name."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertContains(response, "California")

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_displays_insurance_department(self, mock_get_state):
        """Test that the page displays insurance department info."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertContains(response, "Department of Insurance")

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_displays_consumer_assistance(self, mock_get_state):
        """Test that the page displays consumer assistance info."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertContains(response, "Consumer Assistance")

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_displays_medicaid(self, mock_get_state):
        """Test that the page displays Medicaid info."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertContains(response, "Medicaid")

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_invalid_slug_returns_404(self, mock_get_state):
        """Test that an invalid state slug returns 404."""
        mock_get_state.return_value = None  # State not found
        url = reverse("state_help", kwargs={"slug": "not-a-real-state"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    @patch("fighthealthinsurance.state_help.get_state_help")
    def test_state_help_has_back_link(self, mock_get_state):
        """Test that state page has a link back to the index."""
        mock_get_state.return_value = StateHelp(SAMPLE_STATE_DATA["california"])
        url = reverse("state_help", kwargs={"slug": "california"})
        response = self.client.get(url)
        self.assertContains(response, reverse("state_help_index"))


class StateHelpSitemapTest(TestCase):
    """Tests for state help sitemap integration."""

    def setUp(self):
        self.client = Client()

    @patch("fighthealthinsurance.state_help.get_states_sorted_by_name")
    def test_sitemap_includes_state_help(self, mock_get_states):
        """Test that the sitemap includes state help pages."""
        mock_get_states.return_value = list(mock_load_state_help().values())
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        # Should include state help index
        self.assertIn("state-help", content)
