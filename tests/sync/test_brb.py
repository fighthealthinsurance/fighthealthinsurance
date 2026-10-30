"""The BRB flag: while the site is down on purpose, the landing page tells
why with a 503 and every other page keeps working (brb.py, views.root_view).
"""

import io
import re
import tempfile
from pathlib import Path

from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.core.management.base import CommandError
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

    def test_reason_named_forgives_spelling_but_invents_no_story(self):
        self.assertEqual(brb.reason_named("el niño").key, "EL_NINO")
        self.assertIsNone(brb.reason_named("EL_NINOOO"))
        self.assertIsNone(brb.reason_named("LIVE"))

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
        self.assertTrue(any("server migration" in label for label in labels), labels)


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
        # Said in the page itself too, so the static copy (static-brb) says
        # the same from any host.
        self.assertContains(
            response, '<meta name="robots" content="noindex, nofollow">', status_code=503
        )
        self.assertContains(
            response, '<meta http-equiv="Pragma" content="no-cache">', status_code=503
        )
        self.assertContains(
            response, 'http-equiv="Cache-Control" content="no-cache, no-store', status_code=503
        )

    @override_settings(BRB="EL_NINO")
    def test_the_rest_of_the_site_keeps_working(self):
        for name in ("scan", "about"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)

    @override_settings(BRB="MIGRATION")
    def test_migration_takes_the_landing_page_down_with_the_lesbaru(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertTemplateUsed(response, "brb/migration.html")
        self.assertContains(response, "Server Migration", status_code=503)
        self.assertContains(response, "Lesbaru", status_code=503)
        self.assertContains(response, "lesbaru-optimized.jpg", status_code=503)
        self.assertContains(response, "license plate blurred", status_code=503)

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


class TestRenderBrbStatic(TestCase):
    """manage.py render_brb_static: the same page as a static site, for the
    static-brb repository that Cloudflare is pointed at while the web tier
    itself is away."""

    def _render(self, out, **options):
        stdout = io.StringIO()
        call_command("render_brb_static", out=out, stdout=stdout, **options)
        return stdout.getvalue()

    def test_index_tells_the_story_named_and_every_story_has_a_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render(tmp, story="el nino")
            out = Path(tmp)
            index = (out / "index.html").read_text()
            self.assertIn("El Niño", index)
            self.assertEqual(index, (out / "404.html").read_text())
            self.assertEqual(index, (out / "el-nino.html").read_text())
            self.assertIn("Server Migration", (out / "migration.html").read_text())
            self.assertIn("PBS", (out / "brb.html").read_text())

    def test_the_pages_say_no_cache_and_noindex_and_fetch_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render(tmp, story="MIGRATION")
            index = (Path(tmp) / "index.html").read_text()
            self.assertIn('<meta name="robots" content="noindex, nofollow">', index)
            self.assertIn('<meta http-equiv="Pragma" content="no-cache">', index)
            self.assertNotRegex(index, r"<link\b[^>]*\bstylesheet")
            self.assertNotRegex(index, r"<script\b")
            # Rendered without a request, so nothing claims the live site's address.
            self.assertNotIn('rel="canonical"', index)

    def test_every_picture_is_beside_the_pages_at_a_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render(tmp)
            out = Path(tmp)
            pages = [p for p in out.glob("*.html")]
            self.assertEqual(len(pages), 2 + len(brb.BRB_REASONS))
            sources = set()
            for page in pages:
                html = page.read_text()
                self.assertNotIn("/static/", html, page.name)
                sources |= set(re.findall(r'src="([^"]+)"', html))
            self.assertTrue(sources)
            for src in sources:
                self.assertTrue(src.startswith("images/"), src)
                self.assertTrue((out / src).is_file(), src)
            self.assertTrue(any("lesbaru" in src for src in sources), sources)

    def test_the_default_story_is_el_nino(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._render(tmp)
            self.assertIn("Wrote EL_NINO as index.html", report)

    def test_a_value_naming_no_story_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CommandError):
                self._render(tmp, story="nope")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_the_hosts_own_files_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "README.md").write_text("mine")
            (out / "_headers").write_text("mine too")
            self._render(tmp)
            self.assertEqual((out / "README.md").read_text(), "mine")
            self.assertEqual((out / "_headers").read_text(), "mine too")
