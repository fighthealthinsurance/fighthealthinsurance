"""The yearly coverage guide lives at one address with no year in it."""

from django.test import TestCase
from django.urls import reverse


class CoverageChangesTest(TestCase):
    def test_the_guide_renders(self):
        response = self.client.get(reverse("coverage-changes"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prepare for 2027 Insurance Changes")

    def test_the_old_yearly_address_sends_people_to_the_guide(self):
        response = self.client.get("/preparing-for-2026")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("coverage-changes"))

    def test_the_address_has_no_year_in_it(self):
        # So links and search ranking carry over when the content moves on.
        import re

        self.assertIsNone(re.search(r"20\d\d", reverse("coverage-changes")))

    def test_the_guide_names_its_sources(self):
        html = self.client.get(reverse("coverage-changes")).content.decode()
        for source in ("healthcare.gov/quick-guide/dates-and-deadlines", "rp-26-24.pdf", "2027-announcement.pdf"):
            self.assertIn(source, html)

    def test_the_resources_menu_links_to_the_guide(self):
        html = self.client.get(reverse("root")).content.decode()
        self.assertIn(f'href="{reverse("coverage-changes")}"', html)
