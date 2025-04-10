from typing import List, Tuple, Optional, cast, Callable, Coroutine, Any
from loguru import logger
import asyncio
import time
from fighthealthinsurance.models import Denial, GenericQuestionGeneration
from fighthealthinsurance.utils import best_within_timelimit
from fighthealthinsurance.ml.ml_router import ml_router
import re


class MLAppealQuestionsHelper:

    @staticmethod
    async def generate_generic_questions(
        procedure: Optional[str], diagnosis: Optional[str], timeout: int = 90
    ) -> List[Tuple[str, str]]:
        """
        Generate generic appeal questions based only on procedure and diagnosis.
        These are cached for reuse across multiple patients with the same procedure/diagnosis.

        Args:
            procedure: The medical procedure
            diagnosis: The medical diagnosis
            timeout: Timeout for the ML model call in seconds

        Returns:
            A list of (question, answer) tuples.
        """
        models_to_try = set(
            ml_router.full_qa_backends(True) + ml_router.partial_qa_backends()
        )

        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = procedure.strip().lower() if procedure else ""
        diagnosis = diagnosis.strip().lower() if diagnosis else ""

        # Skip if we don't have enough information
        if not procedure or not diagnosis:
            logger.debug(f"Missing procedure or diagnosis for generic questions")
            return []

        # Check for existing cached questions first
        try:
            cached = await GenericQuestionGeneration.objects.filter(
                procedure=procedure, diagnosis=diagnosis
            ).afirst()

            if cached:
                logger.debug(
                    f"Found cached generic questions for {procedure}/{diagnosis}"
                )
                return cast(List[Tuple[str, str]], cached.generated_questions)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error fetching cached generic questions: {e}"
            )

        # If no cached questions exist, generate them
        model_timeout = max(1, timeout - 5)  # Subtract 5 seconds for processing

        raw_questions_awaitables = []

        for model in models_to_try:
            raw_questions_awaitables.append(
                model.get_appeal_questions(
                    denial_text=None,
                    procedure=procedure,
                    diagnosis=diagnosis,
                )
            )

        questions = await best_within_timelimit(
            raw_questions_awaitables,
            score_fn=MLAppealQuestionsHelper.make_score_fn(lambda x: 1),
            timeout=model_timeout,
        )

        # If we have questions, cache them for future use
        if questions:
            try:
                await GenericQuestionGeneration.objects.acreate(
                    procedure=procedure,
                    diagnosis=diagnosis,
                    generated_questions=questions,
                )
                logger.debug(f"Cached generic questions for {procedure}/{diagnosis}")
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Error caching generic questions: {e}"
                )
        return questions if questions else []

    @staticmethod
    def make_score_fn(factor: Callable[[Coroutine[Any, Any, Any]], int]):
        def score_fn(result: Optional[List[Tuple[str, str]]], awaitable):
            my_factor = factor(awaitable)
            # Score the result based on a mixture of source and length
            if result is None:
                return 0
            try:
                if result:
                    # The result should already be in the correct format for question-answer tuples
                    question_score = len(result)
                    # More than 4 is bad news
                    if question_score > 4:
                        question_score = 1
                    return my_factor * question_score
                return 0
            except Exception as e:
                logger.debug(f"Failed to parse: {e}")
                return 0

        return score_fn

    @staticmethod
    async def generate_specific_questions(
        denial_text: Optional[str],
        patient_context: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        timeout: int = 90,
        use_external: bool = False,
    ) -> List[Tuple[str, str]]:
        """
        Generate specific appeal questions based on denial text, patient info, procedure, and diagnosis.
        These are not cached between patients.

        Args:
            denial_text: The text of the denial
            patient_context: Information about the patient
            procedure: The medical procedure
            diagnosis: The medical diagnosis
            timeout: Timeout for the ML model call in seconds
            use_external: Whether to use external models

        Returns:
            A list of (question, answer) tuples.
        """
        models_to_try = set(ml_router.full_qa_backends(use_external))

        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = procedure.strip().lower() if procedure else ""
        diagnosis = diagnosis.strip().lower() if diagnosis else ""

        # If no cached questions exist, generate them
        model_timeout = max(1, timeout - 5)  # Subtract 5 seconds for processing

        raw_questions_awaitables = []

        for model in models_to_try:
            raw_questions_awaitables.append(
                model.get_appeal_questions(
                    denial_text=denial_text,
                    patient_context=patient_context,
                    procedure=procedure,
                    diagnosis=diagnosis,
                )
            )

        questions = await best_within_timelimit(
            raw_questions_awaitables,
            score_fn=MLAppealQuestionsHelper.make_score_fn(lambda x: 1),
            timeout=model_timeout,
        )
        return questions if questions else []

    @staticmethod
    async def generate_questions_for_denial(
        denial: Denial, speculative: bool
    ) -> List[Tuple[str, str]]:
        """
        Generate appeal questions for a given denial. Uses speculative/candidate generation if nothing
        changed.

        Args:
            denial: The denial object for which to generate questions.
            speculative: Whether this is a speculative generation (candidate) or final.

        Returns:
            A list of (question, answer) tuples.
        """
        questions: List[Tuple[str, str]] = []

        # Check if candidate questions exist and the diagnosis/procedure has not changed
        if (
            denial.candidate_procedure == denial.procedure
            and denial.candidate_diagnosis == denial.diagnosis
            and denial.candidate_generated_questions
            and len(denial.candidate_generated_questions) > 0
        ):
            logger.debug(f"Using candidate questions for denial {denial.denial_id}")
            questions = cast(
                List[Tuple[str, str]], denial.candidate_generated_questions
            )
        elif denial.generated_questions and len(denial.generated_questions) > 0:
            logger.debug(f"Using cached questions for denial {denial.denial_id}")
            questions = cast(List[Tuple[str, str]], denial.generated_questions)
        else:
            # Setup timeout based on whether this is speculative or not
            timeout = 60 if speculative else 45

            # Subtract 5 seconds to ensure proper processing time
            model_timeout = max(1, timeout - 5)
            no_context_awaitable = MLAppealQuestionsHelper.generate_generic_questions(
                procedure=denial.procedure,
                diagnosis=denial.diagnosis,
                timeout=model_timeout,
            )
            context_awaitable = MLAppealQuestionsHelper.generate_specific_questions(
                denial_text=denial.denial_text,
                patient_context=denial.health_history,  # Using health_history as patient_context
                procedure=denial.procedure,
                diagnosis=denial.diagnosis,
                timeout=model_timeout,
                use_external=denial.use_external,
            )

            # Bias for context
            def is_with_context(x):
                if x == context_awaitable:
                    return 2
                return 1

            result = await best_within_timelimit(
                [no_context_awaitable, context_awaitable],
                score_fn=MLAppealQuestionsHelper.make_score_fn(is_with_context),
                timeout=model_timeout,
            )

            # Ensure we have a valid list of questions
            if result is not None:
                questions = result

        # Update the denial with the result
        if questions and len(questions) > 0:
            logger.debug(
                f"Generated {len(questions)} questions for denial {denial.denial_id}"
            )
            qs = Denial.objects.filter(denial_id=denial.denial_id)
            if speculative:
                await qs.aupdate(candidate_generated_questions=questions)
            else:
                await qs.aupdate(generated_questions=questions)
            return questions
        else:
            return []
