"""The questions page says what a question means, not why we ask it.

Most help text under the denial-type questions explained why an answer
helps the appeal ("Under the ACA and recent guidance...", "The prudent
layperson standard..."). That reasoning is already argued in each form's
``main()``, and the person does not need it to answer, so it moved onto the
field with ``form_utils.why_we_ask`` and the page shows it collapsed under a
"Why we ask" disclosure. The formulary and colonoscopy questions were about
140-185 words with it; these pin them under 60.
"""

import inspect

from bs4 import BeautifulSoup
from django.test import TestCase

from fighthealthinsurance.form_utils import (
    YesNoNotSureField,
    field_why_we_ask,
    magic_combined_form,
)
from fighthealthinsurance.forms import questions as question_forms

# Words the visible page may show for one denial type's questions.
_WORD_BUDGET = 60

# Phrases that argue for the appeal rather than explain the question. Each
# came out of a help text into a "Why we ask".
_REASONING_PHRASES = (
    "ACA",
    "prudent layperson",
    "supports",
    "medical necessity",
    "medically necessary",
    "insurers require",
    "insurance may",
    "clinical utility",
    "strong evidence",
    "affect coverage",
    "helps determine",
    "helps establish",
    "may warrant",
)


def _question_form_classes():
    return [
        cls
        for _, cls in inspect.getmembers(question_forms, inspect.isclass)
        if issubclass(cls, question_forms.InsuranceQuestions)
    ]


def _rendered(form_class) -> BeautifulSoup:
    combined = magic_combined_form([form_class()], {})
    return BeautifulSoup(combined.as_table(), "html.parser")


def _visible_words(form_class) -> int:
    """Words a person sees before opening any "Why we ask": labels, help
    text, choices and the disclosure's own summary line.

    Not the Yes / No / Not sure under each yes/no question: the same three
    answers every time, read once and not again for each question.
    """
    soup = _rendered(form_class)
    for name, field in form_class.base_fields.items():
        if isinstance(field, YesNoNotSureField):
            soup.find(id=f"id_{name}").decompose()
    for details in soup.find_all("details"):
        for child in details.find_all(recursive=False):
            if child.name != "summary":
                child.decompose()
    return len(soup.get_text(" ").split())


class QuestionPagesAreShortTest(TestCase):
    def test_formulary_questions_fit_the_word_budget(self):
        words = _visible_words(question_forms.FormularyChangeQuestions)
        self.assertLess(words, _WORD_BUDGET)

    def test_colonoscopy_questions_fit_the_word_budget(self):
        words = _visible_words(question_forms.ColonoscopyQuestions)
        self.assertLess(words, _WORD_BUDGET)


class WhyWeAskIsCollapsedTest(TestCase):
    def test_reason_renders_inside_a_closed_disclosure(self):
        soup = _rendered(question_forms.ColonoscopyQuestions)
        reason = field_why_we_ask(
            question_forms.ColonoscopyQuestions.base_fields["polyp_removal"]
        )
        details = [
            d for d in soup.find_all("details", class_="fhi-why") if reason in d.text
        ]
        self.assertEqual(len(details), 1)
        self.assertFalse(details[0].has_attr("open"))
        self.assertEqual(details[0].summary.get_text(strip=True), "Why we ask")

    def test_field_without_a_reason_gets_no_disclosure(self):
        soup = _rendered(question_forms.FormularyChangeQuestions)
        self.assertEqual(soup.find_all("details"), [])

    def test_reason_survives_the_form_copying_its_fields(self):
        form = question_forms.EmergencyServicesQuestions()
        reason = field_why_we_ask(form.fields["denied_based_on_final_diagnosis"])
        self.assertIn("prudent layperson", reason)


class HelpTextSaysWhatTheQuestionMeansTest(TestCase):
    def test_no_help_text_argues_for_the_appeal(self):
        offenders = []
        for cls in _question_form_classes():
            for name, field in cls.base_fields.items():
                help_text = str(field.help_text or "")
                for phrase in _REASONING_PHRASES:
                    if phrase.lower() in help_text.lower():
                        offenders.append(f"{cls.__name__}.{name}: {phrase!r}")
        self.assertEqual(offenders, [], "Move the reasoning to why_we_ask()")

    def test_every_reason_is_a_plain_sentence(self):
        for cls in _question_form_classes():
            for name, field in cls.base_fields.items():
                reason = field_why_we_ask(field)
                if reason:
                    with self.subTest(form=cls.__name__, field=name):
                        self.assertTrue(reason.endswith("."))
                        self.assertNotIn("<", reason)
