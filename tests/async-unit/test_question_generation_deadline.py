"""The outer deadline on question generation, and what one run may overwrite.

Pinned regressions:

- ``generate_appeal_questions`` wrapped the helper in
  ``asyncio.wait_for(..., timeout=20)`` while the model phase alone buys a
  model window plus a longer overtime window on top of it. The outer timer
  fired first, cancelled the helper, threw away every model result that had
  arrived, and the page said there were a few more questions and showed
  none. Both numbers are read off a real run below, never retyped;
- a run that came back with nothing overwrote the questions another,
  overlapping run had already stored and rendered. Answers are filed
  against the question they were asked with, so replacing a rendered set
  strands them under field names nothing can resolve;
- the helper flattened "nothing usable arrived" to ``[]``, so an inner
  deadline or an exhausted set of backends reached the page as "No extra
  questions... We have what we need."

What the test measures is the ceiling on the MODEL phase, not on the whole
function: the pa_requirements lookup runs after it and carries no timer of
its own, being a local indexed query. That is why the deadline is set above
the model ceiling rather than equal to it.
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
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_the_deadline_sits_above_the_helpers_own_budget(self, _citations):
        """Both numbers are read off one real run, neither is retyped.

        The model window is whatever the helper hands
        best_within_timelimit; the overtime is what
        best_two_within_timelimit fills in for an omitted extended_timeout;
        the deadline is the timeout generate_appeal_questions actually
        hands asyncio.wait_for, not the constant it is supposed to read.
        """
        observed: Dict[str, Any] = {}
        real_wait_for = asyncio.wait_for

        async def record_the_deadline(awaitable, timeout=None, **kwargs):
            observed["deadline"] = timeout
            return await real_wait_for(awaitable, timeout=timeout, **kwargs)

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
            await Denial.objects.acreate(
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
                ), patch.object(
                    common_view_logic.asyncio, "wait_for", new=record_the_deadline
                ):
                    await DenialCreatorHelper.generate_appeal_questions(8206)
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

        deadline = observed.get("deadline")
        self.assertIsNotNone(
            deadline, "generate_appeal_questions never called asyncio.wait_for"
        )
        self.assertEqual(
            deadline,
            QUESTION_GENERATION_DEADLINE_SECONDS,
            "the wait_for timeout has to be the constant, not a literal",
        )
        self.assertGreater(deadline, model_window + overtime)

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
                # The claim returns what stands for the row: the other run's
                # set, not this run's empty one.
                self.assertEqual([tuple(row) for row in questions], [_FAST])
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
    def test_every_model_answering_empty_is_finished_not_unfinished(self, _citations):
        """The selector discards falsy results, so [] from both generators
        came back as None and the page said the run could not finish."""
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            MLAppealQuestionsHelper,
        )

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8206,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="knee pain",
            )
            try:
                with patch.object(
                    MLAppealQuestionsHelper,
                    "generate_generic_questions",
                    new=AsyncMock(return_value=[]),
                ), patch.object(
                    MLAppealQuestionsHelper,
                    "generate_specific_questions",
                    new=AsyncMock(return_value=[]),
                ), patch(
                    "fighthealthinsurance.pa_requirements.get_pa_questions_for_denial",
                    return_value=[],
                ):
                    questions = (
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
                self.assertEqual(questions, [])
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(stored.generated_questions, [])
            finally:
                await Denial.objects.filter(denial_id=8206).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_nobody_answering_is_unfinished_not_finished_empty(self, _citations):
        """The inner generators return None when their backends all failed;
        that must not be counted as an answer of nothing."""
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            MLAppealQuestionsHelper,
        )

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8207,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="knee pain",
            )
            try:
                with patch.object(
                    MLAppealQuestionsHelper,
                    "generate_generic_questions",
                    new=AsyncMock(return_value=None),
                ), patch.object(
                    MLAppealQuestionsHelper,
                    "generate_specific_questions",
                    new=AsyncMock(return_value=None),
                ), patch(
                    "fighthealthinsurance.pa_requirements.get_pa_questions_for_denial",
                    return_value=[],
                ):
                    questions = (
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
                self.assertIsNone(questions)
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertIsNone(stored.generated_questions)
            finally:
                await Denial.objects.filter(denial_id=8207).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_set_stored_for_a_corrected_procedure_is_regenerated_not_relabelled(
        self, _citations
    ):
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            MLAppealQuestionsHelper,
            questions_fingerprint,
        )

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8208,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied a CT scan.",
                procedure="CT scan",
                diagnosis="knee pain",
                generated_questions=[["Q about the MRI?", ""]],
                generated_questions_for=questions_fingerprint("MRI", "knee pain"),
            )
            try:
                with patch.object(
                    MLAppealQuestionsHelper,
                    "generate_generic_questions",
                    new=AsyncMock(return_value=[("Q about the CT?", "")]),
                ) as generic, patch.object(
                    MLAppealQuestionsHelper,
                    "generate_specific_questions",
                    new=AsyncMock(return_value=None),
                ), patch(
                    "fighthealthinsurance.pa_requirements.get_pa_questions_for_denial",
                    return_value=[],
                ):
                    questions = (
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
                generic.assert_awaited_once()
                self.assertEqual(
                    [tuple(q) for q in questions], [("Q about the CT?", "")]
                )
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(
                    stored.generated_questions_for,
                    questions_fingerprint("CT scan", "knee pain"),
                )
            finally:
                await Denial.objects.filter(denial_id=8208).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_second_run_does_not_replace_the_first_runs_questions(self, _citations):
        """The helper's own write site, against a stale in-memory row.

        Two runs overlap whenever somebody uses the loading page's Continue
        button. The second read an empty list before the first stored its
        set, and used to replace a set already rendered to the person --
        stranding the answers they were about to submit, which are filed
        against the question they were asked with.
        """

        async def second_run_finds_something_else(tasks, *args, **kwargs):
            for task in tasks:
                task.close()
            return [_SLOW]

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8207,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="back pain",
            )
            try:
                # The first run stored its set after this one read the row,
                # so the object in hand still says there are no questions.
                await Denial.objects.filter(denial_id=8207).aupdate(
                    generated_questions=[list(_FAST)]
                )
                with patch.object(
                    ml_appeal_questions_helper,
                    "best_within_timelimit",
                    new=second_run_finds_something_else,
                ), patch.object(
                    pa_requirements,
                    "get_pa_questions_for_denial",
                    new=lambda denial: [],
                ):
                    returned = (
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
                self.assertEqual([tuple(row) for row in returned], [_FAST])
                stored = await Denial.objects.aget(denial_id=8207)
                self.assertEqual(
                    [tuple(row) for row in stored.generated_questions], [_FAST]
                )
            finally:
                await Denial.objects.filter(denial_id=8207).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_the_outer_write_site_also_refuses_to_replace(self, _citations):
        """The same rule at generate_appeal_questions' own write."""

        async def helper(denial, speculative):
            return [_SLOW]

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8208,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                generated_questions=[list(_FAST)],
            )
            try:
                with patch(_HELPER, new=helper):
                    questions = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
                # What the page renders is what the row holds.
                self.assertEqual([tuple(row) for row in questions], [_FAST])
                stored = await Denial.objects.aget(denial_id=denial.denial_id)
                self.assertEqual(
                    [tuple(row) for row in stored.generated_questions], [_FAST]
                )
            finally:
                await Denial.objects.filter(denial_id=8208).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    def test_a_model_phase_that_finds_nothing_says_it_did_not_finish(self):
        """None has to survive the ML helper boundary.

        best_within_timelimit returns None when nothing usable arrived: its
        window closed empty, or no backend answered. Flattened to [], that
        reached the page as "No extra questions... We have what we need."
        """

        async def nothing_usable(tasks, *args, **kwargs):
            for task in tasks:
                task.close()
            return None

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8209,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="back pain",
            )
            try:
                with patch.object(
                    ml_appeal_questions_helper,
                    "best_within_timelimit",
                    new=nothing_usable,
                ), patch.object(
                    pa_requirements,
                    "get_pa_questions_for_denial",
                    new=lambda denial: [],
                ):
                    self.assertIsNone(
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
            finally:
                await Denial.objects.filter(denial_id=8209).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    def test_a_payer_rule_question_still_counts_as_finishing(self):
        """The PA lookup is deterministic: if it answered, the run finished."""

        async def nothing_usable(tasks, *args, **kwargs):
            for task in tasks:
                task.close()
            return None

        async def run():
            denial = await Denial.objects.acreate(
                denial_id=8210,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("deadline@example.com"),
                denial_text="Denied an MRI.",
                procedure="MRI",
                diagnosis="back pain",
            )
            try:
                with patch.object(
                    ml_appeal_questions_helper,
                    "best_within_timelimit",
                    new=nothing_usable,
                ), patch.object(
                    pa_requirements,
                    "get_pa_questions_for_denial",
                    new=lambda denial: [_FAST],
                ):
                    questions = (
                        await MLAppealQuestionsHelper.generate_questions_for_denial(
                            denial, speculative=False
                        )
                    )
                self.assertEqual([tuple(row) for row in questions], [_FAST])
            finally:
                await Denial.objects.filter(denial_id=8210).adelete()

        async_to_sync(run)()

    @pytest.mark.django_db
    @patch(_CITATIONS, new_callable=AsyncMock)
    def test_a_slow_model_does_not_lose_the_fast_models_questions(self, _citations):
        """One candidate times out; the other's questions still arrive and
        reach the row."""

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
