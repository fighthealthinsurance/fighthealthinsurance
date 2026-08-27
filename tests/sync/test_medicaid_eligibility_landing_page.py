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

    def test_hero_cta_is_labeled_experimental(self):
        self.assertIn("Try the Experimental Eligibility Check", self.content)

    def test_final_cta_is_labeled_experimental(self):
        self.assertIn("Check My Eligibility (Experimental)", self.content)

    def test_page_disclaimer_flags_experimental(self):
        self.assertIn("EXPERIMENTAL feature", self.content)

    def test_page_covers_2026_work_requirements(self):
        self.assertIn("80 hours per month", self.content)


class TestMedicaidEligibilityFunnelIntoChat(TestCase):
    """The CTA's microsite_slug must survive the whole path into chat."""

    def test_slug_is_a_valid_attribution_slug(self):
        # The websocket consumer and denial form validate microsite_slug
        # before persisting it; the landing slug is attribution-only (no
        # Microsite entry), so it needs the explicit allowlist -- otherwise
        # the whole funnel's attribution was nulled with a warning per frame.
        from fighthealthinsurance.microsites import is_valid_attribution_slug

        self.assertTrue(is_valid_attribution_slug("medicaid-eligibility"))
        self.assertFalse(is_valid_attribution_slug("not-a-real-slug"))

    def test_non_string_slugs_are_rejected_not_raised_on(self):
        """The websocket hands this through from raw client JSON.

        The membership test raises TypeError on an unhashable value, which
        answered a frame carrying {"microsite_slug": {}} with an internal
        error instead of ignoring the slug like any other invalid one.
        """
        from fighthealthinsurance.microsites import is_valid_attribution_slug

        for value in ({"x": 1}, ["medicaid-eligibility"], 12, True, None):
            with self.subTest(value=value):
                self.assertFalse(is_valid_attribution_slug(value))

    def test_consent_redirect_preserves_funnel_params(self):
        # First-time visitors (no consent yet) are redirected to the consent
        # form; a bare redirect dropped the query string, so the consent
        # form's hidden fields were empty and the post-consent chat lost the
        # eligibility kickoff.
        response = Client().get(
            reverse("chat"), {"microsite_slug": "medicaid-eligibility"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("chat_consent"), response["Location"])
        self.assertIn("microsite_slug=medicaid-eligibility", response["Location"])


class TestMedicaidEligibilityPageStaging(TestCase):
    """The staging flag controls both routing and discoverability."""

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_page_serves_404_when_disabled(self):
        # Production runs with the flag off: the URL must be invisible.
        response = Client().get(reverse("medicaid-eligibility"))
        self.assertEqual(response.status_code, 404)

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=True)
    def test_page_is_in_the_static_sitemap_when_enabled(self):
        # Pin the precondition the name states rather than leaning on the
        # ambient Dev/Test default, which will change when the flag is
        # promoted or retired.
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertIn("medicaid-eligibility", StaticViewSitemap().items())

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_options_does_not_reveal_the_route_when_disabled(self):
        # Review regression: the flag lived in get(), but View.options()
        # answers 200 without dispatching there, leaving the staged route
        # enumerable while it was supposed to be invisible.
        response = Client().options(reverse("medicaid-eligibility"))
        self.assertEqual(response.status_code, 404)

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_page_is_hidden_from_the_sitemap_when_disabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertNotIn("medicaid-eligibility", StaticViewSitemap().items())
