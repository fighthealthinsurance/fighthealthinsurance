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
