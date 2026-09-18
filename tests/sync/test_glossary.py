"""
Tests for the health-insurance & appeals glossary.

Covers:
1. The glossary index renders and lists terms.
2. A sample term detail page renders with its definition and related links.
3. An unknown slug returns a 404.
4. Data integrity: every related-term link resolves to a real term, there
   are no duplicate slugs, and the term count is within the intended range.
"""

import re

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
    get_terms_sorted,
    validate_glossary,
)

JSON_LD = re.compile(r'<script type="application/ld\+json">.*?</script>', re.S)


def visible_html(response) -> str:
    """The page with its structured data removed.

    Several assertions here passed on the JSON-LD alone: the full definition,
    every term URL and the index URL all appear in it, so a test could say
    "the definition is on the page" while the visible paragraph was gone.
    """
    return JSON_LD.sub("", response.content.decode())


class GlossaryDataIntegrityTest(TestCase):
    """Tests that the glossary data is internally consistent."""

    def test_validate_glossary_passes(self):
        """The shipped glossary data passes validation without raising."""
        # Should not raise.
        validate_glossary()

    def test_term_count_within_expected_range(self):
        """The glossary holds between 40 and 65 terms."""
        self.assertGreaterEqual(len(GLOSSARY_TERMS), 40)
        self.assertLessEqual(len(GLOSSARY_TERMS), 65)

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
        self.assertIn(
            "independent",
            visible_html(response),
            "the definition is only in the structured data, not on the page",
        )

    def test_term_shows_related_links(self):
        """A term page links to each of its related terms."""
        term = get_term("prior-authorization")
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "prior-authorization"})
        )
        self.assertEqual(response.status_code, 200)
        visible = visible_html(response)
        for related in get_related_terms(term):
            url = reverse("glossary_term", kwargs={"slug": related.slug})
            self.assertIn(
                'href="%s"' % url,
                visible,
                "%s is in the structured data but not a link on the page" % url,
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
        # The index's own entry, not merely a term URL that starts with it:
        # every term URL contains "/glossary/", so the looser assertion
        # passed with the index dropped from the sitemap entirely.
        self.assertIn("/glossary/</loc>", content)
        self.assertIn("/glossary/prior-authorization/", content)


class TheGlossaryIsReachableTest(TestCase):
    """59 pages nobody can click are 59 pages nobody reads.

    Everything #903 added was an orphan: registered in the sitemap, linked
    from no template on the site. A search engine would find them; a person
    on the site would not.
    """

    def test_a_page_on_the_site_links_to_it(self):
        from django.template.loader import render_to_string

        rendered = render_to_string("other_resources.html", {})

        self.assertIn(reverse("glossary_index"), rendered)

    def test_the_index_links_out_to_the_terms(self):
        response = self.client.get(reverse("glossary_index"))

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            reverse("glossary_term", kwargs={"slug": "prior-authorization"}), body
        )

    def test_a_term_page_links_back_to_the_index(self):
        response = self.client.get(
            reverse("glossary_term", kwargs={"slug": "prior-authorization"})
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(reverse("glossary_index"), response.content.decode())

    def test_the_pages_are_cached_like_the_site_s_other_content_pages(self):
        """On StaticIshView, so they follow the site's caching rules rather
        than carrying a second copy of them."""
        from fighthealthinsurance.views import (
            GlossaryIndexView,
            GlossaryView,
            StaticIshView,
        )

        self.assertTrue(issubclass(GlossaryIndexView, StaticIshView))
        self.assertTrue(issubclass(GlossaryView, StaticIshView))


class TheContractTheGlossaryClaimsTest(TestCase):
    """Cardinality, asserted exactly, so a regression is visible.

    The earlier version accepted a range, so a glossary that lost nineteen
    terms still passed, and permitted a term with no cross-links at all.
    """

    def test_there_are_fifty_nine_terms(self):
        self.assertEqual(len(get_terms_sorted()), 59)

    def test_every_term_carries_at_least_three_cross_links(self):
        thin = {
            term.slug: len(term.related)
            for term in get_terms_sorted()
            if len(term.related) < 3
        }
        self.assertEqual(thin, {}, "these terms have fewer than three related")

    def test_every_term_has_a_short_form_and_a_definition(self):
        empty = [
            term.slug
            for term in get_terms_sorted()
            if not term.short.strip() or not term.definition.strip()
        ]
        self.assertEqual(empty, [])


class TheDeadlineItPrintsTest(TestCase):
    """A number a patient counts days against.

    The definition said Medicare Advantage and Part D "allow 60 days" without
    saying from when. The statute runs 60 days from receipt and Medicare
    presumes receipt five days after the notice date, so somebody counting
    from the date printed on their letter would think they were two days late
    when they had three days left.
    """

    def test_it_says_what_the_sixty_days_run_from(self):
        definition = get_term("internal-appeal").definition

        self.assertIn("from when you received the notice", definition)
        self.assertIn("65 days", definition)

    def test_it_still_tells_them_to_check_their_own_letter(self):
        self.assertIn(
            "check the deadline", get_term("internal-appeal").definition.lower()
        )
