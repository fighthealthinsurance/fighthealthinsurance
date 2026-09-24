"""
Tests for microsite data integration into appeal flow and chat interfaces.

Tests that:
1. The microsite_slug is stored in the Denial model
2. Microsite pubmed_search_terms are used in PubMed searches
3. Microsite evidence_snippets are included in citation generation
4. Microsite data is passed to chat interface
"""

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance import models
from fighthealthinsurance.microsites import get_microsite


class MicrositeSlugStorageTest(TestCase):
    """Tests that microsite_slug is stored in Denial model."""

    def setUp(self):
        self.client = Client()

    def test_denial_stores_microsite_slug_from_scan_page(self):
        """Test that the Denial model stores microsite_slug when coming from a microsite."""
        # Submit the initial scan form with microsite parameters
        response = self.client.post(
            reverse("scan"),
            {
                "email": "test@example.com",
                "denial_text": "The patient was denied MRI Scan.",
                "zip": "12345",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "microsite_slug": "mri-denial",
                "default_procedure": "MRI Scan",
                "microsite_title": "Appealing MRI Denials",
            },
        )

        # Should render health_history page after successful submission (not redirect)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Health History")

        # Check that a denial was created with the microsite_slug
        denial = models.Denial.objects.filter(
            hashed_email=models.Denial.get_hashed_email("test@example.com")
        ).first()
        self.assertIsNotNone(denial)
        self.assertEqual(denial.microsite_slug, "mri-denial")

    def test_denial_without_microsite_slug(self):
        """Test that denials can be created without microsite_slug."""
        response = self.client.post(
            reverse("scan"),
            {
                "email": "test2@example.com",
                "denial_text": "Generic denial text.",
                "zip": "12345",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
        )

        # Should render health_history page after successful submission (not redirect)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Health History")

        # Check that a denial was created without microsite_slug
        denial = models.Denial.objects.filter(
            hashed_email=models.Denial.get_hashed_email("test2@example.com")
        ).first()
        self.assertIsNotNone(denial)
        self.assertIsNone(denial.microsite_slug)


class MicrositeChatLeadsTest(TestCase):
    """Tests that microsite_slug is passed to ChatLeads."""

    def setUp(self):
        self.client = Client()

    def test_chat_interface_receives_microsite_slug(self):
        """Test that the chat interface receives microsite_slug from URL params."""
        # First complete the consent process
        self.client.post(
            reverse("chat_consent"),
            {
                "first_name": "Test",
                "last_name": "User",
                "email": "chattest@example.com",
                "phone": "1234567890",
                "address": "123 Test St",
                "city": "Test City",
                "state": "CA",
                "zip_code": "12345",
                "tos_agreement": "on",
                "privacy_policy": "on",
            },
        )

        # Now access the chat interface with microsite parameters
        response = self.client.get(
            reverse("chat"),
            {
                "default_procedure": "MRI Scan",
                "default_condition": "Back Pain",
                "microsite_slug": "mri-denial",
            },
        )

        self.assertEqual(response.status_code, 200)
        # Check that the template receives the microsite_slug
        self.assertContains(response, 'data-microsite-slug="mri-denial"')
        self.assertContains(response, 'data-default-procedure="MRI Scan"')
        self.assertContains(response, 'data-default-condition="Back Pain"')


class MicrositeDataIntegrationTest(TestCase):
    """Tests that microsite data (pubmed_search_terms, evidence_snippets) are properly used."""

    def test_microsite_pubmed_search_terms_available(self):
        """Test that microsite pubmed_search_terms are accessible."""
        microsite = get_microsite("mri-denial")
        self.assertIsNotNone(microsite)
        self.assertIsInstance(microsite.pubmed_search_terms, list)
        self.assertGreater(len(microsite.pubmed_search_terms), 0)

    def test_microsite_evidence_snippets_available(self):
        """Test that microsite evidence_snippets are accessible."""
        microsite = get_microsite("mri-denial")
        self.assertIsNotNone(microsite)
        self.assertIsInstance(microsite.evidence_snippets, list)
        self.assertGreater(len(microsite.evidence_snippets), 0)


class MicrositeTemplateTest(TestCase):
    """Tests that microsite templates pass slug to chat and appeal links."""

    def setUp(self):
        self.client = Client()

    def test_microsite_page_includes_slug_in_chat_link(self):
        """Test that the microsite page includes microsite_slug in the chat link."""
        response = self.client.get(reverse("microsite", kwargs={"slug": "mri-denial"}))
        self.assertEqual(response.status_code, 200)
        # Check that the chat link includes microsite_slug parameter
        self.assertContains(response, "microsite_slug=mri-denial")

    def test_microsite_page_includes_slug_in_appeal_link(self):
        """Test that the microsite page includes microsite_slug in the appeal link."""
        response = self.client.get(reverse("microsite", kwargs={"slug": "mri-denial"}))
        self.assertEqual(response.status_code, 200)
        # Check that the appeal link includes microsite_slug parameter
        self.assertContains(response, "microsite_slug=mri-denial")
        self.assertContains(response, "default_procedure=MRI")


class MicrositeGuideLinkTest(TestCase):
    """The guide link wears the site's buttons, not Bootstrap's blue.

    It was Bootstrap's blue outline in both places: thin blue text on the
    hero photo, hard to read and the only hero button in that colour. In
    the hero it now takes the frosted outline the other hero buttons use,
    and in the closing call to action the green outline its neighbours use.
    Only the Medicare work requirements page has a guide link today.
    """

    SLUG = "medicare-work-requirements-denial"

    def _guide_link_classes(self):
        guide = get_microsite(self.SLUG).blog_post_url
        self.assertTrue(guide, "the test page no longer has a guide link")
        html = self.client.get(reverse("microsite", kwargs={"slug": self.SLUG})).content.decode()
        marker = 'href="%s" class="' % guide
        found = []
        start = html.find(marker)
        while start != -1:
            begin = start + len(marker)
            found.append(html[begin : html.index('"', begin)].split())
            start = html.find(marker, begin)
        return found

    def test_the_hero_guide_link_is_frosted_like_the_other_hero_buttons(self):
        hero, _closing = self._guide_link_classes()
        self.assertIn("secondary-cta", hero)
        self.assertNotIn("btn-outline-primary", hero)

    def test_the_closing_guide_link_matches_its_green_neighbours(self):
        _hero, closing = self._guide_link_classes()
        self.assertIn("btn-outline-green", closing)
        self.assertIn("fhi-btn-lg", closing)
        self.assertNotIn("btn-outline-primary", closing)
        self.assertNotIn("btn-lg", closing)
