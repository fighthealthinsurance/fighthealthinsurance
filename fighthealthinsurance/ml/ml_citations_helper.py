import asyncio
from typing import List, Optional, Dict, Any, cast, Callable, Coroutine
from loguru import logger

from fighthealthinsurance.models import Denial, GenericContextGeneration
from fighthealthinsurance.utils import (
    best_within_timelimit,
)
from fighthealthinsurance.ml.ml_router import ml_router


class MLCitationsHelper:
    """Helper class for generating citations using ML models"""

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
        Generates patient-specific citations for an insurance denial using ML models.

        Uses detailed patient context—including denial text, plan context, and health history—along with procedure and diagnosis to retrieve citations from ML backends. Returns an empty list if all patient-specific context fields are missing or if no suitable backends are available.

        Args:
            denial: The insurance denial object containing relevant context.
            timeout: Maximum time in seconds to wait for citation generation.

        Returns:
            A list of citation strings generated by ML models.
        """
        # Normalize inputs - trim whitespace and convert to lowercase
        logger.debug(f"Generating specific citations for {denial}")
        procedure = denial.procedure.strip().lower() if denial.procedure else ""
        diagnosis = denial.diagnosis.strip().lower() if denial.diagnosis else ""
        denial_text = denial.denial_text
        plan_context = denial.plan_context
        patient_context = denial.health_history

        if (
            (not denial_text or denial_text == "")
            and (not plan_context or plan_context == "")
            and (not patient_context or patient_context == "")
        ):
            logger.debug(f"All patient specific context is unset, quick return.")
            return []
        result: List[str] = []
        try:
            # Get the appropriate citation backends
            full_citation_backends = ml_router.full_find_citation_backends()

            # Only proceed if we have backends to use
            if not full_citation_backends:
                logger.debug("No citation backends available for generic citations")
                return []

            # Create tasks for full backends
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

            # Get the best result within the timeout
            try:
                result = (
                    await best_within_timelimit(
                        full_awaitables,
                        score_fn=cls.make_score_fn(),
                        timeout=timeout,
                    )
                    or []
                )
                logger.debug(f"Huzzah got best citations within timelimit {result}")
                return result
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to get best citations within timelimit"
                )
                return []
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate specific citations: {e}"
            )
            return []

    @classmethod
    async def generate_generic_citations(
        cls,
        denial: Optional[Denial] = None,
        procedure_opt: Optional[str] = None,
        diagnosis_opt: Optional[str] = None,
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

        if denial:
            procedure = denial.procedure.strip().lower() if denial.procedure else ""
            diagnosis = denial.diagnosis.strip().lower() if denial.diagnosis else ""
        else:
            procedure = procedure_opt.strip().lower() if procedure_opt else ""
            diagnosis = diagnosis_opt.strip().lower() if diagnosis_opt else ""

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
                    f"Found cached generic citations for {procedure}/{diagnosis} -- {cached.generated_context}"
                )
                return cast(List[str], cached.generated_context)
            else:
                logger.debug(
                    f"No cached generic citations found for {procedure}/{diagnosis}"
                )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error fetching cached generic citations: {e}"
            )

        # If no cached citations exist, generate them
        result: List[str] = []
        try:
            # Get the appropriate citation backends - only partial backends for generic citations
            partial_citation_backends = ml_router.partial_find_citation_backends()

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
                logger.debug(
                    f"Generating best generic citations for {procedure}/{diagnosis} w/ {partial_awaitables}"
                )
                result = []
                try:
                    result = (
                        await best_within_timelimit(
                            partial_awaitables,
                            score_fn=cls.make_score_fn(),
                            timeout=timeout,
                        )
                        or []
                    )
                except:
                    result = []

                # If we have citations, cache them for future use
                if result:
                    try:
                        await GenericContextGeneration.objects.acreate(
                            procedure=procedure,
                            diagnosis=diagnosis,
                            generated_context=result,
                        )
                        logger.debug(
                            f"Stored cached generic citations for {procedure}/{diagnosis} -- {result}"
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
        if denial.ml_citation_context and len(denial.ml_citation_context) > 0:
            return denial.ml_citation_context  # type: ignore
        try:
            model_timeout = timeout - 10

            no_context_awaitable = cls.generate_generic_citations(
                denial=denial,
                timeout=model_timeout,
            )

            # If we have context look for it otherwise short circuit:
            if (not denial.denial_text or len(denial.denial_text) < 5) and (
                not denial.health_history or len(denial.health_history) < 5
            ):
                return await no_context_awaitable

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
        Pulls the speculative/candidate value if nothing changed since computed.

        Args:
            denial: The Denial object to generate citations for
            speculative: Whether to store on the speculative field

        Returns:
            List of generated citation strings
        """
        # Check if we already have citations for this denial
        logger.debug(f"Generating citations for {denial}")
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
            and len(denial.candidate_ml_citation_context) > 0
        ):
            logger.debug(f"Using candidate citations for denial {denial.denial_id}")
            citations = cast(List[str], denial.candidate_ml_citation_context)

        else:
            # Setup timeout based on whether this is speculative or not
            timeout = 75 if speculative else 30

            try:
                if (
                    denial.denial_text
                    or denial.health_history
                    or denial.procedure
                    or denial.diagnosis
                ):
                    logger.debug(f"Generating citations for denial {denial.denial_id}")

                    citations = await cls._generate_citations_for_denial(
                        denial=denial,
                        timeout=timeout,
                    )

                    if citations:
                        logger.debug(
                            f"Generated {len(citations)} citations for denial {denial.denial_id}"
                        )
                else:
                    logger.debug("Nothing to generate citations for")
                    return []
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
