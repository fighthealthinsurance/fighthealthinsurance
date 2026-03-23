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

        # Check hero and persona sections
        self.assertContains(result, "Join the Fight Against Unfair Denials")
        self.assertContains(result, "Share the Tool")
        self.assertContains(result, "Contribute to Open Source")
        self.assertContains(
            result, "Help Someone Fight a Denial They Couldn"
        )
        self.assertContains(result, "Connect Us With People Who Need This")

        # Check persona section IDs
        self.assertContains(result, 'id="help-patients"')
        self.assertContains(result, 'id="developers"')
        self.assertContains(result, 'id="providers"')
        self.assertContains(result, 'id="connectors"')

        # Check for links to key components
        self.assertContains(result, "chooser")
        self.assertContains(result, "github.com/orgs/fighthealthinsurance")
        self.assertContains(result, "buy.stripe.com")

    def test_how_to_help_template_used(self):
        """Test that the correct template is used."""
        url = reverse("how-to-help")
        result = self.client.get(url)
        self.assertTemplateUsed(result, "how_to_help.html")
        self.assertTemplateUsed(result, "base.html")
