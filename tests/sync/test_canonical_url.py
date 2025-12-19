"""Test canonical URL meta tags are rendered correctly on all pages."""

from django.test import TestCase, Client


class CanonicalUrlTests(TestCase):
    """Test that canonical URLs are correctly rendered on all pages."""

    def setUp(self):
        self.client = Client()

    def test_homepage_has_canonical_url(self):
        """Test that the homepage includes the canonical URL."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Check for canonical URL pointing to www.fighthealthinsurance.com
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/">',
            content,
            "Homepage should have canonical URL"
        )

    def test_about_page_has_canonical_url(self):
        """Test that the about page includes the canonical URL."""
        response = self.client.get("/about-us")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Canonical URL should match the resolved path
        self.assertIn(
            "https://www.fighthealthinsurance.com/about-us",
            content,
            "About page should have canonical URL"
        )

    def test_bingo_page_has_canonical_url(self):
        """Test that the bingo page includes the canonical URL."""
        response = self.client.get("/bingo")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Canonical URL should match the resolved path
        self.assertIn(
            "https://www.fighthealthinsurance.com/bingo",
            content,
            "Bingo page should have canonical URL"
        )

    def test_other_resources_has_canonical_url(self):
        """Test that the other resources page includes the canonical URL."""
        response = self.client.get("/other-resources")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Canonical URL should match the resolved path
        self.assertIn(
            "https://www.fighthealthinsurance.com/other-resources",
            content,
            "Other resources page should have canonical URL"
        )

    def test_canonical_url_uses_www_subdomain(self):
        """Test that canonical URLs always use www subdomain."""
        pages = ["/", "/about-us", "/bingo", "/other-resources"]

        for page in pages:
            response = self.client.get(page)
            content = response.content.decode("utf-8")

            # Ensure canonical URL uses www.fighthealthinsurance.com
            self.assertIn(
                "https://www.fighthealthinsurance.com",
                content,
                f"Page {page} should have canonical URL with www subdomain"
            )

            # Ensure it doesn't use non-www version
            self.assertNotIn(
                '<link rel="canonical" href="https://fighthealthinsurance.com',
                content,
                f"Page {page} should not have canonical URL without www"
            )

    def test_canonical_url_preserves_resolved_path(self):
        """Test that canonical URL uses the path as resolved by Django."""
        # Test with a route that might be accessed with or without trailing slash
        # The canonical URL should match whichever version Django resolves to
        response = self.client.get("/chat", follow=True)
        
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # The canonical URL should match the final resolved path
            # (after any redirects Django might perform)
            final_path = response.request['PATH_INFO']
            expected_canonical = f"https://www.fighthealthinsurance.com{final_path}"
            
            self.assertIn(
                expected_canonical,
                content,
                f"Canonical URL should match resolved path: {expected_canonical}"
            )

    def test_sitemap_canonical_url(self):
        """Test that sitemap.xml has correct canonical URL."""
        response = self.client.get("/sitemap.xml")
        
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should have canonical URL matching the resolved path
            self.assertIn(
                "https://www.fighthealthinsurance.com/sitemap.xml",
                content,
                "Sitemap should have canonical URL"
            )
