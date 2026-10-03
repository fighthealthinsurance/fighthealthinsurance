"""A letter with blanks left in it is not faxed to an insurance company
unless the person says to send it as it is.

The appeal page names the blanks before the fax form is sent, but only in a
browser that runs its script. The fax form checks the same pattern list on
the server (FaxForm, through StageFaxView), so a letter still holding
``[Your Name]`` or ``{{SCSID}}`` comes back to the person with the blanks
named, and nothing is staged, saved or sent. That page has a tick box under
the letter, "Send it as it is", for what the check finds that is not really a
blank. Its value is the list of blanks the page names, so ticking it says yes
to those and no others; the appeal page's "Send anyway" posts a list of its
own under the same name. A letter is faxed only when every blank in it is on
a posted list, and the log says how many blanks went, never what they were.
"""

import html
import json
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
# The same letter after an edit that adds a blank.
WITH_A_NEW_BLANK = WITH_BLANKS.replace(
    "appealing", "seen on [Date of Service], appealing"
)
# A letter with a line to sign on, and the same letter with a second line,
# of another length.
WITH_A_LINE = "Dear Example Health,\n\nSigned: ______________\nPat Example"
WITH_TWO_LINES = WITH_A_LINE.replace("Signed:", "Dated ________.\nSigned:")


def approved(*blanks: str) -> str:
    """A list of approved blanks, as the browser posts it."""
    return json.dumps(list(blanks))


SENT_ANYWAY = approved("[Your Name]", "{{SCSID}}")
BOX = re.compile(r'<input[^>]*id="id_approved_placeholders"[^>]*>')


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

    def test_a_letter_whose_blanks_are_all_approved_is_staged_and_sent(self):
        response, _, stage, send = self.post(
            WITH_BLANKS, approved_placeholders=SENT_ANYWAY
        )
        self.assertEqual(
            (response.status_code, stage.call_count, send.call_count), (200, 1, 1)
        )

    def test_a_blank_missing_from_the_approved_list_holds_the_letter(self):
        _, _, stage, send = self.post(
            WITH_A_NEW_BLANK, approved_placeholders=SENT_ANYWAY
        )
        self.assertEqual((stage.call_count, send.call_count), (0, 0))

    def test_a_letter_held_for_a_new_blank_names_every_blank(self):
        response, *_ = self.post(WITH_A_NEW_BLANK, approved_placeholders=SENT_ANYWAY)
        self.assertContains(
            response,
            "Fill in these blanks before we fax your letter: [Your Name], "
            "{{SCSID}}, [Date of Service].",
        )

    def test_two_approved_lists_together_cover_the_letter(self):
        """The ticked box's list and "Send anyway"'s, posted side by side."""
        _, _, stage, _ = self.post(
            WITH_A_NEW_BLANK,
            approved_placeholders=[SENT_ANYWAY, approved("[Date of Service]")],
        )
        self.assertEqual(stage.call_count, 1)

    def test_a_ticked_box_without_a_list_approves_nothing(self):
        for posted in (
            {"approved_placeholders": "1"},
            {"approved_placeholders": "on"},
            {"approved_placeholders": ""},
            {"send_with_placeholders": "1"},
        ):
            with self.subTest(posted=posted):
                _, _, stage, _ = self.post(WITH_BLANKS, **posted)
                self.assertEqual(stage.call_count, 0)

    def test_a_list_that_is_not_a_list_of_blanks_approves_nothing(self):
        for posted in (
            json.dumps({"[Your Name]": True, "{{SCSID}}": True}),
            json.dumps([["[Your Name]", "{{SCSID}}"]]),
            "[" * 100000 + "]" * 100000,
        ):
            with self.subTest(posted=posted[:40]):
                _, _, stage, _ = self.post(WITH_BLANKS, approved_placeholders=posted)
                self.assertEqual(stage.call_count, 0)

    def test_a_line_of_another_length_is_not_covered_by_an_approved_line(self):
        _, _, stage, _ = self.post(
            WITH_TWO_LINES, approved_placeholders=approved("______________")
        )
        self.assertEqual(stage.call_count, 0)

    def test_an_approved_line_is_faxed(self):
        _, _, stage, _ = self.post(
            WITH_A_LINE, approved_placeholders=approved("______________")
        )
        self.assertEqual(stage.call_count, 1)

    def test_the_list_made_in_the_browser_matches_a_letter_posted_with_crlf(self):
        """A browser posts a letter's line breaks as CRLF, while the page's
        script reads them as LF: a blank is the same either way."""
        letter = WITH_A_LINE + "\n" + WITH_BLANKS
        _, _, stage, _ = self.post(
            letter.replace("\n", "\r\n"),
            approved_placeholders=approved(
                "______________", "[Your Name]", "{{SCSID}}"
            ),
        )
        self.assertEqual(stage.call_count, 1)

    def test_the_appeal_is_built_without_the_approved_blanks(self):
        _, assemble, *_ = self.post(WITH_BLANKS, approved_placeholders=SENT_ANYWAY)
        self.assertNotIn("approved_placeholders", assemble.call_args.kwargs)

    def test_the_page_naming_the_blanks_offers_to_send_it_as_it_is(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            '<input type="checkbox" name="approved_placeholders"'
            f' value="{html.escape(json.dumps(["[Your Name]", "{{SCSID}}"]))}"'
            ' class="fhi-check" id="id_approved_placeholders"'
            ' aria-describedby="id_completed_appeal_text_error">',
            html=True,
        )

    def test_the_box_holds_exactly_the_blanks_the_page_names(self):
        """Every blank, including the ones already approved, and nothing
        else: ticking it says yes to what the page lists."""
        page = self.post(WITH_A_NEW_BLANK, approved_placeholders=SENT_ANYWAY)[0]
        box = BOX.search(page.content.decode())
        value = box and re.search(r'value="([^"]*)"', box.group(0))
        self.assertEqual(
            json.loads(html.unescape(value.group(1))) if value else None,
            ["[Your Name]", "{{SCSID}}", "[Date of Service]"],
        )

    def test_the_box_holds_each_line_as_it_is_written(self):
        page = self.post(WITH_TWO_LINES)[0]
        box = BOX.search(page.content.decode())
        value = box and re.search(r'value="([^"]*)"', box.group(0))
        self.assertEqual(
            json.loads(html.unescape(value.group(1))) if value else None,
            ["________", "______________"],
        )

    def test_the_box_is_described_by_the_list_of_blanks(self):
        page = self.post(WITH_BLANKS)[0].content.decode()
        box = BOX.search(page)
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

    def test_a_page_turned_back_for_something_else_has_no_box(self):
        """Sent anyway, the form is turned back for a name of only spaces,
        which a browser lets through. That page names no blanks, so it has
        no box to send them as they are."""
        response, _, stage, _ = self.post(
            WITH_BLANKS, approved_placeholders=SENT_ANYWAY, name="   "
        )
        self.assertEqual(
            (stage.call_count, BOX.search(response.content.decode())), (0, None)
        )

    def test_a_page_naming_the_blanks_never_comes_back_ticked(self):
        """Ticked, a box would send a blank typed in after, unseen."""
        response, *_ = self.post(WITH_A_NEW_BLANK, approved_placeholders=SENT_ANYWAY)
        box = BOX.search(response.content.decode())
        self.assertEqual(
            (box is not None, box is not None and "checked" in box.group(0)),
            (True, False),
        )

    def test_the_box_is_labelled_with_what_ticking_it_means(self):
        response, *_ = self.post(WITH_BLANKS)
        self.assertContains(
            response,
            '<label for="id_approved_placeholders">'
            "Send it as it is: I've checked these are not blanks</label>",
            html=True,
        )

    def test_the_box_sits_under_the_letter(self):
        page = self.post(WITH_BLANKS)[0].content.decode()
        in_order = [
            page.index(f'id="id_{name}"')
            for name in (
                "completed_appeal_text",
                "approved_placeholders",
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
            self.post(WITH_BLANKS, approved_placeholders=SENT_ANYWAY)
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
            self.post(COMPLETE, approved_placeholders=SENT_ANYWAY)
        logged = [str(call.args[0]) for call in logger.info.call_args_list]
        self.assertEqual([line for line in logged if "placeholders" in line], [])


class SendItAsItIsBoxTest(TestCase):
    """The box is on the fax form only once it holds a letter for blanks."""

    def test_the_appeal_page_form_has_no_box(self):
        self.assertNotIn("approved_placeholders", FaxForm().fields)

    def test_a_complete_letter_has_no_box(self):
        form = FaxForm(data={"completed_appeal_text": COMPLETE})
        form.is_valid()
        self.assertNotIn("approved_placeholders", form.fields)

    def test_a_letter_whose_blanks_are_approved_has_no_box(self):
        form = FaxForm(
            data={
                "completed_appeal_text": WITH_BLANKS,
                "approved_placeholders": SENT_ANYWAY,
            }
        )
        form.is_valid()
        self.assertNotIn("approved_placeholders", form.fields)
