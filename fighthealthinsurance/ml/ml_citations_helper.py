import asyncio
from typing import List, Optional, Dict, Any
from loguru import logger

from fighthealthinsurance.models import Denial
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.utils import (
    best_within_timelimit,
)


class MLCitationsHelper:
    """Helper class for generating citations using ML models"""

    ml_router = MLRouter()

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

            # Now see what we can find in our time budget.
            def score_fn(result, awaitable):
                # Score the result based on a mixture of source and length
                if not result:
                    return -1
                return len(result) * (1 if awaitable in full_awaitables else 0.5)

            # Get the best result within the timeout
            try:
                result = (
                    await best_within_timelimit(
                        full_awaitables + partial_awaitables,
                        score_fn=score_fn,
                        timeout=timeout,
                    )
                    or []
                )
                return result
            except:
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
        citations: list[str] = []
        if (
            denial.ml_citation_context is not None
            and len(denial.ml_citation_context) > 0
        ):
            logger.debug(f"Citations already exist for denial {denial.denial_id}")
            return denial.ml_citation_context  # type: ignore
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
            citations = denial.candidate_ml_citation_context  # type: ignore
        else:
            # Get denial context
            denial_text = denial.denial_text
            patient_context = denial.health_history
            plan_context = denial.plan_context

            # Generate citations
            citations = await cls.generate_citations(
                denial_text=denial_text,
                procedure=denial.procedure,
                diagnosis=denial.diagnosis,
                patient_context=patient_context,
                plan_context=plan_context,
                use_external=denial.use_external,
                timeout=45 if speculative else 30,
            )

        # Store citations in the denial object directly using aupdate
        if citations and not speculative:
            # Atomically update the citation_context field
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                ml_citation_context=citations
            )
        elif citations:
            # Atomically update the candidatecitation_context field
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                candidate_ml_citation_context=citations
            )
            logger.debug(
                f"Stored {len(citations)} citations for denial {denial.denial_id}"
            )

        return citations
