"""Every seeded denial type resolves to a question form, and every question
on that page is answerable.

Pinned regressions:

- ``fixtures/initial.yaml`` seeded the "Underpaid Out of Network" type with
  ``form: UnderpaidOutOfNetworkQuestions``, a class that did not exist.
  ``DenialTypes.get_form`` logged the failed lookup and returned None, so
  that denial type silently asked nothing at all;
- two question forms declared booleans with no label, so Django derived the
  label from the field name. Only one of the two is reachable: a real
  prior-auth case (fixture pk 3) was asked to tick "Emergency", "Contact
  insurance before" and "Told prior auth not needed". BalanceBillQuestions,
  which is where "Match eob" lives, is named by no fixture row, so that one
  was never put in front of anyone; it is labelled anyway because the test
  below holds every form in the module, not just today's reachable ones;
- ``OutOfNetworkReimbursement.why_need_out_of_network`` was required on a
  page where every other question is optional. Nothing in the fixture names
  that class, so no patient has ever been shown the field; making it
  optional is prophylactic, and the test below is what holds the whole
  module to the rule rather than just the classes reachable today.

Also pins the contract the appeal builder relies on:
``AppealsBackendHelper._generate_appeals_body`` calls ``preface()``,
``main()`` and ``footer()`` on every form a denial type resolves to, inside
``if parsed.is_valid():`` and with no guard. Two classes in this module are
plain ``forms.Form`` and define only some of the three, so seeding a denial
type against one of those would raise AttributeError mid-generation.
"""

import inspect

import yaml
from django import forms
from django.forms.utils import pretty_name
from django.test import TestCase

from fighthealthinsurance.forms import questions as question_forms
from fighthealthinsurance.models import DenialTypes

_FIXTURE = "fighthealthinsurance/fixtures/initial.yaml"

# Seeded denial types that ask nothing: either a grouping parent, or a type
# whose questions have never been written. Listed one by one so a NEW row
# arriving with no form has to be added here on purpose rather than joining
# a silent majority.
#
# Two of these have a plausible class sitting unused in forms/questions.py --
# "Provider Bill (possible balance billing)" against BalanceBillQuestions and
# "STEP Therapy" against StepTherapy -- but nothing in the fixture points at
# either, so today they ask nothing. Wiring them is a seeded-data decision,
# not a code one.
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
