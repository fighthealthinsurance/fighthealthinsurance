"""
RxNorm drug-name lookup tool for the chat interface.

Lets the LLM ask the system "what is this drug, really?" before searching
PubMed or insurer policy docs. Given a brand or misspelled drug name, it
returns the canonical name, the active ingredient(s), and known brand
names. The LLM can then plug those into a follow-up query.
"""

import re
from typing import Any, Awaitable, Callable, Optional, Tuple

from loguru import logger

from fighthealthinsurance.rxnorm_tools import RxNormTools

from .base_tool import BaseTool
from .patterns import RXNORM_LOOKUP_REGEX


class RxNormLookupTool(BaseTool):
    """Handle ``rxnorm_lookup: <drug name>`` calls from the LLM."""

    pattern = RXNORM_LOOKUP_REGEX
    name = "RxNorm"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        rxnorm_tools: Optional[RxNormTools] = None,
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        super().__init__(send_status_message)
        self.rxnorm_tools = rxnorm_tools or RxNormTools()
        self.call_llm_callback = call_llm_callback

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
        **kwargs: Any,
    ) -> Tuple[str, str]:
        drug_name = match.group(1).strip()
        cleaned_response = self.clean_response(response_text, match)

        if not drug_name or "drug name" in drug_name.lower():
            logger.debug(f"Ignoring empty/placeholder rxnorm_lookup: {drug_name!r}")
            return cleaned_response, context

        await self.send_status_message(f"Normalizing drug name: {drug_name}...")

        info = await self.rxnorm_tools.get_brands_and_generics(drug_name)
        rx_context = self._format_context(drug_name, info)

        if self.call_llm_callback and model_backends and rx_context:
            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                rx_context,
                previous_context_summary,
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = context + additional_context
            elif additional_context:
                context = additional_context

        updated_context = (context + rx_context) if context else rx_context
        return cleaned_response, updated_context

    @staticmethod
    def _format_context(query: str, info: dict[str, Any]) -> str:
        if not info.get("matched"):
            return (
                f"\n\nRxNorm lookup for {query!r}: no canonical RxNorm record "
                f"found. Use the user's spelling as-is and consider asking the "
                f"user to confirm.\n"
            )
        ingredients = ", ".join(info.get("ingredients") or []) or "(unknown)"
        brand_names = ", ".join(info.get("brand_names") or []) or "(none listed)"
        return (
            f"\n\nRxNorm lookup for {query!r}:\n"
            f"  canonical name: {info['canonical_name']}\n"
            f"  RxCUI: {info['rxcui']}\n"
            f"  term type: {info.get('tty') or '(unknown)'}\n"
            f"  active ingredient(s): {ingredients}\n"
            f"  known brand names: {brand_names}\n"
            f"Use the canonical name for any follow-up search.\n"
        )
