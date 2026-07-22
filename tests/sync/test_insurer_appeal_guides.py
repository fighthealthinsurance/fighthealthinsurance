"""
Tests for the per-insurer appeal guide feature.

Covers:
    - Data integrity (validation, unique slugs/aliases, resolvable cross-links).
    - The index view lists every insurer.
    - Each insurer page renders.
    - An unknown slug returns 404.
    - Sitemap and SEO (JSON-LD, canonical) integration.
"""

import json

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance.insurer_appeal_guides import (
    FAQItem,
    InsurerGuide,
    InsurerGuideValidationError,
    LAST_REVIEWED,
    MAX_FAQS,
    MIN_FAQS,
    VALID_PLAN_TYPES,
    get_all_insurer_guides,
    get_insurer_guide,
    get_insurer_slugs,
    get_insurers_sorted_by_name,
    get_related_guides,
    validate_insurer_guides,
)


def _make_guide(slug: str, **overrides) -> InsurerGuide:
    """Build a minimally valid InsurerGuide for validation tests."""
    faqs = tuple(FAQItem(f"Q{i}?", f"A{i}.") for i in range(MIN_FAQS))
    defaults = dict(
        name=slug.title(),
        slug=slug,
        aliases=(f"{slug}-alias",),
        plan_types=("Commercial",),
        summary="Summary.",
        why_denials="Why denials.",
        overview="Overview.",
        faqs=faqs,
        related_slugs=(),
    )
    defaults.update(overrides)
    return InsurerGuide(**defaults)  # type: ignore[arg-type]


class InsurerGuideDataTest(TestCase):
    """Tests for the insurer guide data set and validation."""

    def test_default_data_set_is_valid(self):
        """The shipped insurer guide data set passes validation."""
        # Should not raise.
        validate_insurer_guides()

    def test_expected_insurers_present(self):
        """All of the required insurers/PBMs are covered."""
        slugs = set(get_insurer_slugs())
        expected = {
            "unitedhealthcare",
            "aetna",
            "cigna",
            "anthem-elevance",
            "blue-cross-blue-shield",
            "kaiser-permanente",
            "humana",
            "centene",
            "molina",
            "cvs-caremark",
            "express-scripts",
            "optumrx",
        }
        self.assertTrue(expected.issubset(slugs))

    def test_slugs_are_unique(self):
        """No two guides share a slug."""
        slugs = get_insurer_slugs()
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_aliases_are_unique_across_guides(self):
        """Aliases are globally unique (case-insensitive)."""
        seen: set[str] = set()
        for guide in get_all_insurer_guides().values():
            for alias in guide.aliases:
                key = alias.lower()
                self.assertNotIn(key, seen, f"Duplicate alias: {alias}")
                seen.add(key)

    def test_plan_types_use_controlled_vocabulary(self):
        """Every plan type comes from the allowed set."""
        for guide in get_all_insurer_guides().values():
            for plan_type in guide.plan_types:
                self.assertIn(plan_type, VALID_PLAN_TYPES)

    def test_faq_counts_within_bounds(self):
        """Each guide has between MIN_FAQS and MAX_FAQS FAQ items."""
        for guide in get_all_insurer_guides().values():
            self.assertGreaterEqual(len(guide.faqs), MIN_FAQS)
            self.assertLessEqual(len(guide.faqs), MAX_FAQS)

    def test_cross_links_resolve(self):
        """Every related slug resolves to an existing guide and is not self."""
        slugs = set(get_insurer_slugs())
        for guide in get_all_insurer_guides().values():
            for related in guide.related_slugs:
                self.assertIn(related, slugs)
                self.assertNotEqual(related, guide.slug)

    def test_get_related_guides_returns_guides(self):
        """get_related_guides resolves slugs to InsurerGuide objects."""
        for guide in get_all_insurer_guides().values():
            related = get_related_guides(guide.slug)
            self.assertEqual(len(related), len(guide.related_slugs))
            for r in related:
                self.assertIsInstance(r, InsurerGuide)

    def test_get_related_guides_unknown_slug(self):
        """get_related_guides returns an empty list for an unknown slug."""
        self.assertEqual(get_related_guides("not-a-real-insurer"), [])

    def test_get_insurer_guide_unknown_returns_none(self):
        """get_insurer_guide returns None for an unknown slug."""
        self.assertIsNone(get_insurer_guide("not-a-real-insurer"))

    def test_sorted_by_name(self):
        """get_insurers_sorted_by_name returns guides in case-insensitive name order."""
        guides = get_insurers_sorted_by_name()
        names = [g.name for g in guides]
        # Case-insensitive: plain sorted(names) would accept "CVS Caremark"
        # before "Centene" (code-point order), which is what we're guarding
        # against here.
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertLess(names.index("Centene"), names.index("CVS Caremark"))

    # --- Validation error cases ---

    def test_validate_rejects_duplicate_slug(self):
        guides = (_make_guide("dup"), _make_guide("dup"))
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_duplicate_alias(self):
        guides = (
            _make_guide("a", aliases=("Shared",)),
            _make_guide("b", aliases=("shared",)),
        )
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_invalid_plan_type(self):
        guides = (_make_guide("a", plan_types=("Bogus",)),)
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_too_few_faqs(self):
        guides = (_make_guide("a", faqs=(FAQItem("Q?", "A."),)),)
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_too_many_faqs(self):
        too_many = tuple(FAQItem(f"Q{i}?", f"A{i}.") for i in range(MAX_FAQS + 1))
        guides = (_make_guide("a", faqs=too_many),)
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_self_link(self):
        guides = (_make_guide("a", related_slugs=("a",)),)
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)

    def test_validate_rejects_unresolvable_link(self):
        guides = (_make_guide("a", related_slugs=("nope",)),)
        with self.assertRaises(InsurerGuideValidationError):
            validate_insurer_guides(guides)


class InsurerAppealGuideIndexViewTest(TestCase):
    """Tests for the insurer appeal guide index page."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("insurer_appeal_guide_index")

    def test_index_loads(self):
        """The index page loads successfully."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_index_uses_correct_template(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "insurer_appeal_guide_index.html")

    def test_index_lists_all_insurers(self):
        """The index lists every insurer by name with a link to its guide."""
        response = self.client.get(self.url)
        content = response.content.decode("utf-8")
        for guide in get_all_insurer_guides().values():
            self.assertIn(guide.name, content)
            self.assertIn(
                reverse("insurer_appeal_guide", kwargs={"slug": guide.slug}),
                content,
            )

    def test_index_has_jsonld(self):
        """The index page includes JSON-LD structured data."""
        response = self.client.get(self.url)
        content = response.content.decode("utf-8")
        self.assertIn("application/ld+json", content)

    def test_index_shows_last_reviewed(self):
        response = self.client.get(self.url)
        self.assertContains(response, LAST_REVIEWED)

    def test_index_has_canonical_url(self):
        """The index page emits a canonical link to its own path."""
        response = self.client.get(self.url)
        self.assertContains(response, 'rel="canonical"')
        self.assertContains(response, "/insurance-appeals/")


class InsurerAppealGuideViewTest(TestCase):
    """Tests for individual insurer appeal guide pages."""

    def setUp(self):
        self.client = Client()

    def test_every_insurer_page_renders(self):
        """Each insurer guide page renders successfully with its H1."""
        for guide in get_all_insurer_guides().values():
            url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"Failed for {guide.slug}")
            self.assertContains(
                response, f"How to Appeal a {guide.name} Insurance Denial"
            )

    def test_uses_correct_template(self):
        guide = get_insurers_sorted_by_name()[0]
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        self.assertTemplateUsed(response, "insurer_appeal_guide.html")

    def test_unknown_slug_returns_404(self):
        """An unknown insurer slug returns 404."""
        url = reverse("insurer_appeal_guide", kwargs={"slug": "not-a-real-insurer"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_page_shows_faqs(self):
        """The page renders the insurer's FAQ questions."""
        guide = get_insurer_guide("unitedhealthcare")
        assert guide is not None
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        for faq in guide.faqs:
            self.assertContains(response, faq.question)

    def test_page_has_faqpage_jsonld(self):
        """The page includes valid FAQPage JSON-LD with all questions."""
        guide = get_insurer_guide("aetna")
        assert guide is not None
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        self.assertIn("application/ld+json", content)
        # Extract the JSON-LD payload and confirm it parses and has FAQPage.
        start = content.index("application/ld+json")
        script_open = content.index(">", start) + 1
        script_close = content.index("</script>", script_open)
        payload = json.loads(content[script_open:script_close])
        types = {block.get("@type") for block in payload}
        self.assertIn("FAQPage", types)
        self.assertIn("BreadcrumbList", types)
        self.assertIn("WebPage", types)

        # The FAQPage mainEntity must mirror the data-module FAQs exactly:
        # same count and the same question texts in order.
        faqpage = next(b for b in payload if b.get("@type") == "FAQPage")
        main_entity = faqpage["mainEntity"]
        self.assertEqual(len(main_entity), len(guide.faqs))
        emitted_questions = [q["name"] for q in main_entity]
        self.assertEqual(emitted_questions, [faq.question for faq in guide.faqs])
        # Answers should round-trip as well.
        emitted_answers = [q["acceptedAnswer"]["text"] for q in main_entity]
        self.assertEqual(emitted_answers, [faq.answer for faq in guide.faqs])

    def test_page_cross_links_to_related_guides(self):
        """The page links to its related insurer guides (the mesh)."""
        guide = get_insurer_guide("unitedhealthcare")
        assert guide is not None
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        for related in get_related_guides(guide.slug):
            self.assertContains(
                response,
                reverse("insurer_appeal_guide", kwargs={"slug": related.slug}),
            )

    def test_page_has_canonical_url(self):
        """The page emits a canonical link."""
        guide = get_insurers_sorted_by_name()[0]
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        self.assertContains(response, 'rel="canonical"')

    def test_page_has_cta_into_appeal_flow(self):
        """The page links into the existing scan and chat appeal flow."""
        guide = get_insurers_sorted_by_name()[0]
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        self.assertContains(response, reverse("scan"))
        self.assertContains(response, reverse("chat"))

    def test_page_has_disclaimer(self):
        """The page includes the not-legal-advice disclaimer."""
        guide = get_insurers_sorted_by_name()[0]
        url = reverse("insurer_appeal_guide", kwargs={"slug": guide.slug})
        response = self.client.get(url)
        self.assertContains(response, "not legal advice")


class InsurerAppealGuideSitemapTest(TestCase):
    """Tests for insurer appeal guide sitemap integration."""

    def setUp(self):
        self.client = Client()

    def test_sitemap_includes_insurer_guides(self):
        """The sitemap includes the insurer guide index and detail pages."""
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("insurance-appeals", content)
        # Spot-check a couple of specific insurer URLs.
        self.assertIn(
            reverse("insurer_appeal_guide", kwargs={"slug": "unitedhealthcare"}),
            content,
        )
