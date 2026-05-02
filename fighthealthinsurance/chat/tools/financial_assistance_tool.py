"""
Financial assistance directory tool handler for the chat interface.

Handles `financial_assistance` tool calls from the LLM to look up pharmacy
discount programs (GoodRx, Cost Plus, Amazon Pharmacy), diagnosis-specific
copay foundations (CancerCare, LLS, MS Society, etc.), manufacturer copay
cards (Wegovy, Humira, etc.), 340B safety-net clinics, and state Medicaid
pathways - all from the curated catalog in
fighthealthinsurance.financial_assistance_directory.
"""

import json
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from loguru import logger

from .base_tool import BaseTool
from .patterns import FINANCIAL_ASSISTANCE_REGEX


class FinancialAssistanceTool(BaseTool):
    """
    Tool handler for financial-assistance directory lookups.

    LLM invocation format:
        financial_assistance {"drug": "Wegovy", "diagnosis": "obesity", "state": "CA"}

    All three keys are optional; supplying any one is enough to produce
    useful results. The general copay-foundation directory is always
    returned so patients with any denial can find NeedyMeds, FQHCs, and
    Medicaid pathways.
    """

    pattern = FINANCIAL_ASSISTANCE_REGEX
    name = "Financial Assistance"
    detect_flags = re.DOTALL | re.IGNORECASE

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

    def detect_all(self, text: str) -> List[re.Match[str]]:
        return list(re.finditer(self.pattern, text, flags=self.detect_flags))

    def clean_all_matches(self, text: str, matches: List[re.Match[str]]) -> str:
        cleaned = text
        for match in matches:
            cleaned = cleaned.replace(match.group(0), "")
        return cleaned.strip()

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        current_message_for_llm: str = "",
        history_for_llm: Optional[List[dict]] = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} financial_assistance tool calls; "
                "processing only the first"
            )

        json_data = match.group(1).strip()
        logger.debug(f"Financial assistance tool call params: {json_data}")

        try:
            params = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON in financial_assistance token: {json_data} - {e}"
            )
            await self.send_status_message(
                "Error processing financial assistance lookup: invalid JSON."
            )
            return (
                "I couldn't parse the financial-assistance lookup parameters. "
                "Please try again.",
                context,
            )

        if not isinstance(params, dict):
            await self.send_status_message(
                "Financial assistance lookup needs a JSON object."
            )
            return cleaned_response, context

        await self.send_status_message("Looking up financial assistance options...")

        from fighthealthinsurance.financial_assistance_directory import search

        try:
            results = search(
                drug=params.get("drug"),
                diagnosis=params.get("diagnosis"),
                denial_text=params.get("denial_text"),
                state_abbreviation=params.get("state")
                or params.get("state_abbreviation"),
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Financial assistance search raised; returning empty result"
            )
            await self.send_status_message(
                "Financial assistance lookup failed; continuing without it."
            )
            return cleaned_response, context

        if results.is_empty():
            await self.send_status_message("No financial assistance matches found.")
            return cleaned_response, context

        info_text = self._format_results_for_llm(results)
        action_text = (
            " Use the resources above to answer the user's question. Mention "
            "the most relevant 1-3 programs by name and link, and remind the "
            "user that copay-foundation funds open and close throughout the "
            "year. If a pharmacy discount program is mentioned, note that "
            "out-of-pocket payments through it typically do NOT count toward "
            "their insurance deductible or out-of-pocket maximum."
        )

        if self.call_llm_callback and model_backends:
            if history_for_llm is not None:
                history_for_llm.append(
                    {"role": "user", "content": current_message_for_llm}
                )
                history_for_llm.append({"role": "agent", "content": response_text})

            (
                additional_response,
                additional_context,
            ) = await self.call_llm_callback(
                model_backends,
                info_text + action_text,
                "Financial assistance directory lookup",
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response += "\n\n" + additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context += additional_context
            elif additional_context:
                context = additional_context

        await self.send_status_message("Financial assistance lookup completed.")
        return cleaned_response, context

    @staticmethod
    def _format_results_for_llm(results) -> str:
        """Format a FinancialAssistanceResults into LLM-readable context."""
        sections: list[str] = []
        sections.append(
            "We looked up financial-assistance resources from a curated directory."
        )
        if results.canonical_drug:
            sections.append(f"Drug: {results.canonical_drug}.")
        if results.diagnosis_text:
            sections.append(f"Diagnosis text: {results.diagnosis_text}.")

        def _fmt_program(program) -> str:
            line = f"- {program.name} ({program.url})"
            if program.description:
                line += f": {program.description}"
            if program.eligibility_note:
                line += f" Eligibility: {program.eligibility_note}"
            if program.phone:
                line += f" Phone: {program.phone}"
            return line

        if results.diagnosis_specific:
            sections.append("\nCondition-specific copay foundations:")
            sections.extend(_fmt_program(p) for p in results.diagnosis_specific)
        if results.manufacturer:
            sections.append("\nManufacturer copay programs:")
            sections.extend(_fmt_program(p) for p in results.manufacturer)
        if results.general:
            sections.append("\nGeneral copay foundations and directories:")
            sections.extend(_fmt_program(p) for p in results.general)
        if results.safety_net:
            sections.append("\nSafety-net clinics and 340B resources:")
            sections.extend(_fmt_program(p) for p in results.safety_net)
        if results.state_medicaid_name:
            line = f"\nState Medicaid: {results.state_medicaid_name}"
            if results.state_medicaid_url:
                line += f" ({results.state_medicaid_url})"
            if results.state_medicaid_phone:
                line += f" Phone: {results.state_medicaid_phone}"
            sections.append(line)

        return "\n".join(sections)
