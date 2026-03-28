"""Tests for the journey documentation feature."""

import unittest
from unittest.mock import patch

from fighthealthinsurance.forms.questions import JourneyDocumentationQuestions
from fighthealthinsurance.ml.ml_journey_helper import _score_journey_questions


class TestJourneyDocumentationQuestions(unittest.TestCase):
    """Tests for JourneyDocumentationQuestions form."""

    def _make_form(self, data=None, prof_pov=False):
        form = JourneyDocumentationQuestions(data=data or {}, prof_pov=prof_pov)
        form.is_valid()
        return form

    def test_empty_form_returns_empty_context(self):
        form = self._make_form()
        self.assertEqual(form.medical_context(), "")

    def test_empty_form_returns_empty_main(self):
        form = self._make_form()
        self.assertEqual(form.main(), [])

    def test_medical_context_with_all_fields(self):
        form = self._make_form(
            {
                "prior_medications": "Metformin, Ozempic",
                "test_results": "A1C 9.2%",
                "treatment_timeline": "3 years",
                "why_this_treatment": "Prior meds failed",
            }
        )
        ctx = form.medical_context()
        self.assertIn("Metformin, Ozempic", ctx)
        self.assertIn("A1C 9.2%", ctx)
        self.assertIn("3 years", ctx)
        self.assertIn("Prior meds failed", ctx)

    def test_medical_context_with_partial_fields(self):
        form = self._make_form({"prior_medications": "Aspirin"})
        ctx = form.medical_context()
        self.assertIn("Aspirin", ctx)
        self.assertNotIn("test results", ctx.lower())

    def test_main_patient_pov(self):
        form = self._make_form(
            {"prior_medications": "Humira", "test_results": "CRP elevated"},
            prof_pov=False,
        )
        parts = form.main()
        self.assertEqual(len(parts), 2)
        self.assertIn("I have previously tried", parts[0])
        self.assertIn("My test results", parts[1])

    def test_main_professional_pov(self):
        form = self._make_form(
            {"prior_medications": "Humira", "test_results": "CRP elevated"},
            prof_pov=True,
        )
        parts = form.main()
        self.assertEqual(len(parts), 2)
        self.assertIn("The patient has previously tried", parts[0])
        self.assertIn("Clinical evidence", parts[1])

    def test_main_all_fields_professional(self):
        form = self._make_form(
            {
                "prior_medications": "Drug A",
                "test_results": "Lab B",
                "treatment_timeline": "2 years",
                "why_this_treatment": "Guidelines recommend it",
            },
            prof_pov=True,
        )
        parts = form.main()
        self.assertEqual(len(parts), 4)
        self.assertIn("alternative options", parts[0])
        self.assertIn("Clinical evidence", parts[1])
        self.assertIn("medical necessity", parts[2])
        self.assertIn("clinical rationale", parts[3].lower())


class TestScoreJourneyQuestions(unittest.TestCase):
    """Tests for the scoring function used by best_within_timelimit."""

    def test_none_returns_zero(self):
        self.assertEqual(_score_journey_questions(None, None), 0)

    def test_empty_list_returns_zero(self):
        self.assertEqual(_score_journey_questions([], None), 0)

    def test_two_questions(self):
        result = [("q1", "a1"), ("q2", "a2")]
        self.assertEqual(_score_journey_questions(result, None), 30)

    def test_three_questions_peak(self):
        result = [("q1", "a1"), ("q2", "a2"), ("q3", "a3")]
        self.assertEqual(_score_journey_questions(result, None), 45)

    def test_four_questions(self):
        result = [("q", "a")] * 4
        self.assertEqual(_score_journey_questions(result, None), 60)

    def test_five_questions_decreases(self):
        result = [("q", "a")] * 5
        self.assertEqual(_score_journey_questions(result, None), 45)

    def test_six_questions(self):
        result = [("q", "a")] * 6
        self.assertEqual(_score_journey_questions(result, None), 30)

    def test_seven_plus_penalized(self):
        result = [("q", "a")] * 7
        self.assertEqual(_score_journey_questions(result, None), 1)

    def test_four_beats_six(self):
        """4 questions (sweet spot) should score higher than 6."""
        four = _score_journey_questions([("q", "a")] * 4, None)
        six = _score_journey_questions([("q", "a")] * 6, None)
        self.assertGreater(four, six)


class TestJourneyDocumentationHelper(unittest.TestCase):
    """Tests for JourneyDocumentationHelper static methods."""

    def test_format_documentation_items_with_hints(self):
        from fighthealthinsurance.ml.ml_journey_helper import (
            JourneyDocumentationHelper,
        )

        items = [
            {"label": "Prior meds", "prompt_hint": "Ask about side effects"},
            {"label": "Test results"},
        ]
        result = JourneyDocumentationHelper._format_documentation_items(items)
        self.assertIn("- Prior meds: Ask about side effects", result)
        self.assertIn("- Test results", result)
        # Item without hint should NOT have a colon suffix
        self.assertNotIn("Test results:", result)

    def test_format_documentation_items_empty(self):
        from fighthealthinsurance.ml.ml_journey_helper import (
            JourneyDocumentationHelper,
        )

        result = JourneyDocumentationHelper._format_documentation_items([])
        self.assertEqual(result, "")


class TestMicrositeJourneyDocumentation(unittest.TestCase):
    """Tests for Microsite journey documentation validation and prompt generation."""

    def test_microsite_validates_journey_items(self):
        from fighthealthinsurance.microsites import Microsite, MicrositeValidationError

        # Missing required 'label' key
        with self.assertRaises(MicrositeValidationError):
            Microsite(
                {
                    "slug": "test",
                    "title": "Test",
                    "default_procedure": "Test",
                    "tagline": "t",
                    "hero_h1": "t",
                    "hero_subhead": "t",
                    "intro": "t",
                    "how_we_help": "t",
                    "cta": "t",
                    "journey_documentation_items": [{"category": "test"}],
                }
            )

    def test_microsite_journey_prompt_empty_when_no_items(self):
        from fighthealthinsurance.microsites import Microsite

        ms = Microsite(
            {
                "slug": "test",
                "title": "Test",
                "default_procedure": "Test Proc",
                "tagline": "t",
                "hero_h1": "t",
                "hero_subhead": "t",
                "intro": "t",
                "how_we_help": "t",
                "cta": "t",
            }
        )
        self.assertEqual(ms.get_journey_documentation_prompt(), "")

    def test_microsite_journey_prompt_with_items(self):
        from fighthealthinsurance.microsites import Microsite

        ms = Microsite(
            {
                "slug": "test",
                "title": "Test",
                "default_procedure": "MRI Scan",
                "tagline": "t",
                "hero_h1": "t",
                "hero_subhead": "t",
                "intro": "t",
                "how_we_help": "t",
                "cta": "t",
                "journey_documentation_items": [
                    {
                        "category": "test_results",
                        "label": "Prior imaging",
                        "prompt_hint": "Ask about X-rays",
                    },
                    {"category": "history", "label": "Symptom history"},
                ],
            }
        )
        prompt = ms.get_journey_documentation_prompt()
        self.assertIn("MRI Scan", prompt)
        self.assertIn("Prior imaging", prompt)
        self.assertIn("Symptom history", prompt)
        self.assertIn("Proactively ask", prompt)
