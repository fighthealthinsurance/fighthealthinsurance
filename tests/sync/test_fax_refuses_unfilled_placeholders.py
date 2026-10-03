"""A letter with blanks left in it is not faxed to an insurance company.

The appeal page names the blanks before the fax form is sent, but only in a
browser that runs its script. The fax form checks the same pattern list on
the server (FaxForm, through StageFaxView), so a letter still holding
``[Your Name]`` or ``{{SCSID}}`` comes back to the person with the blanks
named, and nothing is staged, saved or sent.
"""

from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.helpers.fax_helpers import FaxHelperResults
from fighthealthinsurance.models import Denial

EMAIL = "patient@example.com"
WITH_BLANKS = (
    "Dear Example Health,\n\nI am [Your Name], member {{SCSID}}, appealing "
    "the denial of my MRI.\n\nSincerely,\n[Your Name]"
)
COMPLETE = (
    "Dear Example Health,\n\nI am Pat Example, member W123456789, appealing "
    "the denial of my MRI, which cost $500 [1].\n\nSincerely,\nPat Example"
)


class StageFaxRefusesUnfilledPlaceholdersTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="denied",
            semi_sekret="the-case-secret",
            hashed_email=Denial.get_hashed_email(EMAIL),
            insurance_company="Example Health",
        )

    def post(self, letter: str):
        with (
            patch(
                "fighthealthinsurance.common_view_logic.AppealAssemblyHelper.create_or_update_appeal"
            ) as assemble,
            patch(
                "fighthealthinsurance.fax_views.SendFaxHelper.stage_appeal_as_fax",
                return_value=FaxHelperResults(
                    uuid="00000000-0000-0000-0000-000000000042",
                    hashed_email=self.denial.hashed_email,
                ),
            ) as stage,
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
                    "name": "Pat Example",
                    "insurance_company": "Example Health",
                    "fax_phone": "15551234567",
                    "completed_appeal_text": letter,
                    "fax_pwyw": "0",
                },
            )
        return response, assemble, stage, send

    def test_a_letter_with_blanks_is_not_staged_or_sent(self):
        _, assemble, stage, send = self.post(WITH_BLANKS)
        self.assertEqual(
            (assemble.call_count, stage.call_count, send.call_count), (0, 0, 0)
        )

    def test_the_page_comes_back_naming_the_blanks(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            "Fill in these blanks before we fax your letter: [Your Name], {{SCSID}}.",
        )

    def test_the_error_belongs_to_the_letter(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertIn("completed_appeal_text", response.context["fax_form"].errors)

    def test_the_letter_comes_back_as_it_was_sent(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(response, "I am [Your Name], member {{SCSID}}")

    def test_a_refused_letter_does_not_save_the_fax_number_on_the_case(self):
        self.post(WITH_BLANKS)
        self.denial.refresh_from_db()
        self.assertFalse(self.denial.appeal_fax_number)

    def test_a_complete_letter_with_a_citation_and_an_amount_is_faxed(self):
        response, _, stage, send = self.post(COMPLETE)
        self.assertEqual(
            (response.status_code, stage.call_count, send.call_count), (200, 1, 1)
        )
