"""Test the sitemap functionality"""

from django.test import TestCase, Client
from django.urls import reverse


class SitemapTests(TestCase):
    """Test the sitemap views."""

    def setUp(self):
        self.client = Client()

    def test_sitemap_returns_xml(self):
        """Test that the sitemap endpoint returns XML."""
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml")

    def test_sitemap_contains_static_urls(self):
        """Test that the sitemap contains expected static URLs."""
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")

        # Check for expected static pages
        expected_paths = [
            "/about-us",
            "/faq/",
            "/tos",
            "/privacy_policy",
            "/contact",
            "/blog/",
        ]
        for path in expected_paths:
            self.assertIn(path, content)

    def test_sitemap_excludes_rest_api(self):
        """Test that the sitemap does not contain REST API URLs."""
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")

        # REST API paths should not be in the sitemap
        self.assertNotIn("/ziggy/rest/", content)
        self.assertNotIn("/timbit/admin/", content)

    def test_sitemap_uses_request_host(self):
        """Test that the sitemap uses the request's host header for domain."""
        # Test with localhost
        response = self.client.get("/sitemap.xml", HTTP_HOST="localhost:8000")
        content = response.content.decode("utf-8")
        self.assertIn("http://localhost:8000/", content)

        # Test with production domain
        response = self.client.get(
            "/sitemap.xml", HTTP_HOST="www.fighthealthinsurance.com"
        )
        content = response.content.decode("utf-8")
        self.assertIn("http://www.fighthealthinsurance.com/", content)

    def test_sitemap_has_xmlns(self):
        """Test that the sitemap contains the proper xmlns attribute."""
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")
        # Django's default sitemap.xml template includes the xmlns
        self.assertIn('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"', content)


class StaticViewSitemapTests(TestCase):
    """Test the StaticViewSitemap class."""

    def test_static_sitemap_items(self):
        """Test that static sitemap returns expected items."""
        from fighthealthinsurance.sitemap import StaticViewSitemap

        sitemap = StaticViewSitemap()
        items = sitemap.items()

        expected_items = [
            "root",
            "about",
            "faq",
            "tos",
            "privacy_policy",
            "contact",
            "blog",
        ]
        for expected in expected_items:
            self.assertIn(expected, items)

    def test_static_sitemap_locations(self):
        """Test that static sitemap generates valid URLs."""
        from fighthealthinsurance.sitemap import StaticViewSitemap

        sitemap = StaticViewSitemap()
        for item in sitemap.items():
            location = sitemap.location(item)
            self.assertTrue(location.startswith("/"))


class BlogSitemapTests(TestCase):
    """Test the BlogSitemap class."""

    def test_blog_sitemap_handles_missing_file(self):
        """Test that blog sitemap gracefully handles missing blog_posts.json."""
        from fighthealthinsurance.sitemap import BlogSitemap

        sitemap = BlogSitemap()
        # Should return empty list if file doesn't exist
        items = sitemap.items()
        self.assertIsInstance(items, list)


class MicrositeSitemapTests(TestCase):
    """Test the MicrositeSitemap class and WIP flag handling."""

    def test_microsite_sitemap_excludes_wip_microsites(self):
        """Test that microsites with wip=true are excluded from sitemap."""
        from fighthealthinsurance.sitemap import MicrositeSitemap

        sitemap = MicrositeSitemap()
        items = sitemap.items()

        # Get all microsites to check WIP status
        from fighthealthinsurance.microsites import get_all_microsites

        microsites = get_all_microsites()

        for slug in items:
            # All items in sitemap should NOT have wip=true
            microsite = microsites.get(slug)
            self.assertIsNotNone(microsite, f"Microsite {slug} not found in microsites")
            self.assertFalse(
                getattr(microsite, "wip", False),
                f"WIP microsite {slug} should not be in sitemap",
            )

    def test_microsite_sitemap_includes_non_wip_microsites(self):
        """Test that microsites without wip flag are included in sitemap."""
        from fighthealthinsurance.sitemap import MicrositeSitemap
        from fighthealthinsurance.microsites import get_all_microsites

        sitemap = MicrositeSitemap()
        items = sitemap.items()

        microsites = get_all_microsites()

        # Find at least one non-WIP microsite
        non_wip_microsites = [
            slug
            for slug, microsite in microsites.items()
            if not getattr(microsite, "wip", False)
        ]

        # There should be non-WIP microsites in the sitemap
        self.assertGreater(
            len(non_wip_microsites),
            0,
            "There should be at least one non-WIP microsite",
        )

        # All non-WIP microsites should be in the sitemap
        for slug in non_wip_microsites:
            self.assertIn(
                slug, items, f"Non-WIP microsite {slug} should be in sitemap"
            )

    def test_full_sitemap_excludes_wip_microsites(self):
        """Test that WIP microsites don't appear in the full sitemap XML."""
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")

        # Get all WIP microsites
        from fighthealthinsurance.microsites import get_all_microsites

        microsites = get_all_microsites()
        wip_microsites = [
            slug
            for slug, microsite in microsites.items()
            if getattr(microsite, "wip", False)
        ]

        # WIP microsite URLs should not be in the sitemap
        for slug in wip_microsites:
            self.assertNotIn(
                f"/start/{slug}",
                content,
                f"WIP microsite {slug} should not be in sitemap XML",
            )
