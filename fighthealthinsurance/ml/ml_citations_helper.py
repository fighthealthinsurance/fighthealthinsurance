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
    async def generate_generic_citations(
        cls,
        procedure: str,
        diagnosis: str,
        timeout: int = 60,
    ) -> List[str]:
        """
        Generate generic citations using ML models based only on procedure and diagnosis.
        These are cached for reuse across multiple patients with the same procedure/diagnosis.

        Args:
            procedure: The medical procedure
            diagnosis: The medical diagnosis
            timeout: Maximum time to wait for citation generation

        Returns:
            List of citation strings
        """
        # Normalize inputs - trim whitespace and convert to lowercase
        procedure = procedure.strip().lower() if procedure else ""
        diagnosis = diagnosis.strip().lower() if diagnosis else ""

        # Skip if we don't have enough information
        if not procedure or not diagnosis:
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
    async def generate_citations(
        cls,
        denial_text: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        timeout: int,
        patient_context: Optional[str] = None,
        plan_context: Optional[str] = None,
        use_external: bool = False,
    ) -> List[str]:
        """
        Generate citations using ML models in a non-blocking manner.

        Args:
            denial_text: Optional denial letter text
            procedure: Optional procedure information
            diagnosis: Optional diagnosis information
            timeout: Maximum time to wait for citation generation
            patient_context: Optional patient health context
            plan_context: Optional insurance plan context
            use_external: Whether to use external models (which may include sensitive data)

        Returns:
            List of citation strings
        """
        try:
            # Get the appropriate citation backends
            partial_citation_backends = cls.ml_router.partial_find_citation_backends()
            full_citation_backends = []

            if use_external:
                full_citation_backends = cls.ml_router.full_find_citation_backends(
                    use_external=True
                )

            # Only proceed if we have backends to use
            if not partial_citation_backends and not full_citation_backends:
                logger.debug("No citation backends available")
                return []

            # Create tasks for full backends with higher priority
            full_awaitables = []
            for backend in full_citation_backends:
                full_awaitables.append(
                    backend.get_citations(
                        denial_text=denial_text,
                        procedure=procedure,
                        diagnosis=diagnosis,
                        patient_context=patient_context,
                        plan_context=plan_context,
                    )
                )

            # Create tasks for partial backends as optional tasks
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

            # Create a scoring function that gives higher priority to full backends
            def is_full_backend(awaitable):
                return 1.0 if awaitable in full_awaitables else 0.5

            # Get the best result within the timeout
            try:
                result = (
                    await best_within_timelimit(
                        full_awaitables + partial_awaitables,
                        score_fn=cls.make_score_fn(is_full_backend),
                        timeout=timeout,
                    )
                    or []
                )

                # If result is from partial backend and we have procedure/diagnosis,
                # cache it for future use
                if result and not full_awaitables and procedure and diagnosis:
                    try:
                        proc = str(procedure).strip().lower()
                        diag = str(diagnosis).strip().lower()

                        if proc and diag:
                            # Check if this result already exists in cache
                            existing = await GenericContextGeneration.objects.filter(
                                procedure=proc, diagnosis=diag
                            ).afirst()

                            if not existing:
                                await GenericContextGeneration.objects.acreate(
                                    procedure=proc,
                                    diagnosis=diag,
                                    generated_context=result,
                                )
                                logger.debug(
                                    f"Cached new generic citations for {proc}/{diag}"
                                )
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Error caching generated citations: {e}"
                        )

                return result
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to get best citations within timelimit"
                )
                return []
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
            timeout = 60 if speculative else 30

            try:
                # First try to generate patient-specific citations
                if (denial.denial_text or denial.health_history) and (
                    denial.procedure or denial.diagnosis
                ):
                    logger.debug(
                        f"Generating patient-specific citations for denial {denial.denial_id}"
                    )

                    citations = await cls.generate_citations(
                        denial_text=denial.denial_text,
                        procedure=denial.procedure,
                        diagnosis=denial.diagnosis,
                        patient_context=denial.health_history,
                        plan_context=denial.plan_context,
                        use_external=denial.use_external,
                        timeout=timeout,
                    )

                    if citations:
                        logger.debug(
                            f"Generated {len(citations)} patient-specific citations for denial {denial.denial_id}"
                        )

                # If we couldn't get patient-specific citations but have procedure/diagnosis,
                # try to get or generate generic citations
                if not citations and denial.procedure and denial.diagnosis:
                    # Use shorter timeout since this is a fallback
                    generic_timeout = 30
                    logger.debug(
                        f"Falling back to generic citations for denial {denial.denial_id}"
                    )

                    # Try to get cached or generate new generic citations
                    procedure = str(denial.procedure).strip().lower()
                    diagnosis = str(denial.diagnosis).strip().lower()

                    if procedure and diagnosis:
                        citations = await cls.generate_generic_citations(
                            procedure=procedure,
                            diagnosis=diagnosis,
                            timeout=generic_timeout,
                        )

                        if citations:
                            logger.debug(
                                f"Using generic citations for denial {denial.denial_id}"
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
