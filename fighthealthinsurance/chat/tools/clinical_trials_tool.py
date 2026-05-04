"""
ClinicalTrials.gov search tool handler for the chat interface.

When an insurer denies a treatment as "experimental or investigational",
the AI assistant can call this tool to look up registered trials. A trial
existing does not by itself prove coverage, but it helps frame the appeal:
the question is whether the therapy is medically appropriate for the
specific patient, not whether the therapy is hypothetical.
"""

import re
from typing import Any, Awaitable, Callable, Optional, Tuple

from loguru import logger

from fighthealthinsurance.clinicaltrials_tools import (
    DEFAULT_MAX_TRIALS,
    ClinicalTrialsTools,
)

from .base_tool import BaseTool
from .patterns import CLINICAL_TRIALS_QUERY_REGEX


class ClinicalTrialsTool(BaseTool):
    """
    Tool handler for ClinicalTrials.gov searches.

    When the LLM emits a clinical_trials_query, this tool:
    1. Extracts the search terms
    2. Queries the registry
    3. Caches NCT metadata in the database
    4. Returns context for the LLM to incorporate
    """

    pattern = CLINICAL_TRIALS_QUERY_REGEX
    name = "ClinicalTrials.gov"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        clinical_trials_tools: Optional[ClinicalTrialsTools] = None,
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
        max_trials: int = DEFAULT_MAX_TRIALS,
    ):
        super().__init__(send_status_message)
        self.clinical_trials_tools = clinical_trials_tools or ClinicalTrialsTools()
        self.call_llm_callback = call_llm_callback
        self.max_trials = max_trials

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        previous_context_summary: str = "",
        history_for_llm: Any = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        query_terms = match.group(1).strip()
        cleaned_response = self.clean_response(response_text, match)

        if "your search terms" in query_terms.lower():
            logger.debug(f"Got placeholder ClinicalTrials query: {query_terms}")
            return cleaned_response, context
        if not query_terms:
            return cleaned_response, context

        await self.send_status_message(
            f"Searching ClinicalTrials.gov for: {query_terms}..."
        )

        nct_ids = await self.clinical_trials_tools.find_trials_for_query(
            query=query_terms,
            page_size=max(self.max_trials, 10),
        )
        if not nct_ids:
            await self.send_status_message(
                "No matching studies found on ClinicalTrials.gov."
            )
            return cleaned_response, context

        trials = await self.clinical_trials_tools.get_trials(nct_ids[: self.max_trials])
        if not trials:
            await self.send_status_message(
                "ClinicalTrials.gov returned IDs but no detail records were available."
            )
            return cleaned_response, context

        for trial in trials:
            if trial.brief_title:
                await self.send_status_message(
                    f"Found trial {trial.nct_id}: {trial.brief_title[:120]}"
                )

        trial_context = self._build_trials_context(trials)

        if self.call_llm_callback and model_backends:
            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                trial_context,
                previous_context_summary,
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
                fallback_backends=kwargs.get("fallback_backends"),
                full_history=kwargs.get("full_history"),
            )
            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = context + additional_context
            elif additional_context:
                context = additional_context

        updated_context = context + trial_context if context else trial_context
        return cleaned_response, updated_context

    def _build_trials_context(self, trials) -> str:
        """Render trials as a clinicaltrialscontext:[...] block for the LLM."""
        rendered = [
            self.clinical_trials_tools.format_trial_short(trial) for trial in trials
        ]
        rendered = [r for r in rendered if r]
        if not rendered:
            return ""
        body = "\n\n".join(rendered)
        return (
            "\n\nWe got back clinicaltrialscontext:[\n"
            + body
            + "\n]. When citing a trial, include the NCT ID and the URL. "
            + "Remember: the existence of a trial does not by itself establish coverage; "
            + "use it to argue that the therapy is being actively studied or used clinically, "
            + "not that the patient is enrolled in a trial.\n"
        )
