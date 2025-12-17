from django.test import TestCase
from django.urls import reverse


class TestHowToHelpPage(TestCase):
    """Test the How to Help page functionality."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_how_to_help_url_loads(self):
        """Test that the how-to-help URL loads successfully."""
        url = reverse("how-to-help")
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200)

    def test_how_to_help_url_by_path(self):
        """Test that the how-to-help URL loads successfully via direct path."""
        result = self.client.get("/how-to-help")
        self.assertEqual(result.status_code, 200)

    def test_how_to_help_content(self):
        """Test that the how-to-help page contains expected content."""
        url = reverse("how-to-help")
        result = self.client.get(url)

        # Check for key sections
        self.assertContains(result, "How You Can Help")
        self.assertContains(result, "Share With Your Network")
        self.assertContains(result, "Help Us Learn & Improve")
        self.assertContains(result, "Contribute on GitHub")
        self.assertContains(result, "Support Our Development")
        
        # Check for links to key components
        self.assertContains(result, "chooser")  # Link to chooser interface
        self.assertContains(result, "github.com/orgs/fighthealthinsurance")  # GitHub link
        self.assertContains(result, "buy.stripe.com")  # Donation link

    def test_how_to_help_template_used(self):
        """Test that the correct template is used."""
        url = reverse("how-to-help")
        result = self.client.get(url)
        self.assertTemplateUsed(result, "how_to_help.html")
        self.assertTemplateUsed(result, "base.html")
