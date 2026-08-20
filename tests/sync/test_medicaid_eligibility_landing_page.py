"""Tests for the Medicaid eligibility landing page.

The page is staged behind MEDICAID_ELIGIBILITY_PAGE_ENABLED (on in Dev/Test,
off in production) so the URL stays invisible while the page is iterated on.
"""

from django.test import Client, TestCase, override_settings
from django.urls import reverse


class TestMedicaidEligibilityLandingPage(TestCase):
    """The /medicaid-eligibility landing page renders and routes users."""

    def setUp(self):
        self.client = Client()
        self.response = self.client.get(reverse("medicaid-eligibility"))

    def test_page_renders(self):
        self.assertEqual(self.response.status_code, 200)

    def test_page_links_to_the_chat_eligibility_flow(self):
        self.assertContains(self.response, reverse("chat"))

    def test_page_links_to_work_requirements_faq(self):
        self.assertContains(self.response, reverse("medicaid-faq"))

    def test_page_links_to_state_resources(self):
        self.assertContains(self.response, reverse("state_help_index"))

    def test_page_explains_estimate_not_determination(self):
        # The page must not present the AI check as an official determination.
        self.assertContains(self.response, "Estimate, Not a Determination")

    def test_page_shows_experimental_badge_in_hero(self):
        self.assertContains(self.response, "EXPERIMENTAL FEATURE")

    def test_page_has_prominent_experimental_banner(self):
        self.assertContains(
            self.response, "This eligibility check is an experimental feature"
        )

    def test_page_ctas_are_labeled_experimental(self):
        self.assertContains(self.response, "Try the Experimental Eligibility Check")
        self.assertContains(self.response, "Check My Eligibility (Experimental)")

    def test_page_disclaimer_flags_experimental(self):
        self.assertContains(self.response, "EXPERIMENTAL feature")

    def test_page_covers_2026_work_requirements(self):
        self.assertContains(self.response, "80 hours per month")

    def test_page_is_cached_like_other_static_ish_pages(self):
        self.assertIn("public", self.response["Cache-Control"])

    def test_page_is_in_the_static_sitemap_when_enabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertIn("medicaid-eligibility", StaticViewSitemap().items())

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_page_is_hidden_from_the_sitemap_when_disabled(self):
        # In production the staging flag is off: the page must not be
        # discoverable via the sitemap (the URL isn't routed there either).
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertNotIn("medicaid-eligibility", StaticViewSitemap().items())
