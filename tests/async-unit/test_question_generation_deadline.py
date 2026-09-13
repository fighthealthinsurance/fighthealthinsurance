"""The outer deadline on question generation must not outlive its purpose.

Pinned regression: ``generate_appeal_questions`` wrapped the helper in
``asyncio.wait_for(..., timeout=20)`` while
``MLAppealQuestionsHelper.generate_questions_for_denial`` can spend far
longer than that. Its nominal budget variable is 45 (``timeout = 60 if
speculative else 45``), and it spends that as ``model_timeout = 45 - 5`` =
40 seconds of model window plus, only when nothing usable landed in that
window, ``best_within_timelimit``'s default overtime of
``min(max(2 * 40, 60), FHI_ML_EXTENDED_WAIT)`` = 80 seconds. So the helper
can run for 120 seconds; 45 and 80 are not two windows that add up. The
outer 20 second timer fired before the helper's FIRST window closed,
cancelled it, threw away every model result that had arrived, and returned
an empty list. The page then said there were a few more questions and
showed none.

Also pinned here: a run that comes back empty must not overwrite questions
another run already stored. The loading page offers Continue at 20 seconds
while this deadline is 130, so a second, overlapping run is reachable for
about 110 seconds, and it used to be able to replace a full question list
with ``[]``.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.common_view_logic import (
    QUESTION_GENERATION_DEADLINE_SECONDS,
    DenialCreatorHelper,
)
from fighthealthinsurance.models import Denial
from fighthealthinsurance.utils import best_within_timelimit

_HELPER = (
    "fighthealthinsurance.common_view_logic.MLAppealQuestionsHelper."
    "generate_questions_for_denial"
)
_CITATIONS = (
    "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool"
)

_FAST = ("What did your doctor say?", "")
_SLOW = ("What did the second model ask?", "")


class QuestionGenerationDeadlineTest(TestCase):
    # The helper's own ceiling, composed the way the helper composes it:
    # a nominal budget of 45, spent as a (45 - 5) second model window and,
    # when that window yields nothing usable, an overtime window of
    # min(max(2 * 40, 60), 300).
    HELPER_NOMINAL_BUDGET = 45
    HELPER_MODEL_WINDOW = HELPER_NOMINAL_BUDGET - 5
    HELPER_OVERTIME = min(max(2 * HELPER_MODEL_WINDOW, 60), 300)

    def test_the_deadline_sits_above_the_helpers_own_budget(self):
        """40s model window + 80s overtime = 120s, all read off the helper."""
        self.assertEqual(self.HELPER_MODEL_WINDOW, 40)
        self.assertEqual(self.HELPER_OVERTIME, 80)
        self.assertGreater(
            QUESTION_GENERATION_DEADLINE_SECONDS,
            self.HELPER_MODEL_WINDOW + self.HELPER_OVERTIME,
        )

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_an_empty_run_does_not_wipe_questions_another_run_stored(
        self, _citations
    ):
        """Two runs overlap whenever someone uses the loading page's 20
        second Continue button against this 130 second deadline. The one
        that finishes empty must leave the other one's questions alone."""

        async def helper(denial, speculative):
            return []

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8205,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
            )
            try:
                # The first run has already stored real questions.
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    generated_questions=[list(_FAST)]
                )
                with patch(_HELPER, new=helper):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                # [] is still the honest answer to "what did THIS run find"...
                self.assertEqual(questions, [])
                # ...but the row keeps what the other run found.
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(
                    [tuple(row) for row in stored.generated_questions], [_FAST]
                )
            finally:
                await Denial.objects.filter(denial_id=8205).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_slow_model_does_not_lose_the_fast_models_questions(self, _citations):
        """One candidate times out; the other's questions still arrive.

        The windows are scaled down but keep the real relationship: the
        helper's own window (0.3s) sits under the outer deadline (5s), which
        is what the 20s-over-45s pairing got backwards.
        """

        async def helper(denial, speculative):
            async def fast():
                return [_FAST]

            async def slow():
                await asyncio.sleep(30)
                return [_SLOW]

            return (
                await best_within_timelimit(
                    [fast(), slow()],
                    score_fn=lambda result, awaitable: 1,
                    timeout=0.3,
                    extended_timeout=0,
                )
                or []
            )

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8201,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
            )
            try:
                with patch(_HELPER, new=helper), patch.object(
                    common_view_logic, "QUESTION_GENERATION_DEADLINE_SECONDS", 5
                ):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                self.assertEqual(questions, [_FAST])
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(
                    [tuple(row) for row in stored.generated_questions], [_FAST]
                )
            finally:
                await Denial.objects.filter(denial_id=8201).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_deadline_under_the_helper_reports_that_it_did_not_finish(
        self, _citations
    ):
        """None, not []: the caller has to be able to tell those apart."""

        async def helper(denial, speculative):
            await asyncio.sleep(30)
            return [_SLOW]

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8202,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
            )
            try:
                with patch(_HELPER, new=helper), patch.object(
                    common_view_logic, "QUESTION_GENERATION_DEADLINE_SECONDS", 0.05
                ):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                self.assertIsNone(questions)
            finally:
                await Denial.objects.filter(denial_id=8202).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_run_that_does_not_finish_falls_back_to_matching_candidates(
        self, _citations
    ):
        """The speculative set was generated for this same service."""

        async def helper(denial, speculative):
            await asyncio.sleep(30)
            return [_SLOW]

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8203,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="back pain",
                candidate_procedure="MRI",
                candidate_diagnosis="back pain",
                candidate_generated_questions=[list(_FAST)],
            )
            try:
                with patch(_HELPER, new=helper), patch.object(
                    common_view_logic, "QUESTION_GENERATION_DEADLINE_SECONDS", 0.05
                ):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                self.assertEqual([tuple(row) for row in questions], [_FAST])
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(
                    [tuple(row) for row in stored.generated_questions], [_FAST]
                )
            finally:
                await Denial.objects.filter(denial_id=8203).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_candidates_for_a_different_service_are_not_promoted(self, _citations):
        """The helper's own rule: candidates count only while dx/px match."""

        async def helper(denial, speculative):
            await asyncio.sleep(30)
            return [_SLOW]

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8204,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="back pain",
                candidate_procedure="physical therapy",
                candidate_diagnosis="back pain",
                candidate_generated_questions=[list(_FAST)],
            )
            try:
                with patch(_HELPER, new=helper), patch.object(
                    common_view_logic, "QUESTION_GENERATION_DEADLINE_SECONDS", 0.05
                ):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                self.assertIsNone(questions)
            finally:
                await Denial.objects.filter(denial_id=8204).adelete()

        async_to_sync(run)()
