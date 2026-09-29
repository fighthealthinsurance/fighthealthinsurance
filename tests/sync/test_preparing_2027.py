"""The yearly coverage guide: the 2027 page, and the old 2026 address."""

from django.test import TestCase
from django.urls import reverse


class Preparing2027Test(TestCase):
    def test_the_guide_renders(self):
        response = self.client.get(reverse("preparing-2027"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prepare for 2027 Insurance Changes")

    def test_the_2026_address_sends_people_to_the_2027_guide(self):
        response = self.client.get("/preparing-for-2026")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("preparing-2027"))

    def test_the_guide_names_its_sources(self):
        html = self.client.get(reverse("preparing-2027")).content.decode()
        for source in ("healthcare.gov/quick-guide/dates-and-deadlines", "rp-26-24.pdf", "2027-announcement.pdf"):
            self.assertIn(source, html)
