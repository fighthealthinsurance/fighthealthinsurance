"""The spend channel reaches the model work that an assistant denial starts:
the questions path, and generation itself, which marks the channel from the
Denial it loads rather than from a separate read."""

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.models import Denial


class QuestionsPathTest(TestCase):
    def test_question_generation_for_an_assistant_denial_spends_as_assistant(self):
        denial = Denial.objects.create(
            hashed_email="h",
            denial_text="The MRI was denied as not medically necessary.",
            procedure="MRI",
            diagnosis="back pain",
            channel="assistant",
        )
        seen = []

        async def fake_questions(**kwargs):
            seen.append(spend.current_use())
            return []

        with patch.object(
            MLAppealQuestionsHelper, "generate_generic_questions", fake_questions
        ), patch.object(
            MLAppealQuestionsHelper, "generate_specific_questions", fake_questions
        ):
            async_to_sync(MLAppealQuestionsHelper.generate_questions_for_denial)(
                denial, speculative=False
            )
        self.assertTrue(seen, "no question backend was asked")
        self.assertEqual(set(seen), {spend.ASSISTANT})
        self.assertEqual(spend.current_use(), spend.OTHER)


class GenerationChannelTest(TestCase):
    def test_generation_marks_the_channel_from_the_denial_it_loads(self):
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("person@example.com"),
            denial_text="The MRI was denied as not medically necessary.",
            channel="assistant",
        )
        marked = []

        def record(loaded):
            marked.append(loaded.denial_id)

        async def first_frame():
            agen = AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": denial.denial_id,
                    "email": "person@example.com",
                    "semi_sekret": denial.semi_sekret,
                }
            )
            try:
                return await agen.__anext__()
            finally:
                await agen.aclose()

        with patch.object(spend, "set_channel_of", record):
            async_to_sync(first_frame)()
        self.assertEqual(marked, [denial.denial_id])

    def test_there_is_no_separate_channel_read_that_could_fall_back_to_site(self):
        self.assertFalse(hasattr(AppealsBackendHelper, "_denial_channel"))

    def test_a_denial_that_cannot_be_loaded_marks_nothing(self):
        async def first_frame():
            agen = AppealsBackendHelper.generate_appeals(
                {"denial_id": 424242, "email": "nobody@example.com", "semi_sekret": "x"}
            )
            try:
                return await agen.__anext__()
            finally:
                await agen.aclose()

        marked = []
        with patch.object(spend, "set_channel_of", lambda d: marked.append(d)):
            try:
                async_to_sync(first_frame)()
            except Exception:
                pass
        self.assertEqual(marked, [])
        self.assertEqual(spend.current_use(), spend.OTHER)
