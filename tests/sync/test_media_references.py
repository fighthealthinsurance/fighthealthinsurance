"""Tests for the Media References (press) page and its underlying data."""

from django.test import Client, TestCase
from django.urls import reverse
from django.utils.html import escape

from fighthealthinsurance.media_references import (
    MEDIA_REFERENCES,
    SOCIAL_MEDIA_REFERENCES,
)
from fighthealthinsurance.views import STATIC_ISH_PAGE_CACHE_SECONDS

ALL_REFERENCES = MEDIA_REFERENCES + SOCIAL_MEDIA_REFERENCES


class TestMediaReferencesPage(TestCase):
    """The Media References page renders every reference and is cache-friendly."""

    def setUp(self):
        self.client = Client()

    def test_media_references_page_loads(self):
        response = self.client.get(reverse("media-references"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "media_references.html")
        self.assertTemplateUsed(response, "base.html")

    def test_media_references_page_is_cached_like_other_static_ish_pages(self):
        response = self.client.get(reverse("media-references"))
        cache_control = response["Cache-Control"]
        self.assertIn("public", cache_control)
        self.assertIn(f"max-age={STATIC_ISH_PAGE_CACHE_SECONDS}", cache_control)

    def test_media_references_page_lists_every_reference(self):
        response = self.client.get(reverse("media-references"))
        content = response.content.decode("utf-8")
        # Both the press grid and the social grid should render every entry.
        for ref in ALL_REFERENCES:
            # The template auto-escapes, so compare against escaped values
            # (e.g. "&" -> "&amp;").
            self.assertIn(escape(ref["outlet"]), content)
            self.assertIn(escape(ref["title"]), content)
            self.assertIn(ref["url"], content)

    def test_media_references_page_renders_social_section(self):
        response = self.client.get(reverse("media-references"))
        self.assertContains(response, "On Social Media")

    def test_media_references_page_links_to_internal_pbs_page(self):
        # PBS is the one reference with an on-site page; make sure the link renders.
        response = self.client.get(reverse("media-references"))
        self.assertContains(response, reverse("pbs-newshour"))


class TestHomePageLinksToMediaReferences(TestCase):
    """The home page "featured in ..." affordances point at the press page."""

    def test_home_page_links_to_media_references(self):
        response = self.client.get(reverse("root"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("media-references"))


class TestMediaReferencesData(TestCase):
    """Guard the shape of the reference data used to render the page."""

    def test_each_reference_has_required_fields(self):
        for ref in ALL_REFERENCES:
            for key in ("outlet", "kind", "cta", "title", "url", "description"):
                self.assertTrue(
                    ref.get(key), f"Reference {ref.get('outlet')!r} missing {key!r}"
                )

    def test_reference_urls_are_https(self):
        for ref in ALL_REFERENCES:
            self.assertTrue(
                ref["url"].startswith("https://"),
                f"Reference {ref['outlet']!r} URL is not https: {ref['url']}",
            )

    def test_internal_url_names_resolve(self):
        for ref in ALL_REFERENCES:
            internal = ref.get("internal_url_name")
            if internal:
                # Raises NoReverseMatch if the name isn't a real route.
                reverse(internal)
