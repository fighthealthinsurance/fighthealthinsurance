"""Tests for the Medicaid eligibility landing page.

The page is staged behind MEDICAID_ELIGIBILITY_PAGE_ENABLED (on in Dev/Test,
off in production): the route always exists, but the view serves 404 and the
sitemap omits the page until the flag is flipped.
"""

from django.test import Client, TestCase, override_settings
from django.urls import reverse


class TestMedicaidEligibilityLandingPage(TestCase):
    """The /medicaid-eligibility landing page renders and routes users."""

    @classmethod
    def setUpClass(cls):
        # One shared render: the page is read-only and identical for every
        # assertion below, so don't re-render it per test. Keep primitives
        # (not the HttpResponse -- Django's setUpTestData/deepcopy machinery
        # can't copy one).
        super().setUpClass()
        response = Client().get(reverse("medicaid-eligibility"))
        cls.status_code = response.status_code
        cls.content = response.content.decode()

    def test_page_renders(self):
        self.assertEqual(self.status_code, 200)

    def test_page_links_to_the_chat_eligibility_flow(self):
        self.assertIn(reverse("chat"), self.content)

    def test_page_links_to_work_requirements_faq(self):
        self.assertIn(reverse("medicaid-faq"), self.content)

    def test_page_links_to_state_resources(self):
        self.assertIn(reverse("state_help_index"), self.content)

    def test_page_explains_estimate_not_determination(self):
        # The page must not present the AI check as an official determination.
        self.assertIn("Estimate, Not a Determination", self.content)

    def test_page_shows_experimental_badge_in_hero(self):
        self.assertIn("EXPERIMENTAL FEATURE", self.content)

    def test_page_has_prominent_experimental_banner(self):
        self.assertIn(
            "This eligibility check is an experimental feature", self.content
        )

    def test_page_ctas_are_labeled_experimental(self):
        self.assertIn("Try the Experimental Eligibility Check", self.content)
        self.assertIn("Check My Eligibility (Experimental)", self.content)

    def test_page_disclaimer_flags_experimental(self):
        self.assertIn("EXPERIMENTAL feature", self.content)

    def test_page_covers_2026_work_requirements(self):
        self.assertIn("80 hours per month", self.content)


class TestMedicaidEligibilityPageStaging(TestCase):
    """The staging flag controls both routing and discoverability."""

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_page_serves_404_when_disabled(self):
        # Production runs with the flag off: the URL must be invisible.
        response = Client().get(reverse("medicaid-eligibility"))
        self.assertEqual(response.status_code, 404)

    def test_page_is_in_the_static_sitemap_when_enabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertIn("medicaid-eligibility", StaticViewSitemap().items())

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_page_is_hidden_from_the_sitemap_when_disabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertNotIn("medicaid-eligibility", StaticViewSitemap().items())
