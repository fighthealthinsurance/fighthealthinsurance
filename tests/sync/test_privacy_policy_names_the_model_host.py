"""The privacy policy says where the on-device model's files come from.

When a person turns on better text recognition on the scan page, their
browser downloads the model files from Hugging Face, which then sees the
download request and nothing else. The policy must say so, say what stays
private, link Hugging Face's own policy, and carry the date of that change;
the printed PDF is made from this page, so the page is the source of truth.
"""

from bs4 import BeautifulSoup
from django.test import Client, TestCase
from django.urls import reverse


class PrivacyPolicyNamesTheModelHostTest(TestCase):
    def setUp(self):
        body = Client().get(reverse("privacy_policy")).content.decode("utf-8")
        self.page = BeautifulSoup(body, "html.parser")
        self.text = " ".join(self.page.get_text(" ").split())

    def test_the_disclosure_sits_in_the_content_delivery_bullet(self):
        bullet = next(
            (li for li in self.page.find_all("li") if "Content Delivery" in li.get_text()),
            None,
        )
        self.assertIsNotNone(bullet, "the content delivery bullet is gone")
        bullet_text = " ".join(bullet.get_text(" ").split())
        self.assertIn("your browser downloads the AI model files from Hugging Face", bullet_text)
        self.assertIn("but not your documents or your text", bullet_text)
        link = bullet.find("a", href="https://huggingface.co/privacy")
        self.assertIsNotNone(link, "Hugging Face's privacy policy is not linked")
        self.assertIn("Hugging Face", link.get_text(strip=True))

    def test_the_option_is_named_as_it_appears_on_the_scan_page(self):
        self.assertIn("better text recognition for photos and scans on the scan page", self.text)

    def test_the_date_moved_with_the_change(self):
        self.assertIn("Last updated: September 12, 2026", self.text)
        self.assertNotIn("September 7, 2026", self.text)

    def test_the_deletion_right_says_anonymous_totals_survive(self):
        # Since the status counters (#1017) a deletion request leaves
        # count-only totals behind; the right says so, in one plain sentence,
        # right after the request link.
        deletion = next(li for li in self.page.find_all("li") if "Deletion" in li.get_text() and "remove_data" in li.decode())
        text = " ".join(deletion.get_text(" ").split())
        self.assertIn(
            "We keep anonymous totals of how much work the service has done, such as how many appeals it has generated; "
            "they contain no information about you and are not affected by your request.",
            text,
        )

    def test_no_em_dash_entered_with_the_change(self):
        bullet = next(li for li in self.page.find_all("li") if "Content Delivery" in li.get_text())
        self.assertNotIn("—", bullet.get_text())
