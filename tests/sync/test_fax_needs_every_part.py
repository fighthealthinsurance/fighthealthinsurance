"""A fax goes to an insurance company only with every part the person chose to
send. When the letter or the cover page can't be turned into a PDF, the fax
page says nothing was sent, and no fax is staged or sent. Building a fax, or
failing to, leaves none of its parts behind in the temporary directory, since
they hold the letter and the person's details."""

import os
import shutil
import tempfile
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.models import Appeal, Denial, FaxesToSend
from tests.pdf_fixtures import pandoc_stand_in

EMAIL = "rebecca.crumpler@example.com"
LETTER = (
    "Dear Example Health,\n\nI am Rebecca Lee Crumpler, member W123456789, "
    "appealing the denial of my MRI.\n\nSincerely,\nRebecca Lee Crumpler"
)
NOTHING_SENT = "We couldn&#x27;t build your fax, so nothing was sent."


class StageFaxNeedsEveryPartTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="denied",
            semi_sekret="the-case-secret",
            hashed_email=Denial.get_hashed_email(EMAIL),
            insurance_company="Example Health",
        )
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def post(self, fails_for=()):
        with (
            patch.object(tempfile, "tempdir", self.temp_dir),
            patch(
                "fighthealthinsurance.common_view_logic._try_pandoc_engines",
                pandoc_stand_in(fails_for=fails_for),
            ),
            patch("fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"),
            patch(
                "fighthealthinsurance.fax_views.SendFaxHelper.remote_send_fax"
            ) as send,
        ):
            response = self.client.post(
                reverse("stagefaxview"),
                {
                    "denial_id": self.denial.denial_id,
                    "email": EMAIL,
                    "semi_sekret": self.denial.semi_sekret,
                    "name": "Rebecca Lee Crumpler",
                    "insurance_company": "Example Health",
                    "fax_phone": "15551234567",
                    "completed_appeal_text": LETTER,
                    "fax_pwyw": "0",
                },
            )
        return response, send

    def test_a_letter_that_cannot_be_built_is_not_staged_or_sent(self):
        _, send = self.post(fails_for=["appealtxt"])
        self.assertEqual(
            (FaxesToSend.objects.count(), Appeal.objects.count(), send.call_count),
            (0, 0, 0),
        )

    def test_a_cover_page_that_cannot_be_built_stops_the_fax(self):
        _, send = self.post(fails_for=["info_cover"])
        self.assertEqual((FaxesToSend.objects.count(), send.call_count), (0, 0))

    def test_the_page_says_nothing_was_sent(self):
        response, _ = self.post(fails_for=["appealtxt"])
        self.assertContains(response, NOTHING_SENT)

    def test_a_fax_whose_parts_all_build_is_staged_and_sent(self):
        _, send = self.post()
        self.assertEqual((FaxesToSend.objects.count(), send.call_count), (1, 1))

    def test_building_a_fax_leaves_no_files_behind(self):
        self.post()
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_a_fax_that_cannot_be_built_leaves_no_files_behind(self):
        self.post(fails_for=["appealtxt"])
        self.assertEqual(os.listdir(self.temp_dir), [])
