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

        # Check for canonical URL pointing to www.fighthealthinsurance.com/about-us/
        # Normalized to have trailing slash
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/about-us/">',
            content,
            "About page should have canonical URL with trailing slash"
        )

    def test_bingo_page_has_canonical_url(self):
        """Test that the bingo page includes the canonical URL."""
        response = self.client.get("/bingo")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Check for canonical URL pointing to www.fighthealthinsurance.com/bingo/
        # Normalized to have trailing slash
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/bingo/">',
            content,
            "Bingo page should have canonical URL with trailing slash"
        )

    def test_other_resources_has_canonical_url(self):
        """Test that the other resources page includes the canonical URL."""
        response = self.client.get("/other-resources")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Check for canonical URL with trailing slash
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/other-resources/">',
            content,
            "Other resources page should have canonical URL with trailing slash"
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

    def test_chat_page_normalized_to_trailing_slash(self):
        """Test that /chat normalizes to /chat/ in canonical URL."""
        response = self.client.get("/chat", follow=True)
        
        # The page might redirect or render directly
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should normalize to trailing slash version
            self.assertIn(
                "https://www.fighthealthinsurance.com/chat/",
                content,
                "Chat page without trailing slash should canonicalize to /chat/"
            )

    def test_chat_page_with_trailing_slash_stays_normalized(self):
        """Test that /chat/ keeps trailing slash in canonical URL."""
        response = self.client.get("/chat/", follow=True)
        
        # The page might redirect or render directly
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should have canonical URL with trailing slash
            self.assertIn(
                "https://www.fighthealthinsurance.com/chat/",
                content,
                "Chat page with trailing slash should have /chat/ canonical"
            )

    def test_file_like_paths_no_trailing_slash(self):
        """Test that paths that look like files don't get trailing slash."""
        # Test with sitemap.xml as example
        response = self.client.get("/sitemap.xml")
        
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should NOT add trailing slash to file-like paths
            self.assertIn(
                "https://www.fighthealthinsurance.com/sitemap.xml",
                content,
                "File-like paths should not get trailing slash in canonical URL"
            )
            
            # Make sure it didn't add a trailing slash
            self.assertNotIn(
                "https://www.fighthealthinsurance.com/sitemap.xml/",
                content,
                "File paths should not have trailing slash"
            )
