"""
Helper for generating journey documentation guidance using FHI internal models.

This module provides functionality to generate contextual questions about a patient's
medical journey (prior medications, test results, treatment history, etc.) using
FHI internal models with full denial context.
"""

import asyncio
from typing import Any, List, Optional, Tuple, cast

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.utils import best_within_timelimit


class JourneyDocumentationHelper:
    """Generates journey documentation questions using FHI internal models.

    Unlike MLAppealQuestionsHelper which generates questions for the form-based flow,
    this helper generates structured documentation guidance for the chat agent to
    proactively collect evidence that strengthens appeals.
    """

    @staticmethod
    def _format_documentation_items(
        documentation_items: list[dict[str, str]],
    ) -> str:
        """Format documentation items into a bullet list string."""
        lines = []
        for item in documentation_items:
            label = item.get("label", "Unknown")
            hint = item.get("prompt_hint", "")
            if hint:
                lines.append(f"- {label}: {hint}")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines)

    @staticmethod
    def format_questions_as_bullets(
        questions: List[Tuple[str, str]],
    ) -> str:
        """Format (question, explanation) tuples as a bullet list.

        Used by both chat_interface tool handler and get_journey_guidance_for_chat
        to consistently render question lists for the LLM.
        """
        if not questions:
            return ""
        return "\n".join(
            f"- {q}" + (f" (helps because: {a})" if a else "") for q, a in questions
        )

    @staticmethod
    async def generate_journey_questions(
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        denial_text: Optional[str] = None,
        denial_reason: Optional[str] = None,
        patient_context: Optional[str] = None,
        documentation_items: Optional[list[dict[str, str]]] = None,
        timeout: int = 45,
    ) -> List[Tuple[str, str]]:
        """Generate contextual journey documentation questions using internal models.

        Queries FHI internal models with full denial context to produce specific
        questions about the patient's medical journey that would strengthen their appeal.

        Args:
            procedure: The denied procedure/medication
            diagnosis: The patient's diagnosis
            denial_text: Full text of the denial letter
            denial_reason: Summary reason for denial
            patient_context: Any known patient context (from chat history)
            documentation_items: Structured documentation items from microsite config
            timeout: Maximum time to wait for model response

        Returns:
            List of (question, explanation) tuples where explanation describes
            why this information helps the appeal. Returns empty list on any failure.
        """
        # Need at least some context to generate useful questions
        if not any([procedure, diagnosis, denial_text, denial_reason, patient_context]):
            logger.debug("No context provided for journey questions, returning empty")
            return []

        # Build plan_context from documentation items (passed to model
        # via the plan_context parameter of get_appeal_questions)
        plan_context = None
        if documentation_items:
            items_list = JourneyDocumentationHelper._format_documentation_items(
                documentation_items
            )
            plan_context = f"Key documentation categories to ask about:\n{items_list}"

        # Use internal models for this (not external) to keep data private
        models_to_try = ml_router.get_chat_backends(use_external=False)
        if not models_to_try:
            # Fallback to partial QA backends
            models_to_try = ml_router.partial_qa_backends()

        if not models_to_try:
            logger.warning("No models available for journey question generation")
            return []

        model_timeout = max(1, timeout - 5)

        awaitables = []
        for model in models_to_try[:3]:  # Limit to 3 models to avoid excessive load
            awaitables.append(
                model.get_appeal_questions(
                    denial_text=denial_text,
                    procedure=procedure,
                    diagnosis=diagnosis,
                    patient_context=patient_context,
                    plan_context=plan_context,
                )
            )

        try:
            result = await best_within_timelimit(
                awaitables,
                score_fn=_score_journey_questions,
                timeout=model_timeout,
            )
        except Exception as e:
            logger.warning(f"best_within_timelimit raised for journey questions: {e}")
            return []

        if result:
            return cast(List[Tuple[str, str]], result)
        return []

    @staticmethod
    async def get_journey_guidance_for_chat(
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        denial_text: Optional[str] = None,
        denial_reason: Optional[str] = None,
        patient_context: Optional[str] = None,
        documentation_items: Optional[list[dict[str, str]]] = None,
        timeout: int = 35,
    ) -> str:
        """Generate a formatted journey guidance string for injection into chat context.

        This is used for auto-injecting guidance on first message when a microsite
        has journey documentation items defined.

        Args:
            timeout: Maximum total time for this call in seconds.

        Returns:
            Formatted string with journey guidance, or empty string if generation fails.
        """
        # If we have documentation items, build guidance from them directly
        # (faster than calling ML models, and always available)
        guidance_parts = []

        if documentation_items:
            items_text = JourneyDocumentationHelper._format_documentation_items(
                documentation_items
            )
            proc_name = procedure or "the denied treatment"
            guidance_parts.append(
                f"Journey documentation guidance for {proc_name}:\n"
                "The following evidence helps strengthen the appeal. "
                "Proactively ask about these during the conversation (one or two at a time):\n"
                + items_text
            )

        # Also try to get ML-generated questions for additional context,
        # bounded by the caller's timeout.
        try:
            ml_questions = await asyncio.wait_for(
                JourneyDocumentationHelper.generate_journey_questions(
                    procedure=procedure,
                    diagnosis=diagnosis,
                    denial_text=denial_text,
                    denial_reason=denial_reason,
                    patient_context=patient_context,
                    documentation_items=documentation_items,
                    timeout=max(1, min(30, timeout - 2)),
                ),
                timeout=timeout,
            )
            if ml_questions:
                q_text = JourneyDocumentationHelper.format_questions_as_bullets(
                    ml_questions
                )
                guidance_parts.append(f"Suggested questions from analysis:\n{q_text}")
        except asyncio.TimeoutError:
            logger.warning(
                f"Journey guidance ML question generation timed out after {timeout}s"
            )
        except Exception as e:
            logger.warning(f"Failed to generate ML journey questions: {e}")

        if guidance_parts:
            return "\n\n".join(guidance_parts)
        return ""


def _score_journey_questions(
    result: Optional[List[Tuple[str, str]]], awaitable: Any
) -> int:
    """Score journey question results for best_within_timelimit."""
    if result is None:
        return 0
    try:
        if not result:
            return 0
        # Prefer 2-4 questions: peak score at 3, penalize too few or too many
        count = len(result)
        if count > 6:
            return 1
        if count <= 4:
            return count * 15  # 2=30, 3=45, 4=60
        return (8 - count) * 15  # 5=45, 6=30
    except Exception:
        return 0
