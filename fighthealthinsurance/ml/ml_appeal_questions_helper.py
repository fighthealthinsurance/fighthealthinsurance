import asyncio
import re
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, cast

from channels.db import database_sync_to_async
from django.db import transaction
from loguru import logger

from fighthealthinsurance.denial_history_consent import (
    ahistory_may_be_used,
    still_allowed,
)
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial, GenericQuestionGeneration
from fighthealthinsurance.utils import best_within_timelimit

# Maps a get_appeal_questions coroutine to the originating model's quality score
QuestionsCoroutine = Coroutine[Any, Any, List[Tuple[str, str]]]
AwaitableQualityMap = Dict[QuestionsCoroutine, int]


def questions_fingerprint(procedure: Optional[str], diagnosis: Optional[str]) -> str:
    """What a set of questions was generated for."""
    import hashlib

    parts = ((procedure or "").strip().lower(), (diagnosis or "").strip().lower())
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _claim_generated_questions_sync(
    denial_id: int,
    questions: List[Tuple[str, str]],
    generated_for: str,
    used_history: bool = False,
) -> Optional[List[Tuple[str, str]]]:
    with transaction.atomic():
        fresh = Denial.objects.select_for_update().get(denial_id=denial_id)
        current = questions_fingerprint(fresh.procedure, fresh.diagnosis)
        # A row from before the stamp existed holds a set of unknown origin;
        # a nonempty one is kept, as it always was, rather than replaced under
        # someone's answers. An empty one is not a finished set: the old code
        # stored [] for a run that found nothing and regenerated it on every
        # visit, so it is claimable, the same as no set at all. Counting it as
        # current would hand back [] here and never store what this run found.
        stored_is_current = (
            fresh.generated_questions is not None
            and fresh.generated_questions_for == current
        ) or (fresh.generated_questions_for is None and bool(fresh.generated_questions))
        if generated_for != current:
            # This run was started for inputs the person has since corrected.
            # Its questions are not stored; what stands is a set for the
            # current inputs if one exists, otherwise nothing finished.
            if stored_is_current:
                return cast(List[Tuple[str, str]], fresh.generated_questions)
            return None
        if stored_is_current:
            # First writer for these inputs keeps the slot: answers are filed
            # against the questions the person was shown.
            return cast(List[Tuple[str, str]], fresh.generated_questions)
        if used_history and fresh.health_history_consent is False:
            # The answer changed while this ran. Read on the row this block
            # already holds locked, and after the two reads above, because
            # what a refusal governs is the write: a set already standing
            # for these inputs is handed back as it always was, and only
            # putting a new one in is refused. A check made outside this
            # block would be overtaken by the refusal landing between it and
            # the write. Treated the way this function already treats a run
            # whose inputs were corrected mid-run.
            logger.info(
                f"Health history consent was withdrawn while questions for "
                f"denial {denial_id} were being generated; keeping neither "
                "the result nor a copy of it"
            )
            return None
        fresh.generated_questions = questions
        fresh.generated_questions_for = generated_for
        fresh.save(update_fields=["generated_questions", "generated_questions_for"])
        return questions


async def claim_generated_questions(
    denial_id: int,
    questions: List[Tuple[str, str]],
    generated_for: str,
    used_history: bool = False,
) -> Optional[List[Tuple[str, str]]]:
    """Store ``questions`` for the inputs they were generated for, and return
    what stands for the row's current inputs.

    A set already stored for the current inputs keeps the slot: answers are
    filed against the question they were asked under, so replacing a set
    already rendered to somebody would strand their answers. A run started
    for inputs since corrected stores nothing. An empty finished set is
    stored as ``[]``, which Back can tell from a run that never finished.
    Returns None when nothing stands for the current inputs. Serialized
    under a row lock; ``select_for_update`` is a plain read on sqlite.
    """
    stored = await database_sync_to_async(_claim_generated_questions_sync)(
        denial_id, questions, generated_for, used_history
    )
    return cast(Optional[List[Tuple[str, str]]], stored)


class MLAppealQuestionsHelper:
    @staticmethod
    async def generate_generic_questions(
        procedure: Optional[str], diagnosis: Optional[str], timeout: int = 90
    ) -> Optional[List[Tuple[str, str]]]:
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
            # Nothing to ask about is not an answer of nothing: None, so the
            # caller does not read this as a finished run with no questions.
            logger.debug(f"Missing procedure and diagnosis for generic questions")
            return None

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

        raw_questions_awaitables: List[QuestionsCoroutine] = []
        model_quality_map: AwaitableQualityMap = {}

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
        questions = await best_within_timelimit(
            raw_questions_awaitables,
            score_fn=MLAppealQuestionsHelper.make_score_fn(
                lambda x: 1, model_quality=model_quality_map
            ),
            timeout=model_timeout,
        )
        # Generic should not have answers
        if questions:
            questions_without_answers = list(map(lambda xy: (xy[0], ""), questions))
            questions = questions_without_answers

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
        # None when nobody answered, so the caller can tell that from [].
        return questions

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
    ) -> Optional[List[Tuple[str, str]]]:
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
            return None

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
        # None when nobody answered, so the caller can tell that from [].
        return questions

    @staticmethod
    async def generate_questions_for_denial(
        denial: Denial, speculative: bool
    ) -> Optional[List[Tuple[str, str]]]:
        """
        Generate appeal questions for a given denial. Uses speculative/candidate generation if nothing
        changed.

        Args:
            denial: The denial object for which to generate questions.
            speculative: Whether this is a speculative generation (candidate) or final.

        Returns:
            A list of (question, answer) tuples, or None when nothing usable
            came back at all. None and [] are different answers: [] means we
            asked and there was nothing to ask about, None means an inner
            deadline closed or no backend answered, and the page tells the
            person which of those happened.
        """
        questions: Optional[List[Tuple[str, str]]] = None
        # Whether this run is allowed to look at the history, asked before
        # it starts, so a refusal that lands while it runs can be told from
        # one that was already in place.
        used_history = False
        # The inputs this run is for, taken now: the claim at the end compares
        # them with the row's inputs then, and a run for inputs since corrected
        # stores nothing.
        generated_for = questions_fingerprint(denial.procedure, denial.diagnosis)

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
        elif (
            denial.generated_questions
            and len(denial.generated_questions) > 0
            and denial.generated_questions_for in (None, generated_for)
        ):
            # A stored set is reused only when it was generated for the
            # inputs the row holds now (or predates the stamp). One stored
            # for a since-corrected procedure is regenerated, not relabelled.
            logger.debug(f"Using cached questions for denial {denial.denial_id}")
            questions = cast(List[Tuple[str, str]], denial.generated_questions)
        else:
            logger.debug(f"Generating new questions for denial {denial.denial_id}")
            # Setup timeout based on whether this is speculative or not
            timeout = 60 if speculative else 45

            # Subtract 5 seconds to ensure proper processing time
            model_timeout = max(1, timeout - 5)
            # Which of the two answered at all: [] from both is "nothing to
            # ask", which the selector below cannot tell from nobody answering.
            answered: List[str] = []

            async def watched(name: str, coro):
                result = await coro
                if result is not None:
                    # None is the generator's "nobody answered"; [] is an
                    # answer with nothing in it.
                    answered.append(name)
                return result

            no_context_awaitable = watched(
                "generic",
                MLAppealQuestionsHelper.generate_generic_questions(
                    procedure=denial.procedure,
                    diagnosis=denial.diagnosis,
                    timeout=model_timeout,
                ),
            )
            may_use_history = await ahistory_may_be_used(denial)
            used_history = bool(denial.health_history) and may_use_history
            context_awaitable = watched(
                "specific",
                MLAppealQuestionsHelper.generate_specific_questions(
                    denial_text=denial.denial_text,
                    # Only if they said it could be used; see
                    # denial_history_consent. This goes to a model, and with
                    # use_external it can go to an outside one, so the answer
                    # is re-read at the handover rather than trusted from the
                    # row this run started with tens of seconds ago.
                    patient_context=(
                        denial.health_history if may_use_history else None
                    ),
                    procedure=denial.procedure,
                    diagnosis=denial.diagnosis,
                    timeout=model_timeout,
                    use_external=denial.use_external,
                ),
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
            if result is None and answered:
                # Every model that answered had nothing to ask: finished, empty.
                result = []

            # best_within_timelimit returns None when nothing usable
            # arrived: its window closed empty, or every backend failed.
            # Carried through rather than flattened to [].
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
            # A deterministic lookup answered, so this run finished even if
            # the model phase came back with nothing.
            merged: List[Tuple[str, str]] = list(questions or [])
            existing = {q.strip().lower() for q, _ in merged}
            for question, default in pa_questions:
                if question.strip().lower() not in existing:
                    merged.append((question, default))
                    existing.add(question.strip().lower())
            questions = merged

        if questions is None:
            return None

        logger.debug(
            f"Generated {len(questions)} questions for denial {denial.denial_id}"
        )
        if speculative:
            if questions:
                # Conditional in one statement rather than a check and then
                # a write: a refusal landing between the two would be
                # overwritten by the write. A row whose answer is no takes
                # nothing from a run that was allowed to use the history.
                candidates = Denial.objects.filter(denial_id=denial.denial_id)
                if used_history:
                    candidates = candidates.filter(still_allowed())
                if not await candidates.aupdate(
                    candidate_generated_questions=questions
                ):
                    if used_history:
                        logger.info(
                            f"Health history consent was withdrawn while "
                            f"questions for denial {denial.denial_id} were "
                            "being generated; keeping neither the result nor "
                            "a copy of it"
                        )
                        return None
            return questions
        # Empty included: a finished run with nothing to ask is stored as [].
        return await claim_generated_questions(
            denial.denial_id, questions, generated_for, used_history
        )
