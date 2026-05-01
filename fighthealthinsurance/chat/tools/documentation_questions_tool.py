"""
Documentation questions tool handler for the chat interface.

Handles get_documentation_questions tool calls from the LLM to generate
contextual questions that help patients document their medical journey
for insurance appeal strengthening.
"""

import json
from typing import Awaitable, Callable, Optional, Tuple

from loguru import logger

from fighthealthinsurance.microsites import get_microsite
from fighthealthinsurance.ml.ml_journey_helper import JourneyDocumentationHelper

from .base_tool import BaseTool
from .patterns import GET_DOCUMENTATION_QUESTIONS_REGEX

import re


class DocumentationQuestionsTool(BaseTool):
    """
    Tool for generating journey documentation questions.

    When the LLM detects a patient needs help documenting their medical journey,
    it calls this tool with denial context. The tool queries internal ML models
    to generate specific questions about prior medications, test results,
    treatment history, and clinical rationale.
    """

    pattern = GET_DOCUMENTATION_QUESTIONS_REGEX
    detect_flags = re.DOTALL | re.IGNORECASE
    name = "Documentation Questions"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Callable[
            ..., Awaitable[Tuple[Optional[str], Optional[str]]]
        ],
        chat: Optional[object] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.chat = chat

    async def execute(
        self, match: re.Match[str], response_text: str, context: str, **kwargs
    ) -> Tuple[str, str]:
        cleaned_response = self.clean_response(response_text, match)

        try:
            params = json.loads(match.group(1).strip())
        except Exception as json_err:
            logger.debug(f"Failed to parse documentation questions JSON: {json_err}")
            params = {}

        if not isinstance(params, dict):
            params = {}

        await self.send_status_message(
            "Analyzing denial to generate documentation questions..."
        )

        # Get microsite documentation items if available
        doc_items = None
        chat = self.chat
        microsite_slug = getattr(chat, "microsite_slug", None) if chat else None
        if microsite_slug:
            microsite = get_microsite(microsite_slug)
            if microsite and microsite.journey_documentation_items:
                doc_items = microsite.journey_documentation_items

        # Use params from tool call + chat context
        procedure = params.get("procedure") or getattr(chat, "denied_item", None)
        diagnosis = params.get("diagnosis")
        denial_text = params.get("denial_text")
        denial_reason = params.get("denial_reason") or getattr(
            chat, "denied_reason", None
        )
        patient_context = params.get("patient_context")

        questions = await JourneyDocumentationHelper.generate_journey_questions(
            procedure=procedure,
            diagnosis=diagnosis,
            denial_text=denial_text,
            denial_reason=denial_reason,
            patient_context=patient_context,
            documentation_items=doc_items,
            timeout=30,
        )

        if questions:
            q_text = JourneyDocumentationHelper.format_questions_as_bullets(questions)
            doc_context = (
                f"Documentation questions generated for this denial:\n{q_text}\n\n"
                "Use these to guide the conversation. Ask about one or two items at a time. "
                "Explain to the patient why each piece of information helps their appeal."
            )
        elif doc_items:
            items_text = JourneyDocumentationHelper.format_documentation_items(
                doc_items
            )
            doc_context = (
                f"Documentation guidance for this denial:\n{items_text}\n\n"
                "Use these to guide the conversation. Ask about one or two items at a time. "
                "Explain to the patient why each piece of information helps their appeal."
            )
        else:
            doc_context = (
                "Could not generate specific documentation questions. "
                "Ask the patient about their treatment history, prior medications, "
                "test results, and why their doctor recommended this specific treatment."
            )

        await self.send_status_message("Documentation guidance ready")

        # Call LLM with the documentation context (matching PubMed tool pattern)
        model_backends = kwargs.get("model_backends")
        previous_context_summary = kwargs.get("previous_context_summary")
        history_for_llm = kwargs.get("history_for_llm", [])
        depth = kwargs.get("depth", 0)
        is_logged_in = kwargs.get("is_logged_in", False)
        is_professional = kwargs.get("is_professional", False)

        # Add cleaned response to history before recursive call
        history_for_llm = list(history_for_llm) + [
            {"role": "agent", "content": cleaned_response}
        ]

        additional_response, additional_context = await self.call_llm_callback(
            model_backends,
            doc_context,
            previous_context_summary,
            history_for_llm,
            depth=depth + 1,
            is_logged_in=is_logged_in,
            is_professional=is_professional,
        )

        # Append additional response to cleaned text (matching PubMed pattern)
        if cleaned_response and additional_response:
            cleaned_response += additional_response
        elif additional_response:
            cleaned_response = additional_response

        if context and additional_context:
            context += additional_context
        elif additional_context:
            context = additional_context

        return cleaned_response, context
