"""The BRB flag: while the site is down on purpose, the landing page tells
why with a 503 and every other page keeps working (brb.py, views.root_view).
"""

import re

from django.contrib.staticfiles import finders
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import brb
from fighthealthinsurance.constants import SITE_BANNER_CANNED_MESSAGES


class TestBrbReason(TestCase):
    """Which story settings.BRB picks."""

    def test_live_and_unset_leave_the_site_up(self):
        for value in ("LIVE", "live", " Live ", "", None):
            with override_settings(BRB=value):
                self.assertIsNone(brb.brb_reason(), value)

    def test_each_story_is_picked_by_its_key(self):
        for key, reason in brb.BRB_REASONS.items():
            with override_settings(BRB=key):
                self.assertIs(brb.brb_reason(), reason)

    def test_the_spelling_of_a_story_is_forgiven(self):
        for value in ("el nino", " El-Nino ", "el_niño", "EL NIÑO", "elnino"):
            with override_settings(BRB=value):
                reason = brb.brb_reason()
                self.assertIsNotNone(reason, value)
                self.assertEqual(reason.key, "EL_NINO", value)

    def test_a_value_naming_no_story_still_takes_the_site_down(self):
        # A typo under pressure fails toward "down", with the original page.
        with override_settings(BRB="EL_NINOOO"):
            reason = brb.brb_reason()
            self.assertIsNotNone(reason)
            self.assertEqual(reason.key, "BRB")

    def test_every_story_has_a_canned_banner_for_people_in_the_flow(self):
        # The page only replaces the landing page; the banner reaches the rest.
        labels = [label.lower() for label, _ in SITE_BANNER_CANNED_MESSAGES]
        self.assertTrue(any("el niño" in label for label in labels), labels)


class TestBrbPage(TestCase):
    """What the landing page serves under each value."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("root")

    @override_settings(BRB="LIVE")
    def test_the_landing_page_serves_while_live(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "index.html")
        self.assertTemplateNotUsed(response, "brb.html")

    @override_settings(BRB="EL_NINO")
    def test_el_nino_takes_the_landing_page_down_with_its_story(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertTemplateUsed(response, "brb/el_nino.html")
        self.assertTemplateNotUsed(response, "index.html")
        self.assertContains(response, "El Niño", status_code=503)
        self.assertContains(response, "our own servers", status_code=503)
        self.assertContains(response, "Back After the Weather", status_code=503)

    @override_settings(BRB="EL_NINO")
    def test_the_page_is_never_cached_and_says_when_to_retry(self):
        response = self.client.get(self.url)
        self.assertEqual(response["Retry-After"], str(brb.RETRY_AFTER_SECONDS))
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(BRB="EL_NINO")
    def test_the_rest_of_the_site_keeps_working(self):
        for name in ("scan", "about"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)

    @override_settings(BRB="BRB")
    def test_the_original_value_still_shows_the_original_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertTemplateUsed(response, "brb/classic.html")
        self.assertContains(response, "PBS", status_code=503)

    def test_every_story_renders_with_an_image_that_ships(self):
        """Each story renders standalone, and every image it names is a
        static file that is really there, at the size the markup claims."""
        from PIL import Image

        for key in brb.BRB_REASONS:
            with override_settings(BRB=key):
                response = self.client.get(self.url)
            self.assertEqual(response.status_code, 503, key)
            html = response.content.decode()
            # Standalone: nothing to fetch but the pictures.
            self.assertNotRegex(html, r"<link\b[^>]*\bstylesheet", key)
            self.assertNotRegex(html, r"<script\b", key)
            images = re.findall(r"<img\b[^>]*>", html)
            self.assertTrue(images, key)
            for tag in images:
                src = re.search(r'src="([^"]+)"', tag).group(1)
                width = int(re.search(r'width="(\d+)"', tag).group(1))
                height = int(re.search(r'height="(\d+)"', tag).group(1))
                relative = src.split("static/", 1)[1].split("?", 1)[0]
                found = finders.find(relative)
                self.assertIsNotNone(found, f"{key}: {src} is not shipped")
                with Image.open(found) as image:
                    self.assertEqual(image.size, (width, height), f"{key}: {src}")
