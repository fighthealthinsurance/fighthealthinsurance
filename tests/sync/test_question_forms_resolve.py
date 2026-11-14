"""Every seeded denial type resolves to a question form, and every question
on that page is answerable.

Pinned regressions:

- ``fixtures/initial.yaml`` seeded the "Underpaid Out of Network" type with
  ``form: UnderpaidOutOfNetworkQuestions``, a class that did not exist.
  ``DenialTypes.get_form`` logged the failed lookup and returned None, so
  that denial type silently asked nothing at all;
- two question forms declared booleans with no label, so Django derived the
  label from the field name and a real prior-auth case was asked to tick
  "Emergency" and "Told prior auth not needed";
- a question was required on a page where every other question is
  optional.

The last two rules are checked against every form in the module rather than
only the ones a fixture reaches today, so a class becomes safe to seed
before anyone seeds it.

Also pins the contract the appeal builder relies on:
``AppealsBackendHelper._generate_appeals_body`` calls ``preface()``,
``main()`` and ``footer()`` on every form a denial type resolves to, inside
``if parsed.is_valid():`` and with no guard.

The ``form`` on a denial type is free text in admin, so it can name a class
that has since been removed. That resolves to no form, and the questions
page carries on without it.
"""

import inspect

import yaml
from django import forms
from django.forms.utils import pretty_name
from django.test import TestCase

from fighthealthinsurance.common_view_logic import FindNextStepsHelper
from fighthealthinsurance.forms import questions as question_forms
from fighthealthinsurance.models import Denial, DenialTypes

_FIXTURE = "fighthealthinsurance/fixtures/initial.yaml"

# Seeded denial types that ask nothing: either a grouping parent, or a type
# whose questions have never been written. Listed one by one so a NEW row
# arriving with no form has to be added here on purpose.
_TYPES_WITH_NO_QUESTIONS = {
    "Co-Ordination of Benefits",
    "Denied Out-Of-Network Provider",
    "Duplicate Claim",
    "In-Network Treated as Out of Network",
    "Insufficient Medical Information",
    "Insurance Company",
    "Limit for filing expired",
    "Other",
    "Post-Service",
    "Pre-Existing Condition",
    "Pre-Service",
    "Provider Bill (possible balance billing)",
    "STEP Therapy -- have to try cheaper options first",
}


def _question_form_classes():
    """Every Form subclass defined in forms/questions.py."""
    for name, obj in vars(question_forms).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, forms.Form)
            and obj is not forms.Form
            and obj.__module__ == question_forms.__name__
        ):
            yield name, obj


def _instantiate(form_class):
    """Build one, passing prof_pov only when the form takes it."""
    try:
        return form_class(prof_pov=False)
    except TypeError:
        return form_class()


class SeededDenialTypeFormTest(TestCase):
    fixtures = [_FIXTURE]

    def test_every_seeded_form_name_resolves_to_a_class(self):
        """A ``form:`` in the fixture naming a class that is not there is a
        denial type that asks nothing, and nothing says so at runtime."""
        with open(_FIXTURE) as fixture:
            rows = yaml.safe_load(fixture)

        named = {
            row["fields"]["form"]
            for row in rows
            if row["model"] == "fighthealthinsurance.denialtypes"
            and row["fields"].get("form")
        }
        self.assertTrue(named, "the fixture seeds no question forms at all")

        missing = sorted(name for name in named if not hasattr(question_forms, name))
        self.assertEqual(missing, [])

    def test_every_seeded_denial_type_asks_something_or_is_listed(self):
        for denial_type in DenialTypes.objects.all():
            with self.subTest(denial_type=denial_type.name):
                if denial_type.name in _TYPES_WITH_NO_QUESTIONS:
                    continue
                self.assertIsNotNone(
                    denial_type.get_form(),
                    f"{denial_type.name} resolves to no question form; add its "
                    f"form or list it in _TYPES_WITH_NO_QUESTIONS on purpose",
                )

    def test_every_reachable_form_answers_the_appeal_builder(self):
        """preface/main/footer, called unguarded on every resolved form."""
        seen = set()
        for denial_type in DenialTypes.objects.all():
            form_class = denial_type.get_form()
            if form_class is None or form_class in seen:
                continue
            seen.add(form_class)
            with self.subTest(form=form_class.__name__):
                form = form_class({})
                self.assertTrue(
                    form.is_valid(),
                    f"{form_class.__name__} rejects an empty submission: "
                    f"{form.errors}",
                )
                for method in ("preface", "main", "footer"):
                    self.assertIsInstance(
                        getattr(form, method)(),
                        list,
                        f"{form_class.__name__}.{method}() must return a list",
                    )

    def test_the_underpaid_out_of_network_type_asks_its_question(self):
        denial_type = DenialTypes.objects.get(name="Underpaid Out of Network")

        form_class = denial_type.get_form()

        self.assertIsNotNone(form_class)
        self.assertIn("why_need_out_of_network", _instantiate(form_class).fields)


class RemovedFormNameTest(TestCase):
    """A denial type can still name a question form that was removed."""

    _REMOVED = ("OutOfNetworkReimbursement", "BalanceBillQuestions", "StepTherapy")

    def _denial_type(self, name, form):
        return DenialTypes.objects.create(
            name=name, regex="zzz-never-matches", form=form
        )

    def test_a_removed_form_name_resolves_to_no_form(self):
        for name in self._REMOVED:
            with self.subTest(form=name):
                self.assertIsNone(self._denial_type(f"Uses {name}", name).get_form())

    def test_the_questions_page_skips_a_removed_form(self):
        denial = Denial.objects.create(
            denial_text="Step therapy is required before this medication.",
            hashed_email=Denial.get_hashed_email("rebecca.crumpler@example.com"),
        )
        denial.denial_type.add(
            self._denial_type("Uses StepTherapy", "StepTherapy"),
            self._denial_type("Medically Necessary", "MedicalNecessaryQuestions"),
        )

        built = FindNextStepsHelper._build_question_forms(denial)

        self.assertEqual(
            [type(form).__name__ for form in built], ["MedicalNecessaryQuestions"]
        )


class QuestionFieldTest(TestCase):
    def test_no_field_on_the_questions_page_is_required(self):
        for name, form_class in _question_form_classes():
            form = _instantiate(form_class)
            for field_name, field in form.fields.items():
                with self.subTest(form=name, field=field_name):
                    self.assertFalse(
                        field.required,
                        f"{name}.{field_name} is required; nothing on the "
                        f"questions page should block submitting",
                    )

    def test_no_checkbox_label_is_derived_from_its_field_name(self):
        for name, form_class in _question_form_classes():
            form = _instantiate(form_class)
            for field_name, field in form.fields.items():
                if not isinstance(field, forms.BooleanField):
                    continue
                with self.subTest(form=name, field=field_name):
                    self.assertIsNotNone(
                        field.label,
                        f"{name}.{field_name} has no label, so Django prints "
                        f"{pretty_name(field_name)!r} at the person",
                    )
                    self.assertNotEqual(field.label, pretty_name(field_name))


# The free-text boxes a denial type's form may still ask. Each is about the
# claim or the denial, or fills a template slot ({medical_reason}); the
# patient's history (age, symptoms, BMI, diagnosis, how long in treatment)
# is asked by the generated questions instead, so the page does not ask it
# twice. Listed by name so a new free-text box is a decision, not a drift.
_FREE_TEXT_ALLOWED = {
    "medical_reason",
    "why_need_out_of_network",
    "prior_auth_id",
    "alternate_insurance_details",
}


class StaticQuestionsLeaveHistoryToGeneratedQuestionsTest(TestCase):
    def test_free_text_boxes_are_only_the_listed_ones(self):
        for name, form_class in _question_form_classes():
            form = _instantiate(form_class)
            for field_name, field in form.fields.items():
                if isinstance(field, (forms.BooleanField, forms.ChoiceField)):
                    continue
                with self.subTest(form=name, field=field_name):
                    self.assertIn(
                        field_name,
                        _FREE_TEXT_ALLOWED,
                        f"{name}.{field_name} is a free-text question; patient "
                        f"history belongs to the generated questions",
                    )

    def test_an_answer_under_a_removed_field_name_is_ignored(self):
        """Older denials stored answers under fields since removed; their
        forms must still validate and build the appeal."""
        form = question_forms.MedicalNecessaryQuestions(
            {"age": "41", "medical_reason": "my surgeon says so"}
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("41", form.medical_context())


class PriorAuthQuestionsMainTest(TestCase):
    def _main(self, **data):
        form = question_forms.PriorAuthQuestions(data)
        self.assertTrue(form.is_valid(), form.errors)
        return form.main()

    def test_got_one_without_a_number_is_still_said(self):
        self.assertEqual(
            self._main(prior_auth_status="obtained"),
            ["Prior authorization was obtained."],
        )

    def test_got_one_with_a_number_says_the_number(self):
        self.assertEqual(
            self._main(prior_auth_status="obtained", prior_auth_id="PA-123"),
            ["Prior auth was obtained (id PA-123)"],
        )

    def test_a_number_left_from_got_one_is_not_said_for_another_answer(self):
        self.assertEqual(
            self._main(prior_auth_status="told_not_needed", prior_auth_id="PA-123"),
            ["It was communicated that prior authorization was not necessary."],
        )

    def test_asked_the_insurer_first(self):
        self.assertEqual(
            self._main(prior_auth_status="asked_insurer"),
            ["The insurance company was contacted before the service."],
        )

    def test_emergency(self):
        self.assertEqual(
            self._main(prior_auth_status="emergency"),
            ["This service was an emergency so prior auth could not be obtained."],
        )

    def test_did_not_get_one_argues_nothing(self):
        self.assertEqual(self._main(prior_auth_status="not_obtained"), [])

    def test_not_sure_argues_nothing(self):
        self.assertEqual(self._main(prior_auth_status="not_sure"), [])


class PriorAuthQuestionsOldAnswersTest(TestCase):
    """Denials answered before the one question still hold the old boxes."""

    def _main(self, **data):
        form = question_forms.PriorAuthQuestions(data)
        self.assertTrue(form.is_valid(), form.errors)
        return form.main()

    def test_an_old_obtained_box_is_still_said(self):
        self.assertEqual(
            self._main(prior_auth_obtained="on"),
            ["Prior authorization was obtained."],
        )

    def test_old_boxes_ticked_together_are_all_still_said(self):
        self.assertEqual(
            self._main(emergency="on", contact_insurance_before="on"),
            [
                "This service was an emergency so prior auth could not be obtained.",
                "The insurance company was contacted before the service.",
            ],
        )

    def test_an_old_yes_is_still_said(self):
        self.assertEqual(
            self._main(told_prior_auth_not_needed="Yes"),
            ["It was communicated that prior authorization was not necessary."],
        )

    def test_an_old_no_says_nothing(self):
        self.assertEqual(self._main(prior_auth_obtained="No"), [])

    def test_an_old_box_stored_unticked_says_nothing(self):
        self.assertEqual(self._main(emergency="False"), [])

    def test_the_new_answer_wins_over_an_old_box(self):
        self.assertEqual(
            self._main(prior_auth_status="not_obtained", emergency="on"),
            [],
        )


class InsuranceQuestionsFooterTest(TestCase):
    def _footer(self, prof_pov=False, **data):
        form = question_forms.InsuranceQuestions(data, prof_pov=prof_pov)
        self.assertTrue(form.is_valid(), form.errors)
        return form.footer()

    def test_post_service_footer_is_a_single_blank_paragraph(self):
        self.assertEqual(self._footer(), [""])

    def test_non_urgent_pre_service_footer_names_thirty_days(self):
        self.assertIn("30 days", self._footer(pre_service="yes", urgent="no")[1])

    def test_pre_service_with_urgency_not_sure_claims_no_deadline(self):
        """Not knowing whether it is urgent is not saying it isn't: the
        letter must not concede a month the person may not have."""
        self.assertEqual(self._footer(pre_service="yes", urgent="not_sure"), [""])

    def test_urgent_pre_service_footer_is_the_same_for_a_professional(self):
        footer = self._footer(urgent="yes", pre_service="yes")

        self.assertIn("four days", footer[1])
        self.assertIn("concurrent request of external review", footer[1])
        self.assertEqual(
            footer, self._footer(prof_pov=True, urgent="yes", pre_service="yes")
        )


class PreventiveCareIsNotOnlyScreeningTest(TestCase):
    """Vaccines, PrEP, contraception and well visits are preventive care too."""

    def test_the_reason_question_does_not_assume_a_screening(self):
        field = question_forms.PreventiveCareQuestions().fields["medical_reason"]

        self.assertNotIn("screening", f"{field.label} {field.help_text}".lower())

    def test_the_context_tells_the_letter_not_to_call_it_a_screening(self):
        form = question_forms.PreventiveCareQuestions({"medical_reason": "PrEP"})
        self.assertTrue(form.is_valid(), form.errors)

        context = form.medical_context()

        self.assertIn("not only screening", context)
        self.assertIn("unless it is one", context)
        self.assertIn("Why the patient needs this preventive care: PrEP.", context)


class FormularyAlternativesTest(TestCase):
    def test_tried_alternatives_does_not_claim_they_did_not_work(self):
        """The box covers alternatives that worked but could not be
        tolerated, so the letter must not say only that they failed."""
        form = question_forms.FormularyChangeQuestions({"tried_alternatives": "on"})
        self.assertTrue(form.is_valid(), form.errors)

        main_text = " ".join(form.main())

        self.assertIn("not effective or not tolerated", main_text)
        self.assertNotIn("did not work", main_text)
