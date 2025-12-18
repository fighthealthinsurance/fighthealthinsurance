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
        response = self.client.get("/about")
        self.assertEqual(response.status_code, 200)
        
        content = response.content.decode("utf-8")
        
        # Check for canonical URL pointing to www.fighthealthinsurance.com/about
        self.assertIn(
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/about">',
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
        pages = ["/", "/about", "/bingo", "/other-resources"]
        
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
