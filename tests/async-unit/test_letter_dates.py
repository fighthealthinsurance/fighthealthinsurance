"""Letters carry today's date on their date line, and no other date changes.

letter_dates.date_the_letter sets the date line at the top of a letter to
today's date where the case is; substitute_appeal_fields runs it on every
letter the appeals page streams (and the assistant's chat returns, see
tests/sync/test_assistant_draft_tools.py). The clock is frozen on a day that
is not the real one, so a pass cannot come from the machine's own date, and
the settings are the real ones (TIME_ZONE "UTC").
"""

import datetime
import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)

from fighthealthinsurance.common_view_logic import (
    mark_proposal_chosen,
    substitute_appeal_fields,
)
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.letter_dates import (
    STATE_ZONES,
    date_the_letter,
    format_letter_date,
    letter_zone,
    todays_letter_date,
)
from fighthealthinsurance.models import Denial, ProposedAppeal

# 6pm UTC on November 3, 2026.
FROZEN_NOW = datetime.datetime(2026, 11, 3, 18, 0, tzinfo=datetime.timezone.utc)
TODAY = "November 3, 2026"


# 3am UTC on November 4: 7pm on November 3 in Los Angeles.
LATE_IN_LOS_ANGELES = datetime.datetime(2026, 11, 4, 3, 0, tzinfo=datetime.timezone.utc)
# 5:30am UTC on November 4: half past midnight in New York, 11:30pm on
# November 3 in Chicago.
JUST_PAST_MIDNIGHT_IN_NEW_YORK = datetime.datetime(
    2026, 11, 4, 5, 30, tzinfo=datetime.timezone.utc
)
# 6:30am UTC on November 4: half past midnight in Chicago, 11:30pm on
# November 3 in Denver.
JUST_PAST_MIDNIGHT_IN_CHICAGO = datetime.datetime(
    2026, 11, 4, 6, 30, tzinfo=datetime.timezone.utc
)


def frozen_clock(now=FROZEN_NOW):
    return patch("django.utils.timezone.now", return_value=now)


def headed(*lines):
    """A letter whose header is ``lines``, then its salutation and body."""
    return "\n".join(lines) + "\n\nDear Example Health,\nPlease reverse the denial.\n"


def a_letter(date_line=None):
    """A letter as a draft lays one out: sender, date, recipient, subject,
    salutation, and a body naming the denial date, the date of service and
    the appeal deadline. ``date_line`` None leaves the date line out."""
    head = "Jane Doe\n123 Main Street\nSpringfield, IL 62704\n\n"
    if date_line is not None:
        head += f"{date_line}\n\n"
    return head + (
        "Example Health Plan\n"
        "Appeals Department\n"
        "PO Box 1234\n"
        "Springfield, IL 62705\n"
        "\n"
        "Re: Appeal of the denial of my MRI, claim 12345\n"
        "\n"
        "Dear Appeals Committee,\n"
        "\n"
        "I am writing to appeal your decision of March 3, 2026, which denied "
        "coverage for the lumbar MRI my physician ordered on 02/12/2026. Your "
        "letter says I must appeal by August 30, 2026, and I am appealing "
        "within that window.\n"
        "\n"
        "Sincerely,\n"
        "Jane Doe\n"
    )


class DateLineTest(SimpleTestCase):
    def test_a_future_date_line_becomes_today(self):
        self.assertEqual(
            date_the_letter(a_letter("October 25, 2026"), TODAY), a_letter(TODAY)
        )

    def test_a_vague_date_line_becomes_today(self):
        for vague in ("Later this month", "later this month", "Mid-October"):
            with self.subTest(vague=vague):
                self.assertEqual(
                    date_the_letter(a_letter(vague), TODAY), a_letter(TODAY)
                )

    def test_a_date_placeholder_line_becomes_today(self):
        for blank in ("[Date]", "[Insert Date]", "[Today's Date]", "<Insert date>"):
            with self.subTest(blank=blank):
                self.assertEqual(
                    date_the_letter(a_letter(blank), TODAY), a_letter(TODAY)
                )

    def test_every_common_written_form_is_read_as_the_date(self):
        for written in (
            "Oct. 25, 2026",
            "25 October 2026",
            "10/25/2026",
            "2026-10-25",
            "Monday, October 26th, 2026",
            "Thu, Oct 8, 2026",
            "25-Oct-2026",
            "October 25",
        ):
            with self.subTest(written=written):
                self.assertEqual(
                    date_the_letter(a_letter(written), TODAY), a_letter(TODAY)
                )

    def test_a_date_label_and_its_emphasis_are_kept(self):
        for labelled, expected in (
            ("Date: October 25, 2026", f"Date: {TODAY}"),
            ("Date:  [Insert Date]", f"Date:  {TODAY}"),
            ("**Date:** later this month", f"**Date:** {TODAY}"),
            ("Appeal Date: October 25, 2026", f"Appeal Date: {TODAY}"),
            ("Date Sent: 10/25/2026", f"Date Sent: {TODAY}"),
            ("## October 25, 2026", f"## {TODAY}"),
            ("### Date: October 25, 2026", f"### Date: {TODAY}"),
            ("October 25, 2026,", f"{TODAY},"),
            ("\u200bOctober 25, 2026\ufeff", f"\u200b{TODAY}\ufeff"),
        ):
            with self.subTest(labelled=labelled):
                self.assertEqual(
                    date_the_letter(a_letter(labelled), TODAY), a_letter(expected)
                )

    def test_dates_in_the_body_are_untouched(self):
        dated = date_the_letter(a_letter("October 25, 2026"), TODAY)
        for body_date in ("March 3, 2026", "02/12/2026", "August 30, 2026"):
            with self.subTest(body_date=body_date):
                self.assertIn(body_date, dated)

    def test_a_letter_without_a_date_line_is_unchanged(self):
        self.assertEqual(date_the_letter(a_letter(), TODAY), a_letter())

    def test_a_letter_that_opens_with_its_salutation_is_unchanged(self):
        letter = "Dear Example Health,\n\nOctober 25, 2026 is when I was seen.\n"
        self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_a_date_under_the_subject_line_is_untouched(self):
        letter = "Re: Claim 12345\nMarch 3, 2026\n\nDear Example Health,\n"
        self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_a_date_under_a_date_of_birth_label_is_untouched(self):
        letter = (
            "Jane Doe\nDate of Birth:\n01/02/1980\n\nDear Example Health,\n"
            "Please reverse the denial.\n"
        )
        self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_a_date_under_a_line_holding_its_own_value_becomes_today(self):
        for above in (
            "Date of Birth: 01/02/1980",
            "DOB 01/02/1980",
            "Attn: Member Service",
            "Example Health Customer Service",
        ):
            with self.subTest(above=above):
                self.assertEqual(
                    date_the_letter(
                        headed("Jane Doe", above, "October 25, 2026"), TODAY
                    ),
                    headed("Jane Doe", above, TODAY),
                )

    def test_a_date_finishing_a_sentence_above_it_is_untouched(self):
        for above in ("Your plan denied this on", "Your letter dated"):
            with self.subTest(above=above):
                letter = headed(above, "September 15, 2026")
                self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_a_date_a_blank_line_under_its_label_is_untouched(self):
        letter = headed("Jane Doe", "Date of service:", "", "09/01/2026")
        self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_a_list_of_dates_under_a_label_ends_at_a_blank_line(self):
        self.assertEqual(
            date_the_letter(
                headed(
                    "Dates of Service:",
                    "09/01/2026",
                    "09/02/2026",
                    "",
                    "October 25, 2026",
                ),
                TODAY,
            ),
            headed("Dates of Service:", "09/01/2026", "09/02/2026", "", TODAY),
        )

    def test_the_letter_date_label_is_read_under_another_dates_label(self):
        self.assertEqual(
            date_the_letter(
                headed("Denial Date:", "September 15, 2026", "Date: October 1, 2026"),
                TODAY,
            ),
            headed("Denial Date:", "September 15, 2026", f"Date: {TODAY}"),
        )

    def test_numbers_that_cannot_be_a_date_are_untouched(self):
        for number in ("12-34-5678", "13/45/2026", "2026-13-25", "10/25-2026"):
            with self.subTest(number=number):
                letter = headed("Jane Doe", number)
                self.assertEqual(date_the_letter(letter, TODAY), letter)

    def test_running_it_twice_changes_nothing(self):
        once = date_the_letter(a_letter("Later this month"), TODAY)
        self.assertEqual(date_the_letter(once, TODAY), once)

    def test_windows_line_endings_survive(self):
        letter = a_letter("October 25, 2026").replace("\n", "\r\n")
        self.assertEqual(
            date_the_letter(letter, TODAY), a_letter(TODAY).replace("\n", "\r\n")
        )

    def test_an_empty_letter_is_returned_as_it_is(self):
        self.assertEqual(date_the_letter("", TODAY), "")


class TodaysDateTest(SimpleTestCase):
    def test_dates_are_written_month_day_year_without_a_leading_zero(self):
        self.assertEqual(format_letter_date(datetime.date(2026, 11, 3)), TODAY)

    def test_a_case_with_no_state_is_dated_in_pacific_time(self):
        with frozen_clock(LATE_IN_LOS_ANGELES):
            self.assertEqual(todays_letter_date(), TODAY)

    def test_a_state_not_listed_is_dated_in_pacific_time(self):
        with frozen_clock(JUST_PAST_MIDNIGHT_IN_NEW_YORK):
            self.assertEqual(todays_letter_date("ZZ"), TODAY)

    def test_a_case_is_dated_in_its_states_zone(self):
        with frozen_clock(JUST_PAST_MIDNIGHT_IN_NEW_YORK):
            self.assertEqual(todays_letter_date(" ny "), "November 4, 2026")

    def test_a_state_across_two_zones_is_dated_in_its_western_one(self):
        # Most of Texas is on Chicago's time; El Paso is on Denver's.
        with frozen_clock(JUST_PAST_MIDNIGHT_IN_CHICAGO):
            self.assertEqual(todays_letter_date("TX"), TODAY)

    def test_every_listed_zone_loads(self):
        for state, zone in STATE_ZONES.items():
            with self.subTest(state=state):
                self.assertEqual(str(letter_zone(state)), zone)

    def test_the_date_line_defaults_to_today(self):
        with frozen_clock():
            dated = date_the_letter(a_letter("October 25, 2026"))
        self.assertEqual(dated, a_letter(TODAY))


class SubstitutedDatesTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("dates@example.com"),
            insurance_company="Example Health",
        )
        # Denial.date is set on creation; pin it to a day that is not today.
        Denial.objects.filter(pk=self.denial.pk).update(date=datetime.date(2026, 9, 2))
        self.denial.refresh_from_db()

    def test_a_template_date_line_gets_today_not_the_denial_date(self):
        # The Ozempic and OCREVUS templates (fixtures/followup.yaml) open so.
        with frozen_clock():
            letter = substitute_appeal_fields(
                self.denial, "Date:  [Insert Date]\n\nTo:\n[Insurance Company Name]"
            )
        self.assertEqual(letter, f"Date:  {TODAY}\n\nTo:\nExample Health")

    def test_a_date_placeholder_in_the_body_is_left_for_the_person(self):
        for blank in ("[Date]", "[Insert Date]", "[Current Date]", "[Today's Date]"):
            with self.subTest(blank=blank):
                body = f"Dear Example Health,\nYour letter dated {blank} denied it."
                with frozen_clock():
                    letter = substitute_appeal_fields(self.denial, body)
                self.assertEqual(letter, body)

    def test_the_dollar_date_placeholder_is_the_denial_date(self):
        # The COVID-19 vaccine template (fixtures/initial.yaml) uses $DATE for
        # the day the plan denied the claim.
        self.denial.denial_date = datetime.date(2026, 9, 15)
        with frozen_clock():
            letter = substitute_appeal_fields(
                self.denial, "To Whom it May Concern:\nThe claim was denied on $DATE."
            )
        self.assertIn("denied on September 15, 2026.", letter)

    def test_the_dollar_date_placeholder_stays_without_a_denial_date(self):
        with frozen_clock():
            letter = substitute_appeal_fields(
                self.denial, "To Whom it May Concern:\nThe claim was denied on $DATE."
            )
        self.assertIn("denied on $DATE.", letter)

    def test_the_date_line_is_today_where_the_case_is(self):
        self.denial.your_state = "NY"
        with frozen_clock(JUST_PAST_MIDNIGHT_IN_NEW_YORK):
            letter = substitute_appeal_fields(self.denial, a_letter("October 25, 2026"))
        self.assertEqual(letter, a_letter("November 4, 2026"))

    def test_the_date_line_can_be_left_as_it_is(self):
        with frozen_clock():
            letter = substitute_appeal_fields(
                self.denial, a_letter("October 25, 2026"), set_date_line=False
            )
        self.assertEqual(letter, a_letter("October 25, 2026"))

    def test_a_pick_shown_on_an_earlier_day_is_recorded_unedited(self):
        draft = ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text=a_letter("Later this month")
        )
        with frozen_clock():
            pick = mark_proposal_chosen(
                self.denial,
                a_letter("November 2, 2026"),
                editted=None,
                proposed_appeal_id=draft.id,
            )
        self.assertFalse(pick.editted)

    def test_a_pick_with_its_body_changed_is_recorded_edited(self):
        draft = ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text=a_letter("Later this month")
        )
        with frozen_clock():
            pick = mark_proposal_chosen(
                self.denial,
                a_letter(TODAY).replace("lumbar MRI", "MRI"),
                editted=None,
                proposed_appeal_id=draft.id,
            )
        self.assertTrue(pick.editted)

    def test_a_model_written_date_line_becomes_today(self):
        with frozen_clock():
            letter = substitute_appeal_fields(self.denial, a_letter("October 25, 2026"))
        self.assertEqual(letter, a_letter(TODAY))


@override_settings(
    TEMPORAL_ENABLED=False,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
)
class AppealsPageDateTest(TransactionTestCase):
    """The appeals page's stream: a stored draft replayed and a fresh one
    written in the run both reach the browser with today's date."""

    def setUp(self):
        super().setUp()
        for target in (
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        ):
            patcher = patch(target, new_callable=AsyncMock, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        pmt_patcher = patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
        )
        pmt = pmt_patcher.start()
        pmt.find_context_for_denial = AsyncMock(return_value=None)
        self.addCleanup(pmt_patcher.stop)
        self.email = "dated@example.com"
        # gen_attempts=3 skips the research phase, as the sibling stream
        # tests do.
        self.denial = Denial.objects.create(
            denial_id=9411,
            denial_text="Coverage for the lumbar MRI was denied as not medically necessary.",
            insurance_company="Example Health",
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(self.email),
            gen_attempts=3,
        )

    def _streamed_letters(self, mock_gen, fresh):
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        mock_gen.make_appeals.side_effect = lambda *a, **k: iter(fresh)

        async def drive():
            letters = []
            async for chunk in AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": self.denial.denial_id,
                    "email": self.email,
                    "semi_sekret": "sekret",
                }
            ):
                try:
                    frame = json.loads(chunk)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(frame, dict) and "content" in frame:
                    letters.append(frame["content"])
            return letters

        with frozen_clock():
            return async_to_sync(drive)()

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_stored_draft_replays_with_todays_date(self, mock_gen):
        ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text=a_letter("October 25, 2026")
        )
        letters = self._streamed_letters(mock_gen, fresh=[])
        self.assertIn(a_letter(TODAY), letters)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_an_edited_pick_replays_with_its_own_date(self, mock_gen):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=a_letter("October 30, 2026"),
            chosen=True,
            editted=True,
        )
        letters = self._streamed_letters(mock_gen, fresh=[])
        self.assertIn(a_letter("October 30, 2026"), letters)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_freshly_written_draft_streams_with_todays_date(self, mock_gen):
        fresh = GeneratedAppeal(
            text=a_letter("Later this month"),
            model_name="fhi-internal",
            context_level="full",
        )
        letters = self._streamed_letters(mock_gen, fresh=[fresh])
        self.assertIn(a_letter(TODAY), letters)
