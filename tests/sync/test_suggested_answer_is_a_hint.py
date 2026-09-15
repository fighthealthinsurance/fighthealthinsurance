"""The model's suggestion is offered beside the box, never inside it."""

from django.test import TestCase

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import Denial


class SuggestedAnswerIsAHintNotAnAnswerTest(TestCase):
    QUESTION = "Reason for elevated risk requiring this screening."
    SUGGESTION = "The patient has a documented family history of the condition."

    def _forms(self, answers=None):
        denial = Denial.objects.create(
            denial_text="a denial",
            hashed_email="x",
            generated_questions=[[self.QUESTION, self.SUGGESTION]],
        )
        return common_view_logic.FindNextStepsHelper._build_question_forms(
            denial, answers
        )

    def test_the_box_starts_empty_rather_than_holding_the_models_words(self) -> None:
        forms = self._forms()
        field = list(forms[-1].fields.values())[-1]
        self.assertIn(
            field.initial,
            (None, ""),
            "the suggestion is prefilled into the answer box, so pressing Next "
            "without reading it stores the model's words as the patient's own",
        )

    def test_the_suggestion_is_still_offered_beside_the_box(self) -> None:
        forms = self._forms()
        field = list(forms[-1].fields.values())[-1]
        self.assertIn(self.SUGGESTION, field.help_text)

    def test_an_answer_the_person_gave_still_fills_the_box(self) -> None:
        forms = self._forms({self.QUESTION: "Because my mother had it."})
        field = list(forms[-1].fields.values())[-1]
        self.assertEqual(field.initial, "Because my mother had it.")
