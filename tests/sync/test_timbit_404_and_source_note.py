"""The 404 page stars Professor Timbit; the page source and console invite contributors."""

from pathlib import Path

from django.test import TestCase

REPO_URL = "https://github.com/fighthealthinsurance/fighthealthinsurance"
STATIC = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static"


class Timbit404Test(TestCase):
    def test_unknown_url_is_a_real_404_with_timbit(self):
        response = self.client.get("/no-such-page-for-timbit")
        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response, "Professor Timbit sniffed everywhere", status_code=404
        )
        self.assertContains(response, "images/timbit-404.jpg", status_code=404)

    def test_404_echoes_the_path_asked_for(self):
        response = self.client.get("/no-such-page-for-timbit")
        self.assertContains(
            response, "sniff /no-such-page-for-timbit", status_code=404
        )

    def test_404_never_echoes_markup_from_the_path(self):
        response = self.client.get("/x<script>alert(1)</script>")
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "<script>alert(1)", status_code=404)

    def test_404_keeps_the_way_to_start_an_appeal(self):
        response = self.client.get("/no-such-page-for-timbit")
        self.assertContains(response, "Start Your Appeal", status_code=404)

    def test_404_photo_is_small(self):
        self.assertLess((STATIC / "images" / "timbit-404.jpg").stat().st_size, 100_000)


class SourceNoteTest(TestCase):
    def test_page_source_has_the_contributor_note_in_a_comment(self):
        html = self.client.get("/").content.decode()
        start = html.index("<!--")
        comment = html[start : html.index("-->", start)]
        self.assertIn("Professor Timbit", comment)
        self.assertIn(REPO_URL, comment)

    def test_console_note_points_at_the_repo(self):
        self.assertIn(REPO_URL, (STATIC / "js" / "source_note.js").read_text())

    def test_console_note_is_bundled_on_every_page(self):
        base = (
            STATIC.parent / "templates" / "base.html"
        ).read_text()
        self.assertIn("js/source_note.js", base)
