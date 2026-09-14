"""The outer deadline on question generation must not outlive its purpose.

Pinned regression: ``generate_appeal_questions`` wrapped the helper in
``asyncio.wait_for(..., timeout=20)`` while
``MLAppealQuestionsHelper.generate_questions_for_denial`` can spend far
longer than that. Its nominal budget variable is 45 (``timeout = 60 if
speculative else 45``), and it spends that as ``model_timeout = 45 - 5`` =
40 seconds of model window plus, only when nothing usable landed in that
window, ``best_within_timelimit``'s default overtime of
``min(max(2 * 40, 60), FHI_ML_EXTENDED_WAIT)`` = 80 seconds. So the model
phase can run for 120 seconds; 45 and 80 are not two windows that add up.
The outer 20 second timer fired before the helper's FIRST window closed,
cancelled it, threw away every model result that had arrived, and returned
an empty list. The page then said there were a few more questions and
showed none.

120 is the ceiling on the MODEL phase, not on the whole function: the
pa_requirements lookup runs after it and carries no timer of its own, so
it is bounded by the database rather than by anything this test can
read. That lookup is a local indexed query, which is why the deadline is
set against the model phase, and it is why the deadline is set ABOVE the
ceiling rather than equal to it.

Also pinned here: a run that comes back empty must not overwrite questions
another run already stored. The loading page offers Continue at 20 seconds
while this deadline is 130, so a second, overlapping run is reachable for
about 110 seconds, and it used to be able to replace a full question list
with ``[]``.
"""

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance import common_view_logic, pa_requirements
from fighthealthinsurance.common_view_logic import (
    QUESTION_GENERATION_DEADLINE_SECONDS,
    DenialCreatorHelper,
)
from fighthealthinsurance.ml import ml_appeal_questions_helper
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.models import Denial
from fighthealthinsurance.utils import best_within_timelimit, default_extended_timeout

_HELPER = (
    "fighthealthinsurance.common_view_logic.MLAppealQuestionsHelper."
    "generate_questions_for_denial"
)
_CITATIONS = "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool"

_FAST = ("What did your doctor say?", "")
_SLOW = ("What did the second model ask?", "")


class QuestionGenerationDeadlineTest(TestCase):
    @pytest.mark.django_db
    def test_the_deadline_sits_above_the_helpers_own_budget(self):
        """Run the helper and watch what it actually buys, 40s + 80s.

        An earlier version of this test retyped the composition (45, -5,
        *2, 60, 300) as literals in the class body and asserted 130 > 120
        against its own arithmetic. Setting the helper's budget to 200,
        which puts the real ceiling at 495 seconds against this unchanged
        130 second deadline, left the whole file green. So the numbers are
        read here and nowhere typed: the model window is whatever the
        helper hands best_within_timelimit, and the overtime is what
        best_two_within_timelimit fills in for an omitted extended_timeout.
        """
        observed: Dict[str, Any] = {}

        async def record_the_window(tasks, *args, **kwargs):
            observed["timeout"] = kwargs.get(
                "timeout", args[1] if len(args) > 1 else None
            )
            observed["extended_timeout"] = kwargs.get(
                "extended_timeout", args[2] if len(args) > 2 else None
            )
            # The helper builds both model coroutines before this call, and
            # nothing here awaits them; closing them keeps the run clean.
            for task in tasks:
                task.close()
            return None

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8206,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
            )
            try:
                with patch.object(
                    ml_appeal_questions_helper,
                    "best_within_timelimit",
                    new=record_the_window,
                ), patch.object(
                    pa_requirements,
                    "get_pa_questions_for_denial",
                    new=lambda denial: [],
                ):
                    await MLAppealQuestionsHelper.generate_questions_for_denial(
                        denial, speculative=False
                    )
            finally:
                await Denial.objects.filter(denial_id=8206).adelete()

        async_to_sync(run)()

        model_window = observed["timeout"]
        self.assertIsNotNone(
            model_window, "the helper never called best_within_timelimit"
        )
        # The helper passes no extended_timeout, so the overtime is the
        # default best_two_within_timelimit computes from the same window.
        overtime = observed["extended_timeout"]
        if overtime is None:
            overtime = default_extended_timeout(model_window)

        # Tripwire on the numbers the module docstring and the commit
        # message quote, so a budget change has to update the prose too.
        self.assertEqual(model_window, 40)
        self.assertEqual(overtime, 80)
        self.assertGreater(
            QUESTION_GENERATION_DEADLINE_SECONDS,
            model_window + overtime,
        )

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_an_empty_run_does_not_wipe_questions_another_run_stored(self, _citations):
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
