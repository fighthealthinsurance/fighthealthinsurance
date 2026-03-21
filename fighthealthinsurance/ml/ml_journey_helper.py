"""
Helper for generating journey documentation guidance using FHI internal models.

This module provides functionality to generate contextual questions about a patient's
medical journey (prior medications, test results, treatment history, etc.) using
FHI internal models with full denial context.
"""

import asyncio
from typing import Any, List, Optional, Tuple

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
        # Build the prompt with all available context
        context_parts = []

        if procedure:
            context_parts.append(f"Denied procedure/medication: {procedure}")
        if diagnosis:
            context_parts.append(f"Diagnosis: {diagnosis}")
        if denial_reason:
            context_parts.append(f"Denial reason: {denial_reason}")
        if denial_text:
            context_parts.append(f"Denial letter text: {denial_text[:2000]}")
        if patient_context:
            context_parts.append(
                f"Known patient context from conversation: {patient_context}"
            )

        if not context_parts:
            logger.debug("No context provided for journey questions, returning empty")
            return []

        context_str = "\n".join(context_parts)

        # Build documentation items hint if available
        doc_items_hint = ""
        if documentation_items:
            items_list = "\n".join(
                f"- {item.get('label', 'Unknown')}"
                + (
                    f" ({item.get('prompt_hint', '')})"
                    if item.get("prompt_hint")
                    else ""
                )
                for item in documentation_items
            )
            doc_items_hint = (
                f"\nKey documentation categories to ask about:\n{items_list}\n"
            )

        prompt = f"""Context about the patient's insurance denial:
{context_str}
{doc_items_hint}
Your task: Generate 2-4 specific, patient-friendly questions that help document the patient's medical journey to strengthen their appeal. Focus on:
1. Prior treatments/medications tried and why they didn't work
2. Relevant test results or diagnostic evidence
3. Treatment history and timeline
4. Clinical rationale for the denied treatment

Format each question on its own line as: QUESTION? | EXPLANATION_OF_WHY_THIS_HELPS
For example:
What allergy medications have you tried before, and did any cause side effects? | Documenting failed prior treatments shows medical necessity and that cheaper alternatives were already attempted.
How long have you been experiencing these symptoms? | A longer symptom history strengthens the argument that this treatment is medically necessary, not elective.

Only ask about information NOT already known from the context above. Do not ask about the denial itself.
While your reasoning (inside <think></think>) can discuss rationale, do not include it in the answer."""

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
            return result
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
            items_text = []
            for item in documentation_items:
                label = item.get("label", "Unknown")
                hint = item.get("prompt_hint", "")
                if hint:
                    items_text.append(f"- {label}: {hint}")
                else:
                    items_text.append(f"- {label}")

            proc_name = procedure or "the denied treatment"
            guidance_parts.append(
                f"Journey documentation guidance for {proc_name}:\n"
                "The following evidence helps strengthen the appeal. "
                "Proactively ask about these during the conversation (one or two at a time):\n"
                + "\n".join(items_text)
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
                    timeout=min(30, timeout - 2),
                ),
                timeout=timeout,
            )
            if ml_questions:
                q_text = "\n".join(
                    f"- {q}" + (f" (this helps because: {a})" if a else "")
                    for q, a in ml_questions
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
        # Prefer 2-4 questions
        count = len(result)
        if count > 6:
            return 1  # Too many is bad
        return count * 10
    except Exception:
        return 0
