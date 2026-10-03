"""A letter with blanks left in it is not faxed to an insurance company
unless the person says to send it as it is.

The appeal page names the blanks before the fax form is sent, but only in a
browser that runs its script. The fax form checks the same pattern list on
the server (FaxForm, through StageFaxView), so a letter still holding
``[Your Name]`` or ``{{SCSID}}`` comes back to the person with the blanks
named, and nothing is staged, saved or sent. That page has a tick box under
the letter, "Send it as it is", for what the check finds that is not really a
blank; the appeal page's "Send anyway" posts the same field. With it, the
letter is faxed, and the log says how many blanks went, never what they were.
"""

import re
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.forms import FaxForm
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

    def post(self, letter: str, **extra: str):
        # autospec, so the builder refuses an argument it doesn't take the
        # way the real one does.
        with (
            patch(
                "fighthealthinsurance.common_view_logic.AppealAssemblyHelper.create_or_update_appeal",
                autospec=True,
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
                    **extra,
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

    def test_a_letter_with_blanks_sent_as_it_is_is_staged_and_sent(self):
        response, _, stage, send = self.post(WITH_BLANKS, send_with_placeholders="1")
        self.assertEqual(
            (response.status_code, stage.call_count, send.call_count), (200, 1, 1)
        )

    def test_the_appeal_is_built_without_the_answer_to_send_it_as_it_is(self):
        _, assemble, *_ = self.post(WITH_BLANKS, send_with_placeholders="1")
        self.assertNotIn("send_with_placeholders", assemble.call_args.kwargs)

    def test_the_page_naming_the_blanks_offers_to_send_it_as_it_is(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            '<input type="checkbox" name="send_with_placeholders" value="1"'
            ' class="fhi-check" id="id_send_with_placeholders"'
            ' aria-describedby="id_completed_appeal_text_error">',
            html=True,
        )

    def test_the_box_is_described_by_the_list_of_blanks(self):
        page = self.post(WITH_BLANKS)[0].content.decode()
        box = re.search(r'<input[^>]*id="id_send_with_placeholders"[^>]*>', page)
        described_by = re.search(r'aria-describedby="([^"]+)"', box.group(0))
        description = described_by and re.search(
            rf'<ul[^>]*id="{re.escape(described_by.group(1))}"[^>]*>(.*?)</ul>',
            page,
            re.S,
        )
        self.assertIn(
            "Fill in these blanks before we fax your letter: [Your Name], {{SCSID}}.",
            description.group(1) if description else "",
        )

    def test_the_box_is_labelled_with_what_ticking_it_means(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            '<label for="id_send_with_placeholders">'
            "Send it as it is: I've checked these are not blanks</label>",
            html=True,
        )

    def test_the_box_sits_under_the_letter(self):
        page = self.post(WITH_BLANKS)[0].content.decode()
        in_order = [
            page.index(f'id="id_{name}"')
            for name in (
                "completed_appeal_text",
                "send_with_placeholders",
                "include_provided_health_history",
            )
        ]
        self.assertEqual(in_order, sorted(in_order))

    def test_the_message_says_where_the_box_is(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            "If you&#x27;ve checked and these are not blanks, tick the box "
            "under your letter to send it as it is.",
        )

    def test_a_letter_sent_as_it_is_logs_how_many_blanks_and_not_which(self):
        with patch("fighthealthinsurance.fax_views.logger") as logger:
            self.post(WITH_BLANKS, send_with_placeholders="1")
        logged = [
            str(call.args[0])
            for method in (logger.debug, logger.info, logger.warning)
            for call in method.call_args_list
        ]
        self.assertEqual(
            (
                [line for line in logged if "placeholders" in line],
                [line for line in logged if "Your Name" in line or "SCSID" in line],
            ),
            (["Fax staged with placeholders the sender confirmed: 2"], []),
        )

    def test_a_complete_letter_logs_nothing_about_blanks(self):
        with patch("fighthealthinsurance.fax_views.logger") as logger:
            self.post(COMPLETE, send_with_placeholders="1")
        logged = [str(call.args[0]) for call in logger.info.call_args_list]
        self.assertEqual([line for line in logged if "placeholders" in line], [])


class SendItAsItIsBoxTest(TestCase):
    """The box is on the fax form only with a letter that has blanks in it."""

    def test_the_appeal_page_form_has_no_box(self):
        self.assertNotIn("send_with_placeholders", FaxForm().fields)

    def test_a_complete_letter_has_no_box(self):
        form = FaxForm(data={"completed_appeal_text": COMPLETE})
        self.assertNotIn("send_with_placeholders", form.fields)
