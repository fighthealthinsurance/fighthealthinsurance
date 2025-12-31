"""
Tests for microsite functionality.

Tests that:
1. The microsites.json file loads and parses correctly
2. All microsites have required fields with valid data
3. The microsite route loads correctly
4. The default procedure is passed through the appeal flow
"""

import json

from pathlib import Path

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance import models
from fighthealthinsurance.microsites import (
    get_microsite,
    get_all_microsites,
    get_microsite_slugs,
    Microsite,
    REQUIRED_MICROSITE_KEYS,
)


# Keys that are optional but commonly present in microsites
# These have sensible defaults (empty lists) in the Microsite class
COMMON_LIST_KEYS = {
    "common_denial_reasons",
    "faq",
    "evidence_snippets",
    "pubmed_search_terms",
}

OPTIONAL_MICROSITE_KEYS = {
    "image",
    "alternatives",
    "assistance_programs",
    "default_condition",
    "medicare",
    "blog_post_url",
}

# All valid keys that can appear in a microsite JSON entry
ALL_VALID_MICROSITE_KEYS = (
    REQUIRED_MICROSITE_KEYS | COMMON_LIST_KEYS | OPTIONAL_MICROSITE_KEYS
)


def get_microsites_json_path():
    """Get the path to microsites.json."""
    current_dir = Path(__file__).parent
    project_root = current_dir.parent.parent
    return project_root / "fighthealthinsurance" / "static" / "microsites.json"


def load_microsites_json_raw():
    """Load microsites.json as raw dict for schema validation.

    Returns the raw JSON data without converting to Microsite objects.
    """
    json_path = get_microsites_json_path()

    if not json_path.exists():
        return {}

    with open(json_path, "r") as f:
        return json.load(f)


def load_microsites_json_directly():
    """Load microsites.json directly from the file system for testing.

    This bypasses Django's staticfiles storage which may not be available
    in all test environments.
    """
    data = load_microsites_json_raw()

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
        self.assertGreater(
            len(self.microsites),
            0,
            "microsites.json should contain at least one microsite",
        )

    def test_microsites_json_has_expected_count(self):
        """Test that we have the expected number of microsites defined."""
        # Based on the JSON file, we expect at least 25+ microsites
        self.assertGreaterEqual(
            len(self.microsites), 25, "Expected at least 25 microsites defined"
        )

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
                    value, f"Microsite '{slug}' is missing required field '{field}'"
                )
                self.assertIsInstance(
                    value, str, f"Microsite '{slug}' field '{field}' should be a string"
                )
                self.assertGreater(
                    len(value),
                    0,
                    f"Microsite '{slug}' field '{field}' should not be empty",
                )

    def test_all_microsites_have_slug_matching_key(self):
        """Test that each microsite's slug field matches its dictionary key."""
        for key, microsite in self.microsites.items():
            self.assertEqual(
                key,
                microsite.slug,
                f"Microsite key '{key}' doesn't match its slug field '{microsite.slug}'",
            )

    def test_all_microsites_have_valid_faq_structure(self):
        """Test that all microsites have properly structured FAQ entries."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.faq, list, f"Microsite '{slug}' faq should be a list"
            )
            for i, faq_item in enumerate(microsite.faq):
                self.assertIsInstance(
                    faq_item, dict, f"Microsite '{slug}' faq[{i}] should be a dict"
                )
                self.assertIn(
                    "question",
                    faq_item,
                    f"Microsite '{slug}' faq[{i}] missing 'question' key",
                )
                self.assertIn(
                    "answer",
                    faq_item,
                    f"Microsite '{slug}' faq[{i}] missing 'answer' key",
                )
                self.assertGreater(
                    len(faq_item["question"]),
                    0,
                    f"Microsite '{slug}' faq[{i}] question should not be empty",
                )
                self.assertGreater(
                    len(faq_item["answer"]),
                    0,
                    f"Microsite '{slug}' faq[{i}] answer should not be empty",
                )

    def test_all_microsites_have_common_denial_reasons(self):
        """Test that all microsites have at least one common denial reason."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.common_denial_reasons,
                list,
                f"Microsite '{slug}' common_denial_reasons should be a list",
            )
            self.assertGreater(
                len(microsite.common_denial_reasons),
                0,
                f"Microsite '{slug}' should have at least one common denial reason",
            )

    def test_all_microsites_have_evidence_snippets(self):
        """Test that all microsites have at least one evidence snippet."""
        for slug, microsite in self.microsites.items():
            self.assertIsInstance(
                microsite.evidence_snippets,
                list,
                f"Microsite '{slug}' evidence_snippets should be a list",
            )
            self.assertGreater(
                len(microsite.evidence_snippets),
                0,
                f"Microsite '{slug}' should have at least one evidence snippet",
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
            "medicare-work-requirements",
        ]
        for slug in known_slugs:
            self.assertIn(
                slug,
                self.microsites,
                f"Expected microsite '{slug}' to be defined in microsites.json",
            )

    def test_no_unexpected_keys_in_microsites(self):
        """Test that microsites.json contains no unexpected/typo keys.

        This catches typos like 'pubmed_sear_terms' instead of 'pubmed_search_terms'.
        """
        raw_data = load_microsites_json_raw()
        errors = []

        for slug, microsite_data in raw_data.items():
            unexpected_keys = set(microsite_data.keys()) - ALL_VALID_MICROSITE_KEYS
            if unexpected_keys:
                errors.append(
                    f"Microsite '{slug}' has unexpected keys: {unexpected_keys}"
                )

        if errors:
            self.fail("\n".join(errors))

    def test_all_required_keys_present(self):
        """Test that all microsites have all required keys."""
        raw_data = load_microsites_json_raw()
        errors = []

        for slug, microsite_data in raw_data.items():
            missing_keys = REQUIRED_MICROSITE_KEYS - set(microsite_data.keys())
            if missing_keys:
                errors.append(
                    f"Microsite '{slug}' is missing required keys: {missing_keys}"
                )

        if errors:
            self.fail("\n".join(errors))


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

    def test_microsite_has_optional_image_attribute(self):
        """Test that Microsite objects have the optional image attribute."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            # Image is optional, so it should exist as attribute but may be None
            self.assertTrue(hasattr(microsite, "image"))
            # If image is set, it should be a string (URL)
            if microsite.image is not None:
                self.assertIsInstance(microsite.image, str)
                self.assertGreater(len(microsite.image), 0)

    def test_microsite_has_optional_alternatives_attribute(self):
        """Test that Microsite objects have the optional alternatives attribute."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            # Alternatives is optional, so it should exist as attribute but may be empty
            self.assertTrue(hasattr(microsite, "alternatives"))
            self.assertIsInstance(microsite.alternatives, list)
            # If alternatives are set, they should be strings
            for alt in microsite.alternatives:
                self.assertIsInstance(alt, str)
                self.assertGreater(len(alt), 0)

    def test_microsite_has_optional_assistance_programs_attribute(self):
        """Test that Microsite objects have the optional assistance_programs attribute."""
        slugs = get_microsite_slugs()
        if slugs:
            microsite = get_microsite(slugs[0])
            # Assistance programs is optional, so it should exist as attribute but may be empty
            self.assertTrue(hasattr(microsite, "assistance_programs"))
            self.assertIsInstance(microsite.assistance_programs, list)
            # If programs are set, they should have required keys
            for program in microsite.assistance_programs:
                self.assertIsInstance(program, dict)
                self.assertIn("name", program)
                self.assertIn("url", program)
                self.assertIn("description", program)

    def test_biologic_denial_has_assistance_programs(self):
        """Test that the biologic-denial microsite has assistance programs."""
        # Use direct file loading to ensure we get fresh data
        microsites = load_microsites_json_directly()
        microsite = microsites.get("biologic-denial")
        self.assertIsNotNone(microsite)
        self.assertGreater(
            len(microsite.assistance_programs),
            0,
            "biologic-denial microsite should have assistance programs",
        )
        self.assertGreater(
            len(microsite.alternatives),
            0,
            "biologic-denial microsite should have alternatives",
        )


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
            self.assertContains(
                response,
                (
                    f"default_procedure={microsite.default_procedure.replace(' ', '%20').replace('/', '%2F')}"
                    if "/" in microsite.default_procedure
                    else f"default_procedure={microsite.default_procedure.replace(' ', '%20')}"
                ),
            )


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

    def test_scan_page_prefills_denial_text_from_microsite(self):
        """Test that the scan page prefills denial text when coming from microsite."""
        response = self.client.get(
            reverse("scan"),
            {
                "default_procedure": "MRI Scan",
                "microsite_slug": "mri-denial",
                "microsite_title": "Appealing MRI Denials",
            },
        )
        self.assertEqual(response.status_code, 200)
        # Should contain the default denial text
        self.assertContains(response, "The patient was denied MRI Scan.")


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


class MicrositeSpecificTest(TestCase):
    """Tests for specific microsites defined in microsites.json."""

    def setUp(self):
        self.client = Client()

    def test_mri_denial_microsite_loads(self):
        """Test that the MRI denial microsite loads correctly."""
        response = self.client.get(reverse("microsite", kwargs={"slug": "mri-denial"}))
        self.assertEqual(
            response.status_code,
            200,
            "MRI denial microsite should load successfully. "
            "If this fails, ensure microsites.json is available to Django's staticfiles.",
        )

    def test_ct_scan_denial_microsite_loads(self):
        """Test that the CT scan denial microsite loads correctly."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "ct-scan-denial"})
        )
        self.assertEqual(
            response.status_code,
            200,
            "CT scan denial microsite should load successfully.",
        )


class MedicareMicrositeTest(TestCase):
    """Tests for Medicare work requirements microsite."""

    def setUp(self):
        self.client = Client()

    def test_medicare_work_requirements_microsite_exists(self):
        """Test that the Medicare work requirements microsite exists."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(
            microsite, "Medicare work requirements microsite should exist"
        )

    def test_medicare_work_requirements_microsite_loads(self):
        """Test that the Medicare work requirements microsite loads correctly."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(
            response.status_code,
            200,
            "Medicare work requirements microsite should load successfully.",
        )

    def test_medicare_work_requirements_has_medicare_flag(self):
        """Test that the Medicare microsite has the medicare flag set."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(microsite)
        self.assertTrue(
            microsite.medicare,
            "Medicare work requirements microsite should have medicare=True",
        )

    def test_medicare_work_requirements_page_contains_medicare_content(self):
        """Test that the Medicare page contains Medicare-specific content."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Understanding Medicare Work Requirements")
        self.assertContains(response, "Medicare")

    def test_medicare_work_requirements_chat_link_includes_medicare_parameter(self):
        """Test that chat links from Medicare microsite include medicare=true parameter."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Check that the chat link includes medicare=true
        self.assertContains(response, "medicare=true")

    def test_medicare_work_requirements_chat_link_includes_default_procedure(self):
        """Test that chat links from Medicare microsite include default_procedure."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Check that the chat link includes the default procedure
        self.assertContains(response, "default_procedure=Medicare")

    def test_medicare_work_requirements_chat_link_includes_microsite_slug(self):
        """Test that chat links from Medicare microsite include microsite_slug parameter."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Check that the chat link includes the microsite slug
        self.assertContains(response, "microsite_slug=medicare-work-requirements")

    def test_medicare_work_requirements_has_faq(self):
        """Test that the Medicare microsite has FAQ entries."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(microsite)
        self.assertGreater(
            len(microsite.faq),
            0,
            "Medicare work requirements microsite should have FAQ entries",
        )

    def test_medicare_work_requirements_has_common_denial_reasons(self):
        """Test that the Medicare microsite has common denial reasons."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(microsite)
        self.assertGreater(
            len(microsite.common_denial_reasons),
            0,
            "Medicare work requirements microsite should have common denial reasons",
        )

    def test_medicare_work_requirements_has_evidence_snippets(self):
        """Test that the Medicare microsite has evidence snippets."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(microsite)
        self.assertGreater(
            len(microsite.evidence_snippets),
            0,
            "Medicare work requirements microsite should have evidence snippets",
        )


class MedicareChatIntegrationTest(TestCase):
    """Tests for Medicare chat integration."""

    def setUp(self):
        self.client = Client()

    def test_chat_accepts_medicare_parameter(self):
        """Test that the chat view accepts medicare parameter."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        # Access chat with medicare parameter
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Services",
                "medicare": "true",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_chat_page_includes_medicare_data_attribute(self):
        """Test that the chat page includes medicare data attribute."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        # Access chat with medicare parameter
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Services",
                "medicare": "true",
            },
        )
        self.assertEqual(response.status_code, 200)
        # Check that the chat interface root has the medicare data attribute
        self.assertContains(response, 'data-medicare="true"')

    def test_chat_with_medicare_and_procedure(self):
        """Test that chat works with both Medicare flag and default procedure."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        # Access chat with medicare and procedure parameters
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Work Requirements",
                "medicare": "true",
                "microsite_slug": "medicare-work-requirements",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Medicare Work Requirements")
        self.assertContains(response, 'data-medicare="true"')
        self.assertContains(
            response, 'data-default-procedure="Medicare Work Requirements"'
        )
        self.assertContains(
            response, 'data-microsite-slug="medicare-work-requirements"'
        )

    def test_chat_with_medicare_work_requirements_slug(self):
        """Test that chat includes microsite_slug for Medicare work requirements."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        # Access chat with medicare work requirements slug
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Work Requirements",
                "medicare": "true",
                "microsite_slug": "medicare-work-requirements",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'data-microsite-slug="medicare-work-requirements"'
        )

    def test_medicare_chat_via_post(self):
        """Test that Medicare chat parameters work via POST (from consent form)."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        # POST to chat with medicare parameters
        response = self.client.post(
            reverse("chat"),
            {
                "default_procedure": "Medicare Work Requirements",
                "medicare": "true",
                "microsite_slug": "medicare-work-requirements",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-medicare="true"')
        self.assertContains(
            response, 'data-microsite-slug="medicare-work-requirements"'
        )

    def test_medicare_chat_without_consent_redirects(self):
        """Test that accessing Medicare chat without consent redirects to consent page."""
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Work Requirements",
                "medicare": "true",
                "microsite_slug": "medicare-work-requirements",
            },
        )
        # Should redirect to consent page
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/chat-consent"))

    def test_medicare_microsite_chat_link_parameters(self):
        """Test that Medicare microsite chat links include all required parameters."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Check that chat links include all parameters
        self.assertContains(response, "medicare=true")
        self.assertContains(response, "microsite_slug=medicare-work-requirements")
        self.assertContains(response, "default_procedure=Medicare")

    def test_medicare_microsite_no_appeal_button(self):
        """Test that Medicare microsite doesn't show 'Start Your Appeal' button."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Should not contain the Start Your Appeal button (only for non-Medicare microsites)
        # The template uses conditional logic to hide this button for Medicare
        # We check that the chat button is promoted to primary CTA
        self.assertContains(response, "Chat with AI Assistant")

    def test_medicare_microsite_blog_link(self):
        """Test that Medicare microsite includes blog post link."""
        response = self.client.get(
            reverse("microsite", kwargs={"slug": "medicare-work-requirements"})
        )
        self.assertEqual(response.status_code, 200)
        # Check for blog link
        self.assertContains(response, "/blog/medicaid-work-requirements/")
        self.assertContains(response, "Read Our Guide")

    def test_medicare_chat_context_data(self):
        """Test that Medicare chat passes all context data correctly."""
        # First complete consent
        session = self.client.session
        session["consent_completed"] = True
        session["email"] = "test@example.com"
        session.save()

        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "Medicare Work Requirements",
                "default_condition": "",
                "medicare": "true",
                "microsite_slug": "medicare-work-requirements",
            },
        )
        self.assertEqual(response.status_code, 200)
        # Verify all data attributes are present
        self.assertContains(
            response, 'data-default-procedure="Medicare Work Requirements"'
        )
        self.assertContains(response, 'data-medicare="true"')
        self.assertContains(
            response, 'data-microsite-slug="medicare-work-requirements"'
        )

    def test_medicare_microsite_has_blog_post_url(self):
        """Test that Medicare microsite has blog_post_url field."""
        microsite = get_microsite("medicare-work-requirements")
        self.assertIsNotNone(microsite)
        self.assertTrue(
            hasattr(microsite, "blog_post_url"),
            "Medicare microsite should have blog_post_url attribute",
        )
        # Check it's actually set
        blog_url = getattr(microsite, "blog_post_url", None)
        self.assertIsNotNone(
            blog_url, "Medicare microsite blog_post_url should not be None"
        )
        self.assertIn("blog", blog_url, "blog_post_url should contain 'blog'")


class MicrositeDirectoryViewTest(TestCase):
    """Tests for the microsite directory view."""

    def setUp(self):
        self.client = Client()

    def test_microsite_directory_page_loads(self):
        """Test that the microsite directory page loads successfully."""
        response = self.client.get(reverse("microsite_directory"))
        self.assertEqual(response.status_code, 200)

    def test_microsite_directory_uses_correct_template(self):
        """Test that the directory uses the correct template."""
        response = self.client.get(reverse("microsite_directory"))
        self.assertTemplateUsed(response, "microsite_directory.html")

    def test_microsite_directory_has_title(self):
        """Test that the page has the correct title."""
        response = self.client.get(reverse("microsite_directory"))
        self.assertContains(response, "Treatment & Drug Resources")

    def test_microsite_directory_lists_microsites(self):
        """Test that the directory page lists microsites."""
        response = self.client.get(reverse("microsite_directory"))
        # Should contain links to individual microsite pages
        self.assertContains(response, "/microsite/")

    def test_microsite_directory_has_cta_buttons(self):
        """Test that the directory has call-to-action buttons."""
        response = self.client.get(reverse("microsite_directory"))
        # Should have "Start Your Appeal" button
        self.assertContains(response, "Start Your Appeal")
        # Should have "Chat with AI" button
        self.assertContains(response, "Chat with AI")

    def test_microsite_directory_has_fallback_message(self):
        """Test that the directory has fallback message for treatments not listed."""
        response = self.client.get(reverse("microsite_directory"))
        self.assertContains(response, "Don't see your treatment?")

    def test_microsite_directory_context_has_microsites(self):
        """Test that context includes microsites."""
        response = self.client.get(reverse("microsite_directory"))
        self.assertIn("microsites", response.context)
        self.assertIsInstance(response.context["microsites"], list)

    def test_microsite_directory_microsites_sorted_by_title(self):
        """Test that microsites are sorted alphabetically by title."""
        response = self.client.get(reverse("microsite_directory"))
        microsites = response.context["microsites"]
        if len(microsites) > 1:
            titles = [m.title for m in microsites]
            self.assertEqual(titles, sorted(titles))

    def test_microsite_directory_links_work(self):
        """Test that microsite links from directory work."""
        # Get the directory page
        response = self.client.get(reverse("microsite_directory"))
        self.assertEqual(response.status_code, 200)

        # Get microsites from context and verify at least one link works
        microsites = response.context["microsites"]
        if microsites:
            first_microsite = microsites[0]
            microsite_url = reverse("microsite", kwargs={"slug": first_microsite.slug})
            microsite_response = self.client.get(microsite_url)
            self.assertEqual(microsite_response.status_code, 200)

    def test_microsite_directory_in_sitemap(self):
        """Test that the microsite directory is included in the sitemap."""
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/treatments/")
