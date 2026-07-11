"""Tests for the Denial Reason Decoder public educational tool."""

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.denial_reason_decoder import (
    AppealAngle,
    DenialReason,
    get_reason,
    get_reasons_map,
    load_reasons,
)


class TestDenialReasonDataIntegrity(TestCase):
    """Ensure the curated data module is well-formed and consistent."""

    def test_load_reasons_returns_non_empty_list(self):
        reasons = load_reasons()
        self.assertIsInstance(reasons, list)
        self.assertGreater(len(reasons), 0)

    def test_load_reasons_cached_is_consistent(self):
        """Multiple calls should return the same objects."""
        first = load_reasons()
        second = load_reasons()
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            self.assertEqual(a.slug, b.slug)

    def test_all_reasons_have_unique_slugs(self):
        reasons = load_reasons()
        slugs = [r.slug for r in reasons]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_all_reasons_have_required_fields(self):
        reasons = load_reasons()
        for r in reasons:
            self.assertIsInstance(r.slug, str)
            self.assertGreater(len(r.slug), 0)
            self.assertIsInstance(r.title, str)
            self.assertGreater(len(r.title), 0)
            self.assertIsInstance(r.subtitle, str)
            self.assertGreater(len(r.subtitle), 0)
            self.assertIsInstance(r.meta_description, str)
            self.assertGreater(len(r.meta_description), 0)
            self.assertIsInstance(r.what_it_means, str)
            self.assertGreater(len(r.what_it_means), 50)
            self.assertIsInstance(r.why_insurers_use_it, str)
            self.assertGreater(len(r.why_insurers_use_it), 50)

    def test_all_reasons_have_appeal_angles(self):
        reasons = load_reasons()
        for r in reasons:
            self.assertIsInstance(r.appeal_angles, list)
            self.assertGreater(len(r.appeal_angles), 0)
            for angle in r.appeal_angles:
                self.assertIsInstance(angle, AppealAngle)
                self.assertIsInstance(angle.headline, str)
                self.assertGreater(len(angle.headline), 0)
                self.assertIsInstance(angle.description, str)
                self.assertGreater(len(angle.description), 20)
                self.assertIsInstance(angle.evidence_tips, list)
                self.assertGreater(len(angle.evidence_tips), 0)
                for tip in angle.evidence_tips:
                    self.assertIsInstance(tip, str)
                    self.assertGreater(len(tip), 10)

    def test_all_reasons_have_cta_text(self):
        reasons = load_reasons()
        for r in reasons:
            self.assertIsInstance(r.cta_text, str)
            self.assertGreater(len(r.cta_text), 10)

    def test_all_reasons_have_keywords(self):
        reasons = load_reasons()
        for r in reasons:
            self.assertIsInstance(r.keywords, list)
            self.assertGreater(len(r.keywords), 0)

    def test_get_reason_returns_none_for_unknown_slug(self):
        self.assertIsNone(get_reason("nonexistent-slug-12345"))

    def test_get_reason_returns_denial_reason_for_known_slug(self):
        reason = get_reason("not-medically-necessary")
        self.assertIsNotNone(reason)
        self.assertIsInstance(reason, DenialReason)
        self.assertEqual(reason.slug, "not-medically-necessary")

    def test_get_reasons_map_returns_all_slugs(self):
        reasons = load_reasons()
        reason_map = get_reasons_map()
        self.assertEqual(len(reason_map), len(reasons))
        for r in reasons:
            self.assertIn(r.slug, reason_map)

    def test_reasons_count_matches_expected(self):
        """We expect exactly 9 curated reasons."""
        reasons = load_reasons()
        self.assertEqual(len(reasons), 9)

    def test_jsonld_faq_has_required_properties(self):
        reason = get_reason("step-therapy-fail-first")
        self.assertIsNotNone(reason)
        faq = reason.faq_jsonld()
        self.assertEqual(faq["@context"], "https://schema.org")
        self.assertEqual(faq["@type"], "FAQPage")
        self.assertIn("mainEntity", faq)
        self.assertGreater(len(faq["mainEntity"]), 0)

    def test_jsonld_breadcrumb_has_required_properties(self):
        reason = get_reason("out-of-network")
        self.assertIsNotNone(reason)
        bc = reason.breadcrumb_jsonld()
        self.assertEqual(bc["@context"], "https://schema.org")
        self.assertEqual(bc["@type"], "BreadcrumbList")
        self.assertIn("itemListElement", bc)
        self.assertEqual(len(bc["itemListElement"]), 2)

    def test_jsonld_article_has_required_properties(self):
        reason = get_reason("quantity-limit")
        self.assertIsNotNone(reason)
        article = reason.article_jsonld()
        self.assertEqual(article["@context"], "https://schema.org")
        self.assertEqual(article["@type"], "Article")
        self.assertIn("headline", article)
        self.assertIn("url", article)

    def test_related_reasons_excludes_self(self):
        reasons = load_reasons()
        reason_map = get_reasons_map()
        for r in reasons:
            related = r.related_reasons(reason_map)
            for rel in related:
                self.assertNotEqual(rel.slug, r.slug)

    def test_related_reasons_returns_at_most_three(self):
        reasons = load_reasons()
        reason_map = get_reasons_map()
        for r in reasons:
            related = r.related_reasons(reason_map)
            self.assertLessEqual(len(related), 3)


class TestDenialReasonDecoderViews(TestCase):
    """Integration tests for the views and URL routing."""

    def setUp(self):
        self.client = Client()

    # ── index view ────────────────────────────────────────────────────

    def test_index_url_resolves(self):
        url = reverse("denial_reason_decoder_index")
        self.assertEqual(url, "/tools/denial-reason-decoder/")

    def test_index_returns_200(self):
        url = reverse("denial_reason_decoder_index")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_index_contains_all_reasons(self):
        url = reverse("denial_reason_decoder_index")
        response = self.client.get(url)
        reasons = load_reasons()
        for r in reasons:
            self.assertContains(response, r.title)

    def test_index_contains_title(self):
        url = reverse("denial_reason_decoder_index")
        response = self.client.get(url)
        self.assertContains(response, "Denial Reason Decoder")

    def test_index_contains_appeal_link(self):
        url = reverse("denial_reason_decoder_index")
        response = self.client.get(url)
        self.assertContains(response, reverse("scan"))

    # ── detail views ──────────────────────────────────────────────────

    def test_detail_url_resolves_for_known_slug(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "not-medically-necessary"},
        )
        self.assertTrue(url.startswith("/tools/denial-reason-decoder/"))
        self.assertTrue(url.endswith("/not-medically-necessary/"))

    def test_detail_returns_200_for_known_slug(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "step-therapy-fail-first"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_contains_reason_content(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "experimental-or-investigational"},
        )
        response = self.client.get(url)
        self.assertContains(response, "Experimental or Investigational")
        # Should contain the "what it means" content
        self.assertContains(response, "experimental")

    def test_detail_contains_appeal_angles(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "not-medically-necessary"},
        )
        response = self.client.get(url)
        self.assertContains(response, "Appeal Strategies")

    def test_detail_contains_jsonld(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "prior-authorization-required-not-met"},
        )
        response = self.client.get(url)
        self.assertContains(response, "application/ld+json")
        self.assertContains(response, "FAQPage")

    def test_detail_contains_breadcrumb(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "non-formulary-formulary-exclusion"},
        )
        response = self.client.get(url)
        self.assertContains(response, "breadcrumb")
        self.assertContains(response, "Denial Reason Decoder")

    def test_detail_contains_cta(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "coverage-terminated-no-longer-covered"},
        )
        response = self.client.get(url)
        self.assertContains(response, reverse("scan"))

    def test_detail_contains_related_reasons(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "out-of-network"},
        )
        response = self.client.get(url)
        self.assertContains(response, "Explore Other Denial Reasons")

    def test_detail_disclaimer_present(self):
        url = reverse(
            "denial_reason_decoder_detail",
            kwargs={"slug": "non-medical-switching-forced-switch"},
        )
        response = self.client.get(url)
        self.assertContains(response, "Disclaimer")

    def test_all_known_slugs_return_200(self):
        """Every reason in the data module must render successfully."""
        reasons = load_reasons()
        for r in reasons:
            url = reverse(
                "denial_reason_decoder_detail", kwargs={"slug": r.slug}
            )
            response = self.client.get(url)
            self.assertEqual(
                response.status_code,
                200,
                f"Expected 200 for slug '{r.slug}', got {response.status_code}",
            )

    # ── 404 for unknown slug ──────────────────────────────────────────

    def test_unknown_slug_returns_404(self):
        url = "/tools/denial-reason-decoder/this-does-not-exist/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_almost_valid_slug_returns_404(self):
        url = "/tools/denial-reason-decoder/not-medically-necesary/"  # typo
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class TestDenialReasonDecoderSitemap(TestCase):
    """Ensure the sitemap class generates valid URLs for all reasons."""

    def test_sitemap_includes_all_slugs(self):
        from fighthealthinsurance.sitemap import DenialReasonDecoderSitemap

        sitemap = DenialReasonDecoderSitemap()
        items = sitemap.items()
        self.assertIn("index", items)
        reasons = load_reasons()
        for r in reasons:
            self.assertIn(r.slug, items)

    def test_sitemap_location_index(self):
        from fighthealthinsurance.sitemap import DenialReasonDecoderSitemap

        sitemap = DenialReasonDecoderSitemap()
        self.assertEqual(
            sitemap.location("index"),
            reverse("denial_reason_decoder_index"),
        )

    def test_sitemap_location_detail(self):
        from fighthealthinsurance.sitemap import DenialReasonDecoderSitemap

        sitemap = DenialReasonDecoderSitemap()
        url = sitemap.location("not-medically-necessary")
        self.assertTrue(url.startswith("/tools/denial-reason-decoder/"))
