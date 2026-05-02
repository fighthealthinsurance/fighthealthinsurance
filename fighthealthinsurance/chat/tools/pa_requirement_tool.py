"""
Payer prior-authorization requirement lookup tool for the chat interface.

When the LLM emits a ``lookup_pa_requirement`` tool call, this handler:

  1. Parses the JSON parameters (codes, payer name, state, line of business).
  2. Resolves the payer to an InsuranceCompany row by name / alt-name.
  3. Queries the indexed PA requirement list and renders a context block
     using ``pa_requirements.format_pa_context``.
  4. Re-invokes the LLM with that context appended so the next response
     can quote the carrier's published rules directly.

Example call shape the model is expected to emit::

    lookup_pa_requirement {"codes": ["95810"], "payer": "UHC", "state": "CA",
                           "line_of_business": "commercial"}
"""

import json
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from asgiref.sync import sync_to_async
from loguru import logger

from .base_tool import BaseTool
from .patterns import LOOKUP_PA_REQUIREMENT_REGEX


class PaRequirementLookupTool(BaseTool):
    """Look up payer-published PA requirements by CPT/HCPCS code."""

    pattern = LOOKUP_PA_REQUIREMENT_REGEX
    detect_flags: int = re.DOTALL | re.IGNORECASE
    name = "PA Requirement Lookup"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

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
                f"Found {len(all_matches)} PA requirement lookup tool calls; "
                "processing only the first one."
            )

        json_data = match.group(1).strip()
        try:
            params = json.loads(json_data)
        except json.JSONDecodeError:
            logger.warning(
                f"Invalid JSON data in lookup_pa_requirement token: {json_data}"
            )
            await self.send_status_message(
                "Could not parse PA requirement lookup parameters."
            )
            return cleaned_response, context

        await self.send_status_message(
            "Looking up payer prior-authorization requirements..."
        )

        try:
            pa_block, summary = await sync_to_async(_run_lookup)(params)
        except Exception as e:
            logger.opt(exception=True).warning(f"PA requirement lookup failed: {e}")
            await self.send_status_message(
                "PA requirement lookup failed. Continuing with the model's answer."
            )
            return cleaned_response, context

        if not pa_block:
            await self.send_status_message(
                "No matching PA requirements were found in the indexed list."
            )
            note = (
                "PA requirement lookup result: no entries in the indexed payer PA list "
                "matched the requested codes/payer/LOB. This is not the same as "
                "'PA was not required' — the rule may simply not be indexed."
            )
        else:
            await self.send_status_message(summary)
            note = pa_block

        if self.call_llm_callback and model_backends:
            if history_for_llm is not None:
                history_for_llm.append(
                    {"role": "user", "content": current_message_for_llm}
                )
                history_for_llm.append({"role": "agent", "content": response_text})

            followup_message = (
                f"{note}\n\n -- use this when answering: {current_message_for_llm}"
            )

            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                followup_message,
                "",
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response = f"{cleaned_response}\n\n{additional_response}"
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = f"{context}\n\n{additional_context}"
            elif additional_context:
                context = additional_context

        return cleaned_response, context


def _resolve_insurance_company(payer: Optional[str]):
    """
    Resolve a payer name (free text) to an InsuranceCompany row using
    name / alt_names / regex. Returns None when payer is empty or unknown.
    """
    if not payer:
        return None

    from django.db.models import Q

    from fighthealthinsurance.models import InsuranceCompany

    payer_clean = payer.strip()
    if not payer_clean:
        return None

    company = (
        InsuranceCompany.objects.filter(
            Q(name__iexact=payer_clean) | Q(alt_names__icontains=payer_clean)
        )
        .order_by("id")
        .first()
    )
    if company is not None:
        return company

    # Last-ditch: try regex match against company.regex by scanning rows.
    # Keep it bounded — there are only a few hundred rows.
    for candidate in InsuranceCompany.objects.exclude(regex__isnull=True).exclude(
        regex=""
    )[:500]:
        try:
            if candidate.regex and re.search(candidate.regex, payer_clean):
                return candidate
        except re.error:
            continue
    return None


def _run_lookup(params: dict) -> Tuple[str, str]:
    """
    Synchronous core of the lookup, suitable for sync_to_async wrapping.
    Returns (formatted_context, status_summary).
    """
    from fighthealthinsurance.pa_requirements import (
        extract_cpt_hcpcs_codes,
        format_pa_context,
        lookup_pa_requirements,
    )

    raw_codes = params.get("codes") or params.get("code") or []
    if isinstance(raw_codes, str):
        codes = extract_cpt_hcpcs_codes(raw_codes) or [raw_codes.strip().upper()]
    elif isinstance(raw_codes, list):
        codes = [str(c).strip().upper() for c in raw_codes if str(c).strip()]
    else:
        codes = []

    if not codes:
        return "", "No CPT/HCPCS codes were provided."

    company = _resolve_insurance_company(params.get("payer") or params.get("insurer"))
    state = (params.get("state") or "").strip().upper() or None
    lob = (params.get("line_of_business") or params.get("lob") or "").strip() or None

    requirements = lookup_pa_requirements(
        codes=codes,
        insurance_company=company,
        state=state,
        line_of_business=lob,
    )

    block = format_pa_context(requirements, requested_codes=codes)
    summary = (
        f"Found {len(requirements)} PA requirement entr"
        f"{'y' if len(requirements) == 1 else 'ies'} for "
        f"{company.name if company else 'all payers'}."
    )
    return block, summary
