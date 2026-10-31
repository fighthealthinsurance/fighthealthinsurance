"""The follow-up round between the questions page and the appeal
(ml/followup_questions.py), as the person goes through it.

Our own model is stubbed at ml_appeal_questions_helper._healthy_internal_model (its yes/no and the
follow-ups it writes); the rest of the round runs for real.
"""

import os
from unittest import skipIf
from unittest.mock import AsyncMock, MagicMock, patch

from bs4 import BeautifulSoup
from django.test import override_settings
from django.urls import reverse

from fighthealthinsurance import settings as fhi_settings
from fighthealthinsurance.common_view_logic import FindNextStepsHelper
from fighthealthinsurance.denial_context import question_field_name
from fighthealthinsurance.models import DenialTypes
from tests.back_links import back_link
from tests.sync.test_questions_step import _Q1, QuestionsStepTestBase

# Our own model as the follow-up round picks it: the strongest that looks
# healthy (ml_appeal_questions_helper._healthy_internal_model).
_BEST_INTERNAL = "fighthealthinsurance.ml.ml_appeal_questions_helper._healthy_internal_model"
_FOR_DENIAL = (
    "fighthealthinsurance.ml.ml_appeal_questions_helper.MLAppealQuestionsHelper."
    "followup_questions_for_denial"
)
FOLLOW_UP = "How many weeks of physical therapy did the patient complete?"
# The Jev half pinned off too: these tests never send anything to TypeSafe.
ROUND_ON = dict(
    FHI_FOLLOWUP_QUESTIONS_ENABLED=True, TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED=False
)


def _hidden(response) -> dict[str, list[str]]:
    form = BeautifulSoup(response.content, "html.parser").find(
        "form", action=reverse("generate_appeal")
    )
    assert form is not None, "no form posts to generate_appeal"
    values: dict[str, list[str]] = {}
    for tag in form.find_all("input", type="hidden"):
        values.setdefault(tag["name"], []).append(tag.get("value", ""))
    return values


@skipIf(
    "FHI_FOLLOWUP_QUESTIONS_ENABLED" in os.environ
    or "TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED" in os.environ,
    "a flag is set in this environment",
)
class ProductionDefaultTest(QuestionsStepTestBase):
    def test_the_round_is_on_and_its_jev_half_off_in_production(self):
        self.assertTrue(fhi_settings.Prod.FHI_FOLLOWUP_QUESTIONS_ENABLED)
        self.assertFalse(fhi_settings.Prod.TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED)


@override_settings(FHI_FOLLOWUP_QUESTIONS_ENABLED=False)
class RoundOffTest(QuestionsStepTestBase):
    def test_with_the_round_off_the_appeal_page_comes_straight_away(self):
        with patch(_BEST_INTERNAL) as router:
            response = self._generate_appeal(
                **{question_field_name(_Q1): "About four months"}
            )
        self.assertTemplateUsed(response, "appeals.html")
        router.assert_not_called()
        self.assertEqual(FindNextStepsHelper.answers_action(self.denial), "generate_appeal")


@override_settings(**ROUND_ON)
class FollowUpRoundTest(QuestionsStepTestBase):
    def setUp(self):
        super().setUp()
        # Our own model says NO (not enough) and writes one follow-up.
        self.model = MagicMock()
        self.model.answers_are_enough = AsyncMock(return_value=False)
        self.model.get_followup_questions = AsyncMock(return_value=[(FOLLOW_UP, "")])
        router = patch(_BEST_INTERNAL, return_value=self.model)
        router.start()
        self.addCleanup(router.stop)

    def _answer_first_round(self, **extra):
        answers = {"questionnaire": "1", question_field_name(_Q1): "About four months"}
        answers.update(extra)
        return self._generate_appeal(**answers)

    def _post_follow_up_page(self, first_round, **answers):
        """Submit the follow-up page as the browser would."""
        payload = {
            key: values[0]
            for key, values in _hidden(first_round).items()
            if key != "csrfmiddlewaretoken"
        }
        payload.update(answers)
        return self.client.post(reverse("generate_appeal"), payload)

    def test_the_questions_page_posts_through_the_loading_page_until_checked(self):
        self.assertEqual(
            FindNextStepsHelper.answers_action(self.denial), "generate_appeal_loading"
        )
        self._answer_first_round()
        self.denial.refresh_from_db()
        self.assertEqual(FindNextStepsHelper.answers_action(self.denial), "generate_appeal")

    def test_the_loading_page_forwards_the_answers_to_the_appeal(self):
        payload = self._ref()
        payload[question_field_name(_Q1)] = "About four months"
        response = self.client.post(reverse("generate_appeal_loading"), payload)
        self.assertTemplateUsed(response, "find_next_steps_loading.html")
        self.assertEqual(_hidden(response)[question_field_name(_Q1)], ["About four months"])

    def test_the_follow_up_page_carries_what_the_questions_page_posted(self):
        response = self._answer_first_round()
        self.assertTemplateUsed(response, "followup_questions.html")
        self.assertContains(response, FOLLOW_UP)
        hidden = _hidden(response)
        self.assertEqual(hidden[question_field_name(_Q1)], ["About four months"])
        self.assertEqual(hidden["denial_id"], [str(self.denial.denial_id)])
        self.assertNotIn(question_field_name(FOLLOW_UP), hidden)

    def test_answering_the_follow_up_files_both_rounds_and_back_shows_them(self):
        first = self._answer_first_round()
        response = self._post_follow_up_page(
            first, **{question_field_name(FOLLOW_UP): "Six weeks"}
        )
        self.assertTemplateUsed(response, "appeals.html")
        self.assertEqual(self._qa()[_Q1], "About four months")
        self.assertEqual(self._qa()[FOLLOW_UP], "Six weeks")
        self.model.get_followup_questions.assert_awaited_once()
        back = self.client.get(
            back_link(self.client, "find_next_steps", self.denial, self.email)
        )
        field = back.context["combined"].fields[question_field_name(FOLLOW_UP)]
        self.assertEqual(field.initial, "Six weeks")

    def test_a_double_submit_sees_the_same_follow_up_page(self):
        self._answer_first_round()
        again = self._answer_first_round()
        self.assertTemplateUsed(again, "followup_questions.html")
        self.model.get_followup_questions.assert_awaited_once()

    def test_our_own_model_saying_enough_goes_on_to_the_appeal(self):
        self.model.answers_are_enough.return_value = True
        self.assertTemplateUsed(self._answer_first_round(), "appeals.html")
        self.model.get_followup_questions.assert_not_called()

    def test_a_check_that_breaks_goes_on_to_the_appeal(self):
        with patch(_FOR_DENIAL, new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = self._answer_first_round()
        self.assertTemplateUsed(response, "appeals.html")

    def test_a_ticked_box_survives_the_follow_up_round(self):
        """An unticked box posts nothing, so one the follow-up page dropped
        would be saved as unticked (GenerateAppeal._unticked_checkbox_answers)."""
        self.denial.denial_type.set(DenialTypes.objects.filter(pk=3))
        first = self._answer_first_round(emergency="on")
        self._post_follow_up_page(first, **{question_field_name(FOLLOW_UP): "Six weeks"})
        self.assertEqual(self._qa()["emergency"], "on")
