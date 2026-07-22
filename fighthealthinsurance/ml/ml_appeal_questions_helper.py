import asyncio
import datetime
import re
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence, Tuple, cast

from channels.db import database_sync_to_async
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial, GenericQuestionGeneration
from fighthealthinsurance.utils import best_within_timelimit

# Maps a get_appeal_questions coroutine to the originating model's quality score
QuestionsCoroutine = Coroutine[Any, Any, List[Tuple[str, str]]]
AwaitableQualityMap = Dict[QuestionsCoroutine, int]

# Refresh cached generic (procedure/diagnosis-only) questions older than this.
# Mirrors the generic-citation TTL in ml_citations_helper: the content moves
# slowly but shouldn't be immortal. Rows with updated_at=None (from before the
# TTL field existed) are treated as stale.
_GENERIC_QUESTIONS_CACHE_TTL = datetime.timedelta(days=30)


def _questions_worth_caching(questions: Sequence[Sequence[Any]]) -> bool:
    """Gate the shared generic-question cache on a "reasonable" ML result.

    A junk generation must not get pinned in the cache, where it would be
    served to every future patient with the same procedure/diagnosis until the
    TTL expires. Requirements: 1-10 entries, every entry a (question, answer)
    pair whose question is a non-trivial string, and at least one question
    that actually reads as a question (ends with "?"). This gates only the
    cache write — the caller still receives whatever was generated.
    """
    if not questions or len(questions) > 10:
        return False
    texts: List[str] = []
    for entry in questions:
        try:
            question_text = entry[0]
        except (TypeError, IndexError, KeyError):
            return False
        if not isinstance(question_text, str) or len(question_text.strip()) < 10:
            return False
        texts.append(question_text.strip())
    return any(text.endswith("?") for text in texts)


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
        models_to_try = list(
            dict.fromkeys(
                ml_router.partial_qa_backends() + ml_router.full_qa_backends()
            )
        )

        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = procedure.strip().lower() if procedure else ""
        diagnosis = diagnosis.strip().lower() if diagnosis else ""

        # Skip if we don't have enough information
        if procedure == "" and diagnosis == "":
            logger.debug(f"Missing procedure and diagnosis for generic questions")
            return []

        # Check for existing cached questions first. Reads target the current
        # cache version only — parked/deprecated rows (version=-pk from the
        # dedupe migration) are ignored — and .afirst() (not .aget()) means a
        # pathological duplicate can never raise MultipleObjectsReturned.
        stale_questions: Optional[List[Tuple[str, str]]] = None
        try:
            cached = await GenericQuestionGeneration.objects.filter(
                procedure=procedure,
                diagnosis=diagnosis,
                version=GenericQuestionGeneration.CURRENT_VERSION,
            ).afirst()

            if cached is not None:
                fresh = (
                    cached.updated_at is not None
                    and (timezone.now() - cached.updated_at)
                    < _GENERIC_QUESTIONS_CACHE_TTL
                )
                if fresh:
                    logger.debug(
                        f"Found cached generic questions for {procedure}/{diagnosis}"
                    )
                    return cast(List[Tuple[str, str]], cached.generated_questions)
                # Past the TTL (or predates it): regenerate, but keep the old
                # content as a fallback in case regeneration comes back empty.
                stale_questions = cast(
                    List[Tuple[str, str]], cached.generated_questions
                )
                logger.debug(
                    f"Cached generic questions for {procedure}/{diagnosis} are "
                    "stale; regenerating"
                )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error fetching cached generic questions: {e}"
            )

        # If no cached questions exist, generate them
        model_timeout = max(1, timeout - 5)  # Subtract 5 seconds for processing

        raw_questions_awaitables: List[QuestionsCoroutine] = []
        model_quality_map: AwaitableQualityMap = {}

        # Patient-context-free by construction: the models receive ONLY
        # procedure/diagnosis (denial_text=None, no patient_context), and
        # answers are stripped below before anything reaches the shared
        # cross-patient cache.
        for model in models_to_try:
            awaitable = model.get_appeal_questions(
                denial_text=None,
                procedure=procedure,
                diagnosis=diagnosis,
            )
            raw_questions_awaitables.append(awaitable)
            model_quality_map[awaitable] = model.quality()

        logger.debug(
            f"Using models {models_to_try} to create {raw_questions_awaitables}"
        )
        questions: Optional[List[Tuple[str, str]]]
        try:
            questions = await best_within_timelimit(
                raw_questions_awaitables,
                score_fn=MLAppealQuestionsHelper.make_score_fn(
                    lambda x: 1, model_quality=model_quality_map
                ),
                timeout=model_timeout,
            )
        except Exception as e:
            # best_within_timelimit raises when nothing scores above zero
            # (every backend returned empty or failed). Treat that as "no
            # questions" so the stale-cache fallback below can still serve.
            #
            # Deliberate contract change: callers now get [] (or stale cache)
            # instead of a propagated exception on total generation failure.
            # For the prior-auth flow that means a request proceeds to
            # "questions_asked" with just the boilerplate health-history
            # question rather than sticking in "initial" — degraded but
            # usable beats silently stuck.
            logger.debug(f"Generic question generation produced nothing: {e}")
            questions = None
        # Generic should not have answers
        if questions:
            questions_without_answers = list(map(lambda xy: (xy[0], ""), questions))
            questions = questions_without_answers

        if stale_questions and not _questions_worth_caching(questions or []):
            # Regeneration produced nothing worth keeping (empty OR junk the
            # cache guard would reject); serve the known-good stale cached
            # questions instead. The row's timestamp is left untouched so the
            # next request retries generation. Same guard as the cache write,
            # so "bad result" has one definition.
            logger.debug(
                f"Regeneration for {procedure}/{diagnosis} not worth keeping; "
                "serving stale cached questions"
            )
            return list(stale_questions)

        # Cache the questions, but only when the generation looks reasonable —
        # junk must not get pinned for every future patient with this
        # procedure/diagnosis.
        if _questions_worth_caching(questions or []):
            try:
                # Upsert against the (procedure, diagnosis, version)
                # UniqueConstraint (generic_q_proc_diag_ver_uniq), so
                # concurrent cache misses collapse to a single row instead of
                # the old acreate() inserting duplicates.
                await GenericQuestionGeneration.objects.aupdate_or_create(
                    procedure=procedure,
                    diagnosis=diagnosis,
                    version=GenericQuestionGeneration.CURRENT_VERSION,
                    defaults={
                        "generated_questions": questions,
                        # Explicit timestamp: update_or_create defaults don't
                        # trigger auto_now, and the TTL reads key off this.
                        "updated_at": timezone.now(),
                    },
                )
                logger.debug(f"Cached generic questions for {procedure}/{diagnosis}")
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Error caching generic questions: {e}"
                )
        return questions if questions else []

    @staticmethod
    def make_score_fn(
        factor: Callable[[Coroutine[Any, Any, Any]], int],
        model_quality: Optional[AwaitableQualityMap] = None,
    ):
        def score_fn(result: Optional[List[Tuple[str, str]]], awaitable):
            my_factor = factor(awaitable)
            if result is None:
                return 0
            try:
                if not result:
                    return 0

                n = len(result)

                # Ideal: 2-3 questions. 1 is ok, 4 is decent, >4 is bad
                if 2 <= n <= 3:
                    question_score = n * 2  # bonus for ideal count
                elif n == 1:
                    question_score = 1
                elif n == 4:
                    question_score = 3
                else:  # > 4
                    question_score = 1

                # Bonus for well-formed questions (actually end with "?")
                valid_questions = sum(1 for q, _ in result if q.strip().endswith("?"))
                if valid_questions == n:
                    question_score += 1

                # Light model quality bonus (quality/100, so ~1-2 points)
                quality_bonus = 0.0
                if model_quality and awaitable in model_quality:
                    quality_bonus = model_quality[awaitable] / 100.0

                return my_factor * question_score + quality_bonus
            except Exception as e:
                logger.debug(f"Failed to score: {e}")
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

        if (not denial_text or denial_text == "") and (
            not patient_context or patient_context == ""
        ):
            logger.debug(f"All patient specific context is unset, quick return.")
            return []

        # If no cached questions exist, generate them
        model_timeout = max(1, timeout - 5)  # Subtract 5 seconds for processing

        raw_questions_awaitables: List[QuestionsCoroutine] = []
        model_quality_map: AwaitableQualityMap = {}

        for model in models_to_try:
            awaitable = model.get_appeal_questions(
                denial_text=denial_text,
                patient_context=patient_context,
                procedure=procedure,
                diagnosis=diagnosis,
            )
            raw_questions_awaitables.append(awaitable)
            model_quality_map[awaitable] = model.quality()

        questions = await best_within_timelimit(
            raw_questions_awaitables,
            score_fn=MLAppealQuestionsHelper.make_score_fn(
                lambda x: 1, model_quality=model_quality_map
            ),
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
            logger.debug(f"Generating new questions for denial {denial.denial_id}")
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
                logger.debug(f"{x} is my result")
                if x == context_awaitable:
                    logger.debug(f"{x} in context")
                    return 2
                logger.debug(f"{x} not in context")
                return 1

            result = await best_within_timelimit(
                [no_context_awaitable, context_awaitable],
                score_fn=MLAppealQuestionsHelper.make_score_fn(is_with_context),
                timeout=model_timeout,
            )

            # Ensure we have a valid list of questions
            if result is not None:
                questions = result

        # Merge PA-aware questions derived from the indexed payer rules.
        # These come from a deterministic lookup (no model call) and target
        # the criteria the carrier itself published for the procedure code,
        # so they're a free quality boost over generic LLM questions.
        try:
            from fighthealthinsurance.pa_requirements import (
                get_pa_questions_for_denial,
            )

            pa_questions = await database_sync_to_async(get_pa_questions_for_denial)(
                denial
            )
        except Exception as e:
            logger.opt(exception=True).debug(
                f"PA-aware question lookup failed for denial {denial.denial_id}: {e}"
            )
            pa_questions = []

        if pa_questions:
            existing = {q.strip().lower() for q, _ in questions}
            for question, default in pa_questions:
                if question.strip().lower() not in existing:
                    questions.append((question, default))
                    existing.add(question.strip().lower())

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
