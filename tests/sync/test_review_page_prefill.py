"""The review page starts from what we already know about the case.

Details (categorize.html) is the step where the person checks and corrects
what we gathered. Every field the denial row holds a value for starts with
that value, whichever way the person arrives: from reading the letter, or
back from the questions step. The denial date is also read from the letter,
on this server, when the row has none and the letter states it plainly, and a
value read from the letter says so under its field.
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
# The date stands alone above the greeting, where a letter puts its own.
CLEAR_LETTER = (
    "Example Health Plan\n"
    "PO Box 100\n"
    "\n"
    "March 4, 2024\n"
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
    """What the review page shows in a field: an input's value, or the
    values of a select's chosen options."""
    element = page.find(attrs={"name": name})
    assert element is not None, f"the review page has no {name} field"
    if element.name == "select":
        return [option["value"] for option in element.find_all("option", selected=True)]
    return element.get("value")


class ReviewPageTestBase(BackLinkReferenceTestBase):
    def setUp(self):
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
        self.denial.denial_text = CLEAR_LETTER
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
        self.assertEqual(shown(self.back_to_review(), "denial_date"), "2024-03-01")


class ReviewPageReadsTheLetterDateTest(ReviewPageTestBase):
    def test_a_letter_with_a_clear_date_fills_an_empty_date(self):
        self.without_a_stored_date(CLEAR_LETTER)
        self.assertEqual(shown(self.back_to_review(), "denial_date"), "2024-03-04")

    def test_the_letter_date_is_filled_after_the_letter_is_read_too(self):
        self.without_a_stored_date(CLEAR_LETTER)
        self.assertEqual(shown(self.on_to_review(), "denial_date"), "2024-03-04")

    def test_a_letter_with_an_ambiguous_date_leaves_the_date_blank(self):
        self.without_a_stored_date(AMBIGUOUS_LETTER)
        self.assertIsNone(shown(self.back_to_review(), "denial_date"))

    def test_reading_the_date_asks_no_outside_model(self):
        self.without_a_stored_date(CLEAR_LETTER)
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
            date = shown(self.back_to_review(), "denial_date")
        self.assertEqual(date, "2024-03-04")
        for mock in (ask, triage, infer, infer_no_context, open_infer):
            with self.subTest(called=mock):
                mock.assert_not_called()


class FromYourLetterHintTest(ReviewPageTestBase):
    def test_the_hint_appears_only_under_the_field_filled_from_the_letter(self):
        self.without_a_stored_date(CLEAR_LETTER)
        page = self.back_to_review()
        rows = [row for row in page.find_all("tr") if HINT in row.get_text(" ")]
        self.assertEqual(
            [row.find(attrs={"name": True})["name"] for row in rows], ["denial_date"]
        )

    def test_the_hint_is_read_out_with_its_field(self):
        self.without_a_stored_date(CLEAR_LETTER)
        page = self.back_to_review()
        field = page.find(attrs={"name": "denial_date"})
        described_by = page.find(id=field["aria-describedby"])
        self.assertIn(HINT, described_by.get_text(" "))

    def test_no_hint_when_every_value_came_from_the_row(self):
        self.assertNotIn(HINT, self.back_to_review().get_text(" "))
