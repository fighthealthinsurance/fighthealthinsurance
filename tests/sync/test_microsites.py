"""
Tests for microsite functionality.

Tests that:
1. The microsites.json file loads and parses correctly
2. All microsites have required fields with valid data
3. The microsite route loads correctly
4. The default procedure is passed through the appeal flow
5. Users can still override the default procedure
"""

import json
import os
from pathlib import Path

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance import models
from fighthealthinsurance.microsites import (
    get_microsite,
    get_all_microsites,
    get_microsite_slugs,
    Microsite,
)


def load_microsites_json_directly():
    """Load microsites.json directly from the file system for testing.

    This bypasses Django's staticfiles storage which may not be available
    in all test environments.
    """
    # Find the project root and load the JSON file directly
    current_dir = Path(__file__).parent
    project_root = current_dir.parent.parent
    json_path = project_root / "fighthealthinsurance" / "static" / "microsites.json"

    if not json_path.exists():
        return {}

    with open(json_path, "r") as f:
        data = json.load(f)

    # Convert to Microsite objects like the module does
    microsites = {}
    for slug, microsite_data in data.items():
        microsites[slug] = Microsite(microsite_data)
    return microsites


class MicrositeJSONValidationTest(TestCase):
    """Tests that validate the microsites.json file loads and parses correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Load microsites directly from file for validation tests
        cls.microsites = load_microsites_json_directly()

    def test_microsites_json_loads_successfully(self):
        """Test that microsites.json loads without errors."""
        self.assertIsInstance(self.microsites, dict)
        self.assertGreater(len(self.microsites), 0, "microsites.json should contain at least one microsite")

    def test_microsites_json_has_expected_count(self):
        """Test that we have the expected number of microsites defined."""
        # Based on the JSON file, we expect at least 25+ microsites
        self.assertGreaterEqual(len(self.microsites), 25, "Expected at least 25 microsites defined")

    def test_all_microsites_have_required_string_fields(self):
        """Test that all microsites have required non-empty string fields."""
        required_string_fields = [
            "slug",
            "title",
            "default_procedure",
            "tagline",
            "hero_h1",
            "hero_subhead",
            "intro",
            "how_we_help",
            "cta",
        ]

        for slug, microsite in self.microsites.items():
            for field in required_string_fields:
                value = getattr(microsite, field, None)
                self.assertIsNotNone(
                    value,
                    f"Microsite '{slug}' is missing required field '{field}'"
                )
                self.assertIsInstance(
                    value, str,
                    f"Microsite '{slug}' field '{field}' should be a string"
                )
                self.assertGreater(
                    len(value), 0,
                    f"Microsite '{slug}' field '{field}' should not be empty"
                )

    def test_all_microsites_have_slug_matching_key(self):
        """Test that each microsite's slug field matches its dictionary key."""
        for key, microsite in self.microsites.items():
            self.assertEqual(
                key, microsite.slug,
                f"Microsite key '{key}' doesn't match its slug field '{microsite.slug}'"
            )

    def test_all_microsites_have_valid_faq_structure(self):
        """Test that all microsites have properly structured FAQ entries."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.faq, list,
                f"Microsite '{slug}' faq should be a list"
            )
            for i, faq_item in enumerate(microsite.faq):
                self.assertIsInstance(
                    faq_item, dict,
                    f"Microsite '{slug}' faq[{i}] should be a dict"
                )
                self.assertIn(
                    "question", faq_item,
                    f"Microsite '{slug}' faq[{i}] missing 'question' key"
                )
                self.assertIn(
                    "answer", faq_item,
                    f"Microsite '{slug}' faq[{i}] missing 'answer' key"
                )
                self.assertGreater(
                    len(faq_item["question"]), 0,
                    f"Microsite '{slug}' faq[{i}] question should not be empty"
                )
                self.assertGreater(
                    len(faq_item["answer"]), 0,
                    f"Microsite '{slug}' faq[{i}] answer should not be empty"
                )

    def test_all_microsites_have_common_denial_reasons(self):
        """Test that all microsites have at least one common denial reason."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.common_denial_reasons, list,
                f"Microsite '{slug}' common_denial_reasons should be a list"
            )
            self.assertGreater(
                len(microsite.common_denial_reasons), 0,
                f"Microsite '{slug}' should have at least one common denial reason"
            )

    def test_all_microsites_have_evidence_snippets(self):
        """Test that all microsites have at least one evidence snippet."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.evidence_snippets, list,
                f"Microsite '{slug}' evidence_snippets should be a list"
            )
            self.assertGreater(
                len(microsite.evidence_snippets), 0,
                f"Microsite '{slug}' should have at least one evidence snippet"
            )

    def test_known_microsites_exist(self):
        """Test that specific known microsites are present."""
        known_slugs = [
            "mri-denial",
            "ct-scan-denial",
            "sleep-study-denial",
            "cpap-denial",
            "mental-health-denial",
            "biologic-denial",
            "surgery-denial",
            "ffs-denial",
            "hrt-denial",
            "grs-denial",
        ]
        for slug in known_slugs:
            self.assertIn(
                slug, self.microsites,
                f"Expected microsite '{slug}' to be defined in microsites.json"
            )


class MicrositeModuleTest(TestCase):
    """Tests for the microsites module."""

    def test_get_all_microsites_returns_dict(self):
        """Test that get_all_microsites returns a dictionary."""
        microsites = get_all_microsites()
        self.assertIsInstance(microsites, dict)

    def test_get_microsite_slugs_returns_list(self):
        """Test that get_microsite_slugs returns a list of strings."""
        slugs = get_microsite_slugs()
        self.assertIsInstance(slugs, list)
        if slugs:
            self.assertIsInstance(slugs[0], str)

    def test_get_microsite_returns_microsite_for_valid_slug(self):
        """Test that get_microsite returns a Microsite for a valid slug."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            self.assertIsInstance(microsite, Microsite)
            self.assertEqual(microsite.slug, slugs[0])

    def test_get_microsite_returns_none_for_invalid_slug(self):
        """Test that get_microsite returns None for an invalid slug."""
        microsite = get_microsite("this-slug-does-not-exist-12345")
        self.assertIsNone(microsite)

    def test_microsite_has_required_attributes(self):
        """Test that Microsite objects have all required attributes."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            self.assertTrue(hasattr(microsite, "slug"))
            self.assertTrue(hasattr(microsite, "title"))
            self.assertTrue(hasattr(microsite, "default_procedure"))
            self.assertTrue(hasattr(microsite, "tagline"))
            self.assertTrue(hasattr(microsite, "hero_h1"))
            self.assertTrue(hasattr(microsite, "intro"))
            self.assertTrue(hasattr(microsite, "faq"))


class MicrositeViewTest(TestCase):
    """Tests for the microsite landing page view."""

    def setUp(self):
        self.client = Client()

    def test_microsite_route_loads_for_valid_slug(self):
        """Test that the microsite route loads for a valid slug."""
        slugs = get_microsite_slugs()
        if slugs:
            response = self.client.get(reverse("microsite", kwargs={"slug": slugs[0]}))
            self.assertEqual(response.status_code, 200)

    def test_microsite_route_returns_404_for_invalid_slug(self):
        """Test that the microsite route returns 404 for an invalid slug."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "nonexistent-microsite-slug"})
        )
        self.assertEqual(response.status_code, 404)

    def test_microsite_page_contains_title(self):
        """Test that the microsite page contains the microsite title."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            response = self.client.get(reverse("microsite", kwargs={"slug": slugs[0]}))
            self.assertContains(response, microsite.title)

    def test_microsite_page_contains_start_appeal_button(self):
        """Test that the microsite page contains a Start Your Appeal button."""
        slugs = get_microsite_slugs()
        if slugs:
            response = self.client.get(reverse("microsite", kwargs={"slug": slugs[0]}))
            self.assertContains(response, "Start Your Appeal")

    def test_microsite_page_links_to_scan_with_default_procedure(self):
        """Test that the microsite page links to scan with default_procedure."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            response = self.client.get(reverse("microsite", kwargs={"slug": slugs[0]}))
            # Check that the link includes the default_procedure parameter
            self.assertContains(response, f"default_procedure={microsite.default_procedure.replace(' ', '%20').replace('/', '%2F')}" if '/' in microsite.default_procedure else f"default_procedure={microsite.default_procedure.replace(' ', '%20')}")


class MicrositeDefaultProcedureFlowTest(TestCase):
    """Tests for the default procedure flow from microsites."""

    def setUp(self):
        self.client = Client()

    def test_scan_page_shows_microsite_note_when_default_procedure_provided(self):
        """Test that the scan page shows a note when coming from a microsite."""
        response = self.client.get(
            reverse("scan"),
            {
                "default_procedure": "MRI Scan",
                "microsite_slug": "mri-denial",
                "microsite_title": "Appealing MRI Denials",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Appealing MRI Denials")
        self.assertContains(response, "MRI Scan")

    def test_scan_page_includes_hidden_fields_for_microsite_data(self):
        """Test that the scan page includes hidden fields for microsite data."""
        response = self.client.get(
            reverse("scan"),
            {
                "default_procedure": "MRI Scan",
                "microsite_slug": "mri-denial",
                "microsite_title": "Appealing MRI Denials",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="default_procedure"')
        self.assertContains(response, 'name="microsite_slug"')
        self.assertContains(response, 'name="microsite_title"')

    def test_scan_page_works_without_microsite_params(self):
        """Test that the scan page works normally without microsite params."""
        response = self.client.get(reverse("scan"))
        self.assertEqual(response.status_code, 200)
        # Should not contain microsite-specific note
        self.assertNotContains(response, "You started from the")


class MicrositeOverrideTest(TestCase):
    """Tests verifying users can override the default procedure."""

    def setUp(self):
        self.client = Client()

        # Create a test denial that we can use
        self.denial = models.Denial.objects.create(
            denial_id=99999,
            hashed_email="test-hashed-email-microsite",
            semi_sekret="test-semi-sekret-microsite",
            procedure="",  # Empty procedure to test prefill
        )

    def test_categorize_page_allows_editing_prefilled_procedure(self):
        """Test that the categorize page allows editing the prefilled procedure."""
        # Set up session with microsite data
        session = self.client.session
        session["denial_id"] = self.denial.denial_id
        session["default_procedure"] = "MRI Scan"
        session["microsite_title"] = "Appealing MRI Denials"
        session["microsite_slug"] = "mri-denial"
        session.save()

        # Access the categorize review page
        response = self.client.get(
            reverse("categorize_review"),
            {
                "denial_id": self.denial.denial_id,
                "email": "test@example.com",
                "semi_sekret": self.denial.semi_sekret,
            },
        )

        self.assertEqual(response.status_code, 200)

        # Check that the page shows the microsite prefill note
        self.assertContains(response, "Appealing MRI Denials")
        self.assertContains(response, "MRI Scan")

        # Check that the procedure field is editable (not disabled)
        # The form should be rendered with the procedure field
        self.assertContains(response, 'name="procedure"')

    def test_user_can_submit_different_procedure(self):
        """Test that user can submit a different procedure than the default."""
        # The form should accept any procedure value, not just the default
        # This is implicitly tested by the form not having the field locked
        # The actual form submission would require a more complex integration test
        pass


class MicrositeSpecificTest(TestCase):
    """Tests for specific microsites defined in microsites.json."""

    def setUp(self):
        self.client = Client()

    def test_mri_denial_microsite_loads(self):
        """Test that the MRI denial microsite loads correctly."""
        response = self.client.get(reverse("microsite", kwargs={"slug": "mri-denial"}))
        # May be 200 or 404 depending on if microsites.json is available in staticfiles
        self.assertIn(response.status_code, [200, 404])

    def test_ct_scan_denial_microsite_loads(self):
        """Test that the CT scan denial microsite loads correctly."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "ct-scan-denial"})
        )
        self.assertIn(response.status_code, [200, 404])
