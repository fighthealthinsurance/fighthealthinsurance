"""
Tests for the health-insurance & appeals glossary.

Covers:
1. The glossary index renders and lists terms.
2. A sample term detail page renders with its definition and related links.
3. An unknown slug returns a 404.
4. Data integrity: every related-term link resolves to a real term, there
   are no duplicate slugs, and the term count is within the intended range.
"""

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance.glossary import (
    GLOSSARY_TERMS,
    GlossaryValidationError,
    get_all_terms,
    get_glossary_slugs,
    get_related_terms,
    get_term,
    get_terms_grouped_by_letter,
    validate_glossary,
)


class GlossaryDataIntegrityTest(TestCase):
    """Tests that the glossary data is internally consistent."""

    def test_validate_glossary_passes(self):
        """The shipped glossary data passes validation without raising."""
        # Should not raise.
        validate_glossary()

    def test_term_count_within_expected_range(self):
        """The glossary holds between 40 and 60 terms."""
        self.assertGreaterEqual(len(GLOSSARY_TERMS), 40)
        self.assertLessEqual(len(GLOSSARY_TERMS), 60)

    def test_no_duplicate_slugs(self):
        """Every glossary slug is unique."""
        slugs = get_glossary_slugs()
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_all_related_links_resolve(self):
        """Every related-term slug points at a real glossary entry."""
        valid_slugs = set(get_glossary_slugs())
        for term in get_all_terms():
            for related_slug in term.related:
                self.assertIn(
                    related_slug,
                    valid_slugs,
                    msg=f"Term '{term.slug}' links to unknown term '{related_slug}'",
                )

    def test_no_term_relates_to_itself(self):
        """No term lists itself as a related term."""
        for term in get_all_terms():
            self.assertNotIn(term.slug, term.related)

    def test_get_related_terms_returns_term_objects(self):
        """get_related_terms resolves slugs to GlossaryTerm objects."""
        prior_auth = get_term("prior-authorization")
        self.assertIsNotNone(prior_auth)
        related = get_related_terms(prior_auth)
        self.assertEqual(len(related), len(prior_auth.related))
        self.assertEqual([t.slug for t in related], list(prior_auth.related))

    def test_every_term_has_required_fields(self):
        """Every term has a non-empty slug, name, short summary, and definition."""
        for term in get_all_terms():
            self.assertTrue(term.slug)
            self.assertTrue(term.term)
            self.assertTrue(term.short)
            self.assertTrue(term.definition)

    def test_validate_glossary_detects_dangling_link(self):
        """validate_glossary raises when a related slug does not resolve."""
        from dataclasses import replace

        broken = get_term("formulary")
        broken = replace(broken, related=("does-not-exist",))
        with self.assertRaises(GlossaryValidationError):
            validate_glossary((broken,))

    def test_grouping_covers_every_term(self):
        """Grouping terms A-Z includes every term exactly once."""
        grouped = get_terms_grouped_by_letter()
        grouped_slugs = [t.slug for _, terms in grouped for t in terms]
        self.assertEqual(sorted(grouped_slugs), sorted(get_glossary_slugs()))


class GlossaryIndexViewTest(TestCase):
    """Tests for the glossary index page."""

    def setUp(self):
        self.client = Client()

    def test_index_renders(self):
        """The glossary index returns 200 and uses the index template."""
        response = self.client.get(reverse("glossary_index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "glossary_index.html")

    def test_index_lists_terms(self):
        """The index page shows term names and links to term detail pages."""
        response = self.client.get(reverse("glossary_index"))
        self.assertContains(response, "Prior Authorization")
        self.assertContains(response, "Medical Necessity")
        # Links to individual term pages are present.
        self.assertContains(
            response, reverse("glossary_term", kwargs={"slug": "prior-authorization"})
        )

    def test_index_includes_defined_term_set_json_ld(self):
        """The index emits DefinedTermSet structured data for SEO."""
        response = self.client.get(reverse("glossary_index"))
        self.assertContains(response, "application/ld+json")
        self.assertContains(response, "DefinedTermSet")


class GlossaryTermViewTest(TestCase):
    """Tests for individual glossary term pages."""

    def setUp(self):
        self.client = Client()

    def test_sample_term_renders(self):
        """A known term detail page renders with its definition."""
        term = get_term("external-review")
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "external-review"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "glossary.html")
        self.assertContains(response, term.term)
        self.assertContains(response, "independent")

    def test_term_shows_related_links(self):
        """A term page links to each of its related terms."""
        term = get_term("prior-authorization")
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "prior-authorization"})
        )
        self.assertEqual(response.status_code, 200)
        for related in get_related_terms(term):
            self.assertContains(
                response,
                reverse("glossary_term", kwargs={"slug": related.slug}),
            )

    def test_term_includes_defined_term_and_breadcrumb_json_ld(self):
        """A term page emits DefinedTerm and BreadcrumbList structured data."""
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "medical-necessity"})
        )
        self.assertContains(response, "application/ld+json")
        self.assertContains(response, "DefinedTerm")
        self.assertContains(response, "BreadcrumbList")

    def test_unknown_slug_returns_404(self):
        """An unknown glossary slug returns a 404."""
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "not-a-real-term"})
        )
        self.assertEqual(response.status_code, 404)


class GlossarySitemapTest(TestCase):
    """Tests that glossary URLs appear in the sitemap."""

    def setUp(self):
        self.client = Client()

    def test_sitemap_includes_glossary_urls(self):
        """The sitemap lists the glossary index and at least one term page."""
        response = self.client.get(reverse("django.contrib.sitemaps.views.sitemap"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("/glossary/", content)
        self.assertIn("/glossary/prior-authorization/", content)
