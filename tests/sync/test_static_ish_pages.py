"""Tests for StaticIshView, the cached marketing/content page base class."""

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import views
from fighthealthinsurance.views import STATIC_ISH_PAGE_CACHE_SECONDS, StaticIshView


class TestStaticIshViewCaching(TestCase):
    """StaticIshView subclasses are cached whole with public cache headers."""

    def setUp(self):
        self.client = Client()

    def test_static_ish_page_sets_public_cache_headers(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        cache_control = response["Cache-Control"]
        self.assertIn("public", cache_control)
        self.assertIn(f"max-age={STATIC_ISH_PAGE_CACHE_SECONDS}", cache_control)

    def test_landing_page_uses_the_default_timeout(self):
        response = self.client.get(reverse("root"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f"max-age={STATIC_ISH_PAGE_CACHE_SECONDS}", response["Cache-Control"]
        )

    def test_default_timeout_is_thirty_minutes(self):
        # The TTL bounds how stale a cached-in SiteBanner can go on these
        # pages (both in the page cache and at the CDN via max-age).
        self.assertEqual(STATIC_ISH_PAGE_CACHE_SECONDS, 60 * 30)
        self.assertEqual(
            StaticIshView.cache_timeout_seconds, STATIC_ISH_PAGE_CACHE_SECONDS
        )

    def test_microsite_directory_keeps_its_longer_timeout(self):
        # Crawler-oriented directory deliberately trades banner freshness for
        # a longer cache; the subclass override must survive refactors.
        self.assertEqual(
            views.MicrositeDirectoryView.cache_timeout_seconds, 60 * 60 * 24
        )
        response = self.client.get(reverse("microsite_directory"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age=86400", response["Cache-Control"])
