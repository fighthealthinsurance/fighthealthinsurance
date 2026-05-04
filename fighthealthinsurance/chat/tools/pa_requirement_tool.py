"""
Payer prior-authorization requirement lookup tool for the chat interface.

When the LLM emits a ``lookup_pa_requirement`` tool call, this handler
parses the JSON parameters (codes, payer, state, line of business),
queries the indexed PA requirement list, and re-invokes the LLM with the
formatted result so the next reply can quote the carrier's published
rules directly.

Example call shape the model is expected to emit::

    lookup_pa_requirement {"codes": ["95810"], "payer": "UHC", "state": "CA",
                           "line_of_business": "commercial"}
"""

from typing import Optional, Tuple

from asgiref.sync import sync_to_async

from .json_followup_tool import JsonFollowupTool
from .patterns import LOOKUP_PA_REQUIREMENT_REGEX


class PaRequirementLookupTool(JsonFollowupTool):
    """Look up payer-published PA requirements by CPT/HCPCS code."""

    pattern = LOOKUP_PA_REQUIREMENT_REGEX
    name = "PA Requirement Lookup"

    async def run(
        self, params: dict, *, current_message_for_llm: str = ""
    ) -> Tuple[str, str]:
        pa_block, summary = await sync_to_async(_run_lookup)(params)
        if not pa_block:
            return (
                "PA requirement lookup result: no entries in the indexed payer PA list "
                "matched the requested codes/payer/LOB. This is not the same as "
                "'PA was not required' — the rule may simply not be indexed.",
                "No matching PA requirements were found in the indexed list.",
            )
        return pa_block, summary


def _resolve_insurance_company(payer: Optional[str]):
    """Thin wrapper around the shared resolver in pa_requirements."""
    from fighthealthinsurance.pa_requirements import (
        resolve_insurance_company_by_name,
    )

    return resolve_insurance_company_by_name(payer)


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
