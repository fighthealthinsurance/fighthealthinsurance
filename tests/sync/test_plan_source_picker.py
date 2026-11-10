"""How the person gets their insurance, asked in two steps, and whether they
have any other insurance.

The review page used to show every PlanSource row as one long list of tick
boxes. It now asks the broad answer first (through a job, Medicare,
Medicaid, ...) and, under an answer that covers several kinds, which kind
(form_utils.PlanSourcePicker). It also asks whether the person has other
insurance, which can pay what the denying plan won't.
"""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.http import QueryDict
from django.template.loader import render_to_string
from django.test import TestCase

from fighthealthinsurance.common_view_logic import FindNextStepsHelper
from fighthealthinsurance.form_utils import PLAN_SOURCE_GROUPS
from fighthealthinsurance.forms import PostInferedForm
from fighthealthinsurance.models import (
    OTHER_INSURANCE_NO,
    OTHER_INSURANCE_YES,
    Denial,
    PlanSource,
)

PLAN_SOURCE_FIXTURE = "fighthealthinsurance/fixtures/plan_source.yaml"
REF = {"denial_id": "1", "email": "someone@example.com", "semi_sekret": "sekret"}
_QUESTIONS = (
    "fighthealthinsurance.common_view_logic.DenialCreatorHelper."
    "generate_appeal_questions"
)


def source(name):
    """The fixture row by name; some fixture names end in spaces."""
    return next(s for s in PlanSource.objects.all() if s.name.strip() == name)


def post(**fields):
    data = QueryDict(mutable=True)
    data.update(REF)
    for key, value in fields.items():
        if isinstance(value, list):
            data.setlist(key, value)
        else:
            data[key] = value
    return PostInferedForm(data=data)


def page(form):
    html = render_to_string(
        "categorize.html", {"post_infered_form": form, "back_url": "/back/"}
    )
    return BeautifulSoup(html, "html.parser")


class PlanSourcePickerRendersTest(TestCase):
    fixtures = [PLAN_SOURCE_FIXTURE]

    def test_the_first_step_is_a_short_list_of_broad_answers(self):
        firsts = page(PostInferedForm()).find_all(
            "input", attrs={"name": "plan_source_group"}
        )
        self.assertEqual(
            [box["value"] for box in firsts], [g[0] for g in PLAN_SOURCE_GROUPS]
        )

    def test_every_plan_source_row_is_offered_once(self):
        picker = page(PostInferedForm()).find(id="id_plan_source")
        offered = [
            box.get("data-plan-source") or box["value"]
            for box in picker.find_all("input")
            if box.get("data-plan-source") or box["name"] == "plan_source"
        ]
        self.assertEqual(
            sorted(offered),
            sorted(str(pk) for pk in PlanSource.objects.values_list("pk", flat=True)),
        )

    def test_employer_kinds_are_asked_under_through_a_job(self):
        picker = page(PostInferedForm()).find(id="id_plan_source")
        job = picker.find(id="id_plan_source_group_job").parent
        kinds = [box["value"] for box in job.fieldset.find_all("input")]
        self.assertIn(str(source("Employer -- Private").pk), kinds)
        self.assertIn(str(source("Union").pk), kinds)
        self.assertNotIn(str(source("Medicaid").pk), kinds)

    def test_a_stored_kind_starts_with_its_answer_and_kind_ticked(self):
        union = source("Union")
        picker = page(PostInferedForm(initial={"plan_source": [union]})).find(
            id="id_plan_source"
        )
        self.assertEqual(
            [box["id"] for box in picker.find_all("input", checked=True)],
            ["id_plan_source_group_job", "id_plan_source_job_4"],
        )

    def test_a_row_no_group_names_is_still_offered(self):
        extra = PlanSource.objects.create(name="Tribal health program")
        picker = page(PostInferedForm()).find(id="id_plan_source")
        box = picker.find(attrs={"data-plan-source": str(extra.pk)})
        self.assertEqual(box["name"], "plan_source_group")


class PlanSourcePickerReadsThePostTest(TestCase):
    fixtures = [PLAN_SOURCE_FIXTURE]

    def test_a_kind_under_its_answer_is_the_plan_source(self):
        form = post(
            plan_source_group="job",
            plan_source=str(source("Employer -- State Government").pk),
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            list(form.cleaned_data["plan_source"]),
            [source("Employer -- State Government")],
        )

    def test_an_answer_of_one_kind_needs_no_follow_up(self):
        form = post(plan_source_group="medicaid")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(list(form.cleaned_data["plan_source"]), [source("Medicaid")])

    def test_a_kind_left_from_another_answer_is_not_used(self):
        # Picked a private employer, then switched to Medicare: the employer
        # kind is still ticked, out of sight, and must not be saved.
        form = post(
            plan_source_group="medicare",
            plan_source=str(source("Employer -- Private").pk),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Which kind of Medicare?", str(form.errors["plan_source"]))

    def test_an_answer_with_no_kind_asks_for_one(self):
        form = post(plan_source_group="job")
        self.assertIn("What kind of employer?", str(form.errors["plan_source"]))

    def test_the_page_drawn_again_keeps_the_answer_whose_kind_is_asked(self):
        # Otherwise the follow-up the error asks about is hidden, and a
        # second Continue would drop the answer without a word.
        form = post(plan_source_group="medicare")
        self.assertFalse(form.is_valid())
        picker = page(form).find(id="id_plan_source")
        self.assertEqual(
            [box["id"] for box in picker.find_all("input", checked=True)],
            ["id_plan_source_group_medicare"],
        )

    def test_a_post_without_the_first_answer_is_read_as_before(self):
        # The REST API and older pages send plan_source values directly.
        two = [source("Medicare Advantage"), source("Medicaid")]
        form = post(plan_source=[str(s.pk) for s in two])
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.cleaned_data["plan_source"]), set(two))

    def test_other_insurance_kind_is_read_only_with_a_yes(self):
        form = post(other_insurance=OTHER_INSURANCE_NO, other_plan_source_group="job")
        self.assertTrue(form.is_valid(), form.errors)

    def test_other_insurance_is_one_plan_source(self):
        form = post(
            other_insurance=OTHER_INSURANCE_YES, other_plan_source_group="medicaid"
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["other_plan_source"], source("Medicaid"))


class OtherInsuranceIsSavedTest(TestCase):
    fixtures = [PLAN_SOURCE_FIXTURE]

    def setUp(self):
        self.email = "other-insurance@example.com"
        self.denial = Denial.objects.create(
            denial_id=9101,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(self.email),
            denial_text="Denied an MRI.",
        )

    def find_next_steps(self, **fields):
        kwargs = dict(
            denial_id=self.denial.denial_id,
            email=self.email,
            semi_sekret="sekret",
            procedure="",
            diagnosis="",
            insurance_company="",
            plan_id="",
            claim_id="",
            denial_type=None,
        )
        kwargs.update(fields)
        with patch(_QUESTIONS):
            result = FindNextStepsHelper.find_next_steps(**kwargs)
        self.denial.refresh_from_db()
        return result

    def test_a_yes_saves_the_other_plan(self):
        self.find_next_steps(
            other_insurance=OTHER_INSURANCE_YES,
            other_plan_source=source("Medicaid"),
            other_insurance_company="Medi-Cal",
        )
        self.assertEqual(
            (
                self.denial.other_insurance,
                self.denial.other_plan_source,
                self.denial.other_insurance_company,
            ),
            (OTHER_INSURANCE_YES, source("Medicaid"), "Medi-Cal"),
        )

    def test_a_company_name_at_the_length_limit_is_saved(self):
        self.find_next_steps(
            other_insurance=OTHER_INSURANCE_YES, other_insurance_company="c" * 300
        )
        self.assertEqual(self.denial.other_insurance_company, "c" * 300)

    def test_a_no_clears_the_other_plan(self):
        self.find_next_steps(
            other_insurance=OTHER_INSURANCE_YES,
            other_plan_source=source("Union"),
            other_insurance_company="Local 99",
        )
        self.find_next_steps(other_insurance=OTHER_INSURANCE_NO)
        self.assertEqual(
            (self.denial.other_plan_source, self.denial.other_insurance_company),
            (None, None),
        )

    def test_a_question_left_alone_keeps_the_answer(self):
        self.find_next_steps(other_insurance=OTHER_INSURANCE_YES)
        self.find_next_steps(other_insurance="")
        self.assertEqual(self.denial.other_insurance, OTHER_INSURANCE_YES)


class OtherInsuranceNextStepTest(TestCase):
    fixtures = [PLAN_SOURCE_FIXTURE]

    def note(self, plan_source=None, **fields):
        denial = Denial.objects.create(
            denial_text="denied",
            hashed_email=Denial.get_hashed_email("other-note@example.com"),
            **fields,
        )
        if plan_source:
            denial.plan_source.set([source(plan_source)])
        rows = FindNextStepsHelper._get_outside_help_details(denial)
        found = [row for row in rows if "other insurance" in row[0]]
        return found[0][0] if found else None

    def test_no_note_without_other_insurance(self):
        self.assertIsNone(self.note(other_insurance=OTHER_INSURANCE_NO))

    def test_a_yes_suggests_sending_the_claim_to_the_other_plan(self):
        text = self.note(other_insurance=OTHER_INSURANCE_YES)
        self.assertIn("coordination of benefits", text)

    def test_the_other_company_is_named_and_escaped(self):
        text = self.note(
            other_insurance=OTHER_INSURANCE_YES,
            other_insurance_company="<b>Acme</b>",
        )
        self.assertIn("&lt;b&gt;Acme&lt;/b&gt;", text)

    def test_medicaid_as_the_other_plan_says_it_pays_last(self):
        text = self.note(
            other_insurance=OTHER_INSURANCE_YES,
            other_plan_source=source("Medicaid"),
        )
        self.assertIn("Ask your provider to bill Medicaid", text)

    def test_a_medicaid_denial_with_other_insurance_points_to_that_plan(self):
        text = self.note(plan_source="Medicaid", other_insurance=OTHER_INSURANCE_YES)
        self.assertIn("your other plan should pay for first", text)
