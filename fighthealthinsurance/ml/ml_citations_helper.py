import asyncio
from typing import List, Optional, Dict, Any, cast, Callable, Coroutine
from loguru import logger

from fighthealthinsurance.models import Denial, GenericContextGeneration
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.utils import (
    best_within_timelimit,
)


class MLCitationsHelper:
    """Helper class for generating citations using ML models"""

    ml_router = MLRouter()

    @staticmethod
    def make_score_fn(factor: Callable[[Any], float] = lambda _: 1.0):
        """
        Create a scoring function for citation results.

        Args:
            factor: A callable that takes an awaitable and returns a float multiplier

        Returns:
            A scoring function for use with best_within_timelimit
        """

        def score_fn(result: Optional[List[str]], awaitable: Any) -> float:
            multiplier = factor(awaitable)
            # Score the result based on a mixture of source and length
            if not result:
                return -1.0
            return float(len(result)) * multiplier

        return score_fn

    @classmethod
    async def generate_specific_citations(
        cls,
        denial: Denial,
        timeout: int = 60,
    ) -> List[str]:
        """
        Generate specific citations using ML models based only on procedure and diagnosis.

        Args:
            denial: Denial
            timeout: Maximum time to wait for citation generation

        Returns:
            List of citation strings
        """
        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = denial.procedure.strip().lower() if denial.procedure else ""
        diagnosis = denial.diagnosis.strip().lower() if denial.diagnosis else ""
        denial_text = denial.denial_text
        plan_context = denial.plan_context
        patient_context = denial.health_history

        if not denial_text and not plan_context and not patient_context:
            logger.debug(f"All patient specific context is unset, quick return.")
            return []

        # Skip if we don't have enough information
        if not procedure and not diagnosis:
            logger.debug(f"Missing procedure or diagnosis for generic citations")
            return []

        # Check for existing cached citations first
        try:
            cached = await GenericContextGeneration.objects.filter(
                procedure=procedure, diagnosis=diagnosis
            ).afirst()

            if cached:
                logger.debug(
                    f"Found cached generic citations for {procedure}/{diagnosis}"
                )
                return cast(List[str], cached.generated_context)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error fetching cached generic citations: {e}"
            )

        # If no cached citations exist, generate them
        result: List[str] = []
        try:
            # Get the appropriate citation backends - only partial backends for generic citations
            partial_citation_backends = cls.ml_router.partial_find_citation_backends()

            # Only proceed if we have backends to use
            if not partial_citation_backends:
                logger.debug("No citation backends available for generic citations")
                return []

            # Create tasks for partial backends
            partial_awaitables = []
            for backend in partial_citation_backends:
                partial_awaitables.append(
                    backend.get_citations(
                        denial_text=denial_text,
                        procedure=procedure,
                        diagnosis=diagnosis,
                        patient_context=patient_context,
                        plan_context=plan_context,
                    )
                )

            # Get the best result within the timeout
            try:
                result = (
                    await best_within_timelimit(
                        partial_awaitables,
                        score_fn=cls.make_score_fn(),
                        timeout=timeout,
                    )
                    or []
                )

                # If we have citations, cache them for future use
                if result:
                    try:
                        await GenericContextGeneration.objects.acreate(
                            procedure=procedure,
                            diagnosis=diagnosis,
                            generated_context=result,
                        )
                        logger.debug(
                            f"Cached generic citations for {procedure}/{diagnosis}"
                        )
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Error caching generic citations: {e}"
                        )

                return result
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to get best citations within timelimit"
                )
                return []
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate generic citations: {e}"
            )
            return []

    @classmethod
    async def generate_generic_citations(
        cls,
        denial: Denial,
        timeout: int = 60,
    ) -> List[str]:
        """
        Generate generic citations using ML models based only on procedure and diagnosis.
        These are cached for reuse across multiple patients with the same procedure/diagnosis.

        Args:
            denial: the denial
            timeout: Maximum time to wait for citation generation

        Returns:
            List of citation strings
        """
        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = denial.procedure.strip().lower() if denial.procedure else ""
        diagnosis = denial.diagnosis.strip().lower() if denial.diagnosis else ""

        # Skip if we don't have enough information
        if not procedure and not diagnosis:
            logger.debug(f"Missing procedure or diagnosis for generic citations")
            return []

        # Check for existing cached citations first
        try:
            cached = await GenericContextGeneration.objects.filter(
                procedure=procedure, diagnosis=diagnosis
            ).afirst()

            if cached:
                logger.debug(
                    f"Found cached generic citations for {procedure}/{diagnosis}"
                )
                return cast(List[str], cached.generated_context)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error fetching cached generic citations: {e}"
            )

        # If no cached citations exist, generate them
        result: List[str] = []
        try:
            # Get the appropriate citation backends - only partial backends for generic citations
            partial_citation_backends = cls.ml_router.partial_find_citation_backends()

            # Only proceed if we have backends to use
            if not partial_citation_backends:
                logger.debug("No citation backends available for generic citations")
                return []

            # Create tasks for partial backends
            partial_awaitables = []
            for backend in partial_citation_backends:
                partial_awaitables.append(
                    backend.get_citations(
                        denial_text=None,
                        procedure=procedure,
                        diagnosis=diagnosis,
                        patient_context=None,
                        plan_context=None,
                    )
                )

            # Get the best result within the timeout
            try:
                result = (
                    await best_within_timelimit(
                        partial_awaitables,
                        score_fn=cls.make_score_fn(),
                        timeout=timeout,
                    )
                    or []
                )

                # If we have citations, cache them for future use
                if result:
                    try:
                        await GenericContextGeneration.objects.acreate(
                            procedure=procedure,
                            diagnosis=diagnosis,
                            generated_context=result,
                        )
                        logger.debug(
                            f"Cached generic citations for {procedure}/{diagnosis}"
                        )
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Error caching generic citations: {e}"
                        )

                return result
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to get best citations within timelimit"
                )
                return []
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate generic citations: {e}"
            )
            return []

    @classmethod
    async def _generate_citations_for_denial(
        cls,
        denial: Denial,
        timeout: int = 45,
    ) -> List[str]:
        """
        Chooses the best citations for a denial object based on the available context.

        Args:
            denial: Denial

        Returns:
            List of citation strings
        """
        try:
            model_timeout = timeout - 10

            no_context_awaitable = cls.generate_generic_citations(
                denial=denial,
                timeout=model_timeout,
            )
            context_awaitable = cls.generate_specific_citations(
                denial=denial,
                timeout=model_timeout,
            )

            def is_full_backend(awaitable: Coroutine) -> int:
                if awaitable == context_awaitable:
                    return 2
                return 1

            # Get the best result within the timeout
            result = (
                await best_within_timelimit(
                    [no_context_awaitable, context_awaitable],
                    score_fn=cls.make_score_fn(is_full_backend),
                    timeout=timeout,
                )
                or []
            )
            return result
        except Exception as e:
            logger.opt(exception=True).warning(f"Failed to generate citations: {e}")
            return []

    @classmethod
    async def generate_citations_for_denial(
        cls, denial: Denial, speculative: bool
    ) -> List[str]:
        """
        Generate citations for a denial object if they don't already exist.

        Args:
            denial: The Denial object to generate citations for
            speculative: Whether to store on the speculative field

        Returns:
            List of generated citation strings
        """
        # Check if we already have citations for this denial
        citations: List[str] = []
        if (
            denial.ml_citation_context is not None
            and len(denial.ml_citation_context) > 0
        ):
            logger.debug(f"Citations already exist for denial {denial.denial_id}")
            return cast(List[str], denial.ml_citation_context)

        elif (
            denial.candidate_ml_citation_context
            and (
                not denial.procedure or (denial.candidate_procedure == denial.procedure)
            )
            and (
                not denial.diagnosis or (denial.candidate_diagnosis == denial.diagnosis)
            )
        ):
            logger.debug(f"Using candidate citations for denial {denial.denial_id}")
            citations = cast(List[str], denial.candidate_ml_citation_context)

        else:
            # Setup timeout based on whether this is speculative or not
            timeout = 75 if speculative else 30

            try:
                # First try to generate patient-specific citations
                if (denial.denial_text or denial.health_history) and (
                    denial.procedure or denial.diagnosis
                ):
                    logger.debug(f"Generating citations for denial {denial.denial_id}")

                    citations = await cls._generate_citations_for_denial(
                        denial=denial,
                        timeout=timeout,
                    )

                    if citations:
                        logger.debug(
                            f"Generated {len(citations)} patient-specific citations for denial {denial.denial_id}"
                        )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Error generating citations for denial {denial.denial_id}: {e}"
                )

        # Store citations in the denial object directly using aupdate
        if citations:
            # Atomically update the appropriate field
            if not speculative:
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    ml_citation_context=citations
                )
            else:
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    candidate_ml_citation_context=citations
                )
            logger.debug(
                f"Stored {len(citations)} citations for denial {denial.denial_id}"
            )

        return citations
