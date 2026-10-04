"""The agreements a person ticks are recorded, in the words they saw."""

from bs4 import BeautifulSoup
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import consent
from fighthealthinsurance.models import ConsentRecord, Denial


def _label_text(page: str, name: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    label = soup.find("label", attrs={"for": name})
    assert label is not None, name
    return " ".join(label.get_text(" ", strip=True).split()).replace(" .", ".")


class TheWordingHasOneSourceTest(TestCase):
    def test_the_intake_page_shows_the_words_the_record_keeps(self):
        page = self.client.get(reverse("scan")).content.decode()
        for name, label in consent.BOXES.items():
            self.assertEqual(_label_text(page, name), label, name)

    def test_the_policy_pages_carry_the_versions_the_record_keeps(self):
        tos = self.client.get(reverse("tos")).content.decode()
        privacy = self.client.get(reverse("privacy_policy")).content.decode()
        self.assertIn(
            f"Last updated: {consent.TERMS_VERSION:%B} {consent.TERMS_VERSION.day}, {consent.TERMS_VERSION.year}",
            tos,
        )
        self.assertIn(
            f"Last updated: {consent.PRIVACY_VERSION:%B} {consent.PRIVACY_VERSION.day}, {consent.PRIVACY_VERSION.year}",
            privacy,
        )


class TheIntakeRecordsTheBoxesTest(TestCase):
    def test_a_submission_keeps_what_was_ticked(self):
        response = self.client.post(
            reverse("process"),
            {
                "email": "consent-test@example.com",
                "denial_text": "Your claim has been denied as not medically necessary.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        denial = Denial.objects.get()
        record = ConsentRecord.objects.get(denial=denial)
        self.assertEqual(record.channel, "site")
        self.assertFalse(record.on_behalf)
        self.assertEqual(record.finish_in, "site")
        self.assertEqual(record.terms_version, consent.TERMS_VERSION)
        self.assertEqual(record.privacy_version, consent.PRIVACY_VERSION)
        self.assertEqual([b["name"] for b in record.boxes], list(consent.BOXES))
        self.assertTrue(all(b["ticked"] for b in record.boxes))
        self.assertEqual({b["name"]: b["label"] for b in record.boxes}, consent.BOXES)

    def test_the_record_holds_no_email_or_letter(self):
        self.client.post(
            reverse("process"),
            {
                "email": "consent-test@example.com",
                "denial_text": "A letter with a secret word: zebraflute.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
            follow=True,
        )
        record = ConsentRecord.objects.get()
        flat = str(record.boxes) + record.channel + record.assistant_client
        self.assertNotIn("consent-test@example.com", flat)
        self.assertNotIn("zebraflute", flat)

    def test_a_broken_record_never_blocks_the_intake(self):
        with patch(
            "fighthealthinsurance.models.ConsentRecord.objects.create",
            side_effect=RuntimeError("no table today"),
        ):
            response = self.client.post(
                reverse("process"),
                {
                    "email": "consent-test@example.com",
                    "denial_text": "Your claim has been denied.",
                    "pii": "on",
                    "tos": "on",
                    "privacy": "on",
                    "personalonly": "on",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(ConsentRecord.objects.count(), 0)
