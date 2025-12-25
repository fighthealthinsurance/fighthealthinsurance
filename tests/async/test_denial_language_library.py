from django.test import TestCase, Client
from django.urls import reverse


class TestDenialLanguageLibrary(TestCase):
    """Tests for the public denial language library page."""

    def setUp(self):
        self.client = Client()

    def test_denial_language_page_loads(self):
        """Test that the denial language library page loads successfully."""
        response = self.client.get("/denial-language/")
        self.assertEqual(response.status_code, 200)

    def test_denial_language_page_has_title(self):
        """Test that the page has the expected title."""
        response = self.client.get("/denial-language/")
        self.assertContains(response, "What Your Denial Really Means")

    def test_denial_language_page_has_denial_phrases(self):
        """Test that the page displays denial phrases."""
        response = self.client.get("/denial-language/")
        # Check for some common denial phrases
        self.assertContains(response, "Not medically necessary")
        self.assertContains(response, "out-of-network")

    def test_denial_language_page_has_explanations(self):
        """Test that the page includes explanations of denial phrases."""
        response = self.client.get("/denial-language/")
        self.assertContains(response, "What It Really Means")
        self.assertContains(response, "How to Counter This")

    def test_denial_language_page_has_seo_metadata(self):
        """Test that the page includes SEO metadata."""
        response = self.client.get("/denial-language/")
        # Check for meta description
        self.assertContains(response, "Decode your insurance denial")

    def test_denial_language_url_name_resolves(self):
        """Test that the URL name resolves correctly."""
        url = reverse("denial-language-library")
        self.assertEqual(url, "/denial-language/")

    def test_denial_language_context_has_phrases(self):
        """Test that the view provides denial phrases in context."""
        response = self.client.get("/denial-language/")
        self.assertIn("denial_phrases", response.context)
        # Should have multiple phrases
        self.assertGreater(len(response.context["denial_phrases"]), 0)

    def test_denial_language_context_has_total(self):
        """Test that the view provides total phrase count in context."""
        response = self.client.get("/denial-language/")
        self.assertIn("total_phrases", response.context)
        self.assertGreater(response.context["total_phrases"], 0)
