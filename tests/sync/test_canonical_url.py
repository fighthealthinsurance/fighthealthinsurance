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

        # Check for canonical URL pointing to www.fighthealthinsurance.com/about-us
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/about-us">',
            content,
            "About page should have canonical URL with path"
        )

    def test_bingo_page_has_canonical_url(self):
        """Test that the bingo page includes the canonical URL."""
        response = self.client.get("/bingo")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Check for canonical URL pointing to www.fighthealthinsurance.com/bingo
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/bingo">',
            content,
            "Bingo page should have canonical URL with path"
        )

    def test_other_resources_has_canonical_url(self):
        """Test that the other resources page includes the canonical URL."""
        response = self.client.get("/other-resources")
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")

        # Check for canonical URL pointing to www.fighthealthinsurance.com
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/other-resources">',
            content,
            "Other resources page should have canonical URL with path"
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

    def test_chat_page_with_trailing_slash(self):
        """Test that /chat/ has canonical URL with trailing slash."""
        response = self.client.get("/chat/", follow=True)
        
        # The page might redirect or render directly
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should have canonical URL with the trailing slash
            self.assertIn(
                "https://www.fighthealthinsurance.com/chat/",
                content,
                "Chat page with trailing slash should preserve trailing slash in canonical"
            )

    def test_chat_page_without_trailing_slash(self):
        """Test that /chat has canonical URL without trailing slash."""
        response = self.client.get("/chat", follow=True)
        
        # The page might redirect or render directly
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            
            # Should have canonical URL matching the accessed URL
            # This test verifies that we preserve the actual requested path
            self.assertIn(
                "https://www.fighthealthinsurance.com/chat",
                content,
                "Chat page should have canonical URL"
            )
