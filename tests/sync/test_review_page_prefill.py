"""The review page starts from what we already know about the case.

Details (categorize.html) is the step where the person checks and corrects
what we gathered. Every field the denial row holds a value for starts with
that value, whichever way the person arrives: from reading the letter, or
back from the questions step. On the way forward, the denial date is also
read from the letter, on this server, when the row has none and the letter
states it plainly, and a value read from the letter says so under its field.
The way back shows the row as the person left it.
"""

import datetime
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.urls import reverse

from fighthealthinsurance import models
from fighthealthinsurance.ml import denial_triage, ml_models, typesafe
from tests.sync.test_back_url_token import EMAIL, BackLinkReferenceTestBase

HINT = "From your letter. Please check."
ROW_DATE = datetime.date(2024, 3, 1)


# The view reads the letter against today; the tests fix today so every run
# reads the same dates.
TODAY = datetime.date(2026, 10, 3)


def letter_day():
    """A recent date for the letter, since an old one is not taken as its
    date."""
    return TODAY - datetime.timedelta(days=30)


def clear_letter():
    """A letter whose date stands alone above the greeting, where a letter
    puts its own."""
    day = letter_day()
    return (
        "Example Health Plan\n"
        "PO Box 100\n"
        "\n"
        f"{day:%B} {day.day}, {day.year}\n"
        "\n"
        "Dear Member,\n"
        "We denied the MRI performed on February 20, 2024.\n"
    )


# Two dates, neither marked as the letter's own.
AMBIGUOUS_LETTER = (
    "Dear Member,\n"
    "We denied the MRI performed on February 20, 2024. You may appeal by "
    "08/31/2024.\n"
)


def shown(page, name):
    """What the review page shows in a field: an input's value, the values
    of a select's chosen options, or the values of the ticked boxes."""
    element = page.find(attrs={"name": name})
    assert element is not None, f"the review page has no {name} field"
    if element.name == "select":
        return [option["value"] for option in element.find_all("option", selected=True)]
    if element.get("type") == "checkbox":
        return [
            box["value"]
            for box in page.find_all("input", attrs={"name": name})
            if box.has_attr("checked")
        ]
    return element.get("value")


class ReviewPageTestBase(BackLinkReferenceTestBase):
    def setUp(self):
        today = patch("django.utils.timezone.localdate", return_value=TODAY)
        today.start()
        self.addCleanup(today.stop)
        super().setUp()
        self.plan_source = models.PlanSource.objects.create(name="Employer")
        self.insurer = models.InsuranceCompany.objects.create(name="Example Health")
        self.plan = models.InsurancePlan.objects.create(
            insurance_company=self.insurer, plan_name="Example Gold PPO"
        )
        self.denial.employer_name = "Acme Widgets"
        self.denial.denial_date = ROW_DATE
        self.denial.insurance_company_obj = self.insurer
        self.denial.insurance_plan_obj = self.plan
        self.denial.denial_type_text = "Out of network"
        self.denial.denial_text = clear_letter()
        self.denial.save()
        self.denial.plan_source.set([self.plan_source])

    def back_to_review(self):
        """The review page reached by its back link."""
        response = self.client.get(
            self.ref_url("categorize_review", self.issue_token())
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "categorize.html")
        return BeautifulSoup(response.content, "html.parser")

    def on_to_review(self):
        """The review page reached by going forward from reading the letter."""
        response = self.client.post(
            reverse("eev"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": self.denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "categorize.html")
        return BeautifulSoup(response.content, "html.parser")

    def without_a_stored_date(self, letter):
        self.denial.denial_date = None
        self.denial.denial_text = letter
        self.denial.save()


class ReviewPageStartsFromTheRowTest(ReviewPageTestBase):
    def expected(self):
        return {
            "employer_name": "Acme Widgets",
            "denial_date": "2024-03-01",
            "plan_source": [str(self.plan_source.pk)],
            "insurance_company_obj": [str(self.insurer.pk)],
            "insurance_plan_obj": [str(self.plan.pk)],
            "denial_type_text": "Out of network",
        }

    def test_each_field_starts_with_the_rows_value_when_coming_back(self):
        page = self.back_to_review()
        for name, value in self.expected().items():
            with self.subTest(field=name):
                self.assertEqual(shown(page, name), value)

    def test_each_field_starts_with_the_rows_value_after_the_letter_is_read(self):
        page = self.on_to_review()
        for name, value in self.expected().items():
            with self.subTest(field=name):
                self.assertEqual(shown(page, name), value)

    def test_a_stored_date_is_kept_over_the_one_in_the_letter(self):
        self.assertEqual(shown(self.on_to_review(), "denial_date"), "2024-03-01")


class ReviewPageReadsTheLetterDateTest(ReviewPageTestBase):
    def test_a_letter_with_a_clear_date_fills_an_empty_date(self):
        self.without_a_stored_date(clear_letter())
        self.assertEqual(
            shown(self.on_to_review(), "denial_date"), letter_day().isoformat()
        )

    def test_a_date_the_person_cleared_stays_blank_when_they_come_back(self):
        # Continuing with the box cleared stores no date, so this is the row
        # the way back finds after the person cleared the date we read.
        self.without_a_stored_date(clear_letter())
        self.assertIsNone(shown(self.back_to_review(), "denial_date"))

    def test_a_letter_with_an_ambiguous_date_leaves_the_date_blank(self):
        self.without_a_stored_date(AMBIGUOUS_LETTER)
        self.assertIsNone(shown(self.on_to_review(), "denial_date"))

    def test_reading_the_date_asks_no_outside_model(self):
        self.without_a_stored_date(clear_letter())
        refuse = AssertionError("the review page asked a model")
        with (
            patch.object(typesafe, "ask", side_effect=refuse) as ask,
            patch.object(denial_triage, "triage", side_effect=refuse) as triage,
            patch.object(
                ml_models.RemoteModelLike, "_infer", side_effect=refuse
            ) as infer,
            patch.object(
                ml_models.RemoteModelLike, "_infer_no_context", side_effect=refuse
            ) as infer_no_context,
            patch.object(
                ml_models.RemoteOpenLike, "_infer", side_effect=refuse
            ) as open_infer,
        ):
            date = shown(self.on_to_review(), "denial_date")
        self.assertEqual(date, letter_day().isoformat())
        for mock in (ask, triage, infer, infer_no_context, open_infer):
            with self.subTest(called=mock):
                mock.assert_not_called()


class FromYourLetterHintTest(ReviewPageTestBase):
    def test_the_hint_appears_only_under_the_field_filled_from_the_letter(self):
        self.without_a_stored_date(clear_letter())
        page = self.on_to_review()
        rows = [row for row in page.find_all("tr") if HINT in row.get_text(" ")]
        self.assertEqual(
            [row.find(attrs={"name": True})["name"] for row in rows], ["denial_date"]
        )

    def test_the_hint_is_read_out_with_its_field(self):
        self.without_a_stored_date(clear_letter())
        page = self.on_to_review()
        field = page.find(attrs={"name": "denial_date"})
        described_by = page.find(id=field["aria-describedby"])
        self.assertIn(HINT, described_by.get_text(" "))

    def test_no_hint_when_every_value_came_from_the_row(self):
        self.assertNotIn(HINT, self.on_to_review().get_text(" "))


def sent(page) -> dict[str, list[str]]:
    """What a form on the page sends as it stands: every named input and
    select, with only the ticked boxes and chosen options."""
    data: dict[str, list[str]] = {}
    for element in page.find_all(["input", "select"], attrs={"name": True}):
        name = element["name"]
        if element.name == "select":
            values = [o["value"] for o in element.find_all("option", selected=True)]
        elif element.get("type") == "checkbox":
            values = [element["value"]] if element.has_attr("checked") else []
        else:
            values = [element.get("value", "")]
        data.setdefault(name, []).extend(values)
    return data


class ReviewPageClearsWhatThePersonEmptiedTest(ReviewPageTestBase):
    """A box the page showed filled and the person emptied is cleared on the
    case, and so is a list whose ticked boxes they all unticked. A box the
    page showed empty is left alone, so a value extraction stored after the
    page was drawn survives a submit of the empty box."""

    def setUp(self):
        super().setUp()
        self.denial_type = models.DenialTypes.objects.create(
            name="Medical necessity (test)", regex="necessary", negative_regex=""
        )
        self.denial.procedure = "Continuous glucose monitor"
        self.denial.diagnosis = "Type 1 diabetes"
        self.denial.save()
        self.denial.denial_type.set([self.denial_type])

    def review_answers(self) -> dict[str, list[str]]:
        """What the review page's form sends untouched, on the way back."""
        form = self.back_to_review().find(
            "form", attrs={"action": reverse("find_next_steps_loading")}
        )
        return sent(form)

    def submit(self, data):
        """Continue from the review page, through the loading page that
        passes the answers on, the way the browser does."""
        loading = self.client.post(reverse("find_next_steps_loading"), data)
        self.assertEqual(loading.status_code, 200)
        relayed = BeautifulSoup(loading.content, "html.parser").find(
            "form", id="next-steps-form"
        )
        with patch(
            "fighthealthinsurance.common_view_logic.DenialCreatorHelper."
            "generate_appeal_questions",
            return_value=[],
        ):
            response = self.client.post(reverse("find_next_steps"), sent(relayed))
        self.assertEqual(response.status_code, 200)
        self.denial.refresh_from_db()

    def test_an_emptied_box_is_cleared_on_the_case(self):
        data = self.review_answers()
        data["diagnosis"] = [""]
        self.submit(data)
        self.assertIsNone(self.denial.diagnosis)

    def test_unticking_every_denial_type_clears_them(self):
        data = self.review_answers()
        data["denial_type"] = []
        self.submit(data)
        self.assertEqual(self.denial.denial_type.count(), 0)

    def test_unticking_every_plan_source_clears_them(self):
        data = self.review_answers()
        data["plan_source"] = []
        self.submit(data)
        self.assertEqual(self.denial.plan_source.count(), 0)

    def test_a_cleared_box_stays_cleared_through_a_form_error(self):
        data = self.review_answers()
        data["diagnosis"] = [""]
        data["denial_date"] = ["not a date"]
        refused = self.client.post(reverse("find_next_steps_loading"), data)
        self.assertTemplateUsed(refused, "categorize.html")
        again = sent(
            BeautifulSoup(refused.content, "html.parser").find(
                "form", attrs={"action": reverse("find_next_steps_loading")}
            )
        )
        again["denial_date"] = ["2024-03-01"]
        self.submit(again)
        self.assertIsNone(self.denial.diagnosis)

    def test_a_box_shown_empty_keeps_what_extraction_stored_since(self):
        self.denial.plan_id = None
        self.denial.save()
        data = self.review_answers()
        models.Denial.objects.filter(denial_id=self.denial.denial_id).update(
            plan_id="PLN-1"
        )
        self.submit(data)
        self.assertEqual(self.denial.plan_id, "PLN-1")
