"""Denial Watch shares with its own card, describes itself as a Dataset, and
invites reporters and researchers to get in touch."""

import json
import re

from django.test import TestCase, override_settings
from django.urls import reverse

DW_IMAGE = "https://www.fighthealthinsurance.com/static/images/og-denial-watch.jpg"
SITE_IMAGE = "https://www.fighthealthinsurance.com/static/images/og-share.jpg"


def _json_ld(html):
    nodes = []
    for raw in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        data = json.loads(raw)
        nodes.extend(data.get("@graph", [data]) if isinstance(data, dict) else data)
    return nodes


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class DenialWatchForReportersTest(TestCase):
    def setUp(self):
        self.html = self.client.get(reverse("denial-watch")).content.decode()

    def test_shares_with_its_own_image(self):
        self.assertIn(f'<meta property="og:image" content="{DW_IMAGE}">', self.html)
        self.assertIn(f'<meta name="twitter:image" content="{DW_IMAGE}">', self.html)

    def test_describes_itself_as_a_dataset(self):
        datasets = [n for n in _json_ld(self.html) if n.get("@type") == "Dataset"]
        self.assertEqual(len(datasets), 1)
        self.assertGreaterEqual(len(datasets[0]["description"]), 50)
        self.assertTrue(datasets[0]["isBasedOn"])

    def test_invites_reporters(self):
        self.assertIn('id="for-reporters"', self.html)
        self.assertIn("support42@fighthealthinsurance.com", self.html)


class SharedImageDefaultTest(TestCase):
    def test_other_pages_keep_the_site_image(self):
        html = self.client.get("/").content.decode()
        self.assertIn(f'<meta property="og:image" content="{SITE_IMAGE}">', html)
        self.assertIn(f'<meta name="twitter:image" content="{SITE_IMAGE}">', html)

    def test_about_us_invites_reporters(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn('id="for-reporters"', html)
