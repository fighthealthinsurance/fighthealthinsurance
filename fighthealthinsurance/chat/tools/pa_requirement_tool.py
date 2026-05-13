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

import re
from typing import Optional, Tuple

from asgiref.sync import sync_to_async

from .json_followup_tool import JsonFollowupTool, extract_balanced_json
from .patterns import LOOKUP_PA_REQUIREMENT_REGEX


class PaRequirementLookupTool(JsonFollowupTool):
    """Look up payer-published PA requirements by CPT/HCPCS code."""

    pattern = LOOKUP_PA_REQUIREMENT_REGEX
    name = "PA Requirement Lookup"

    def extract_json_payload(self, match: re.Match[str], response_text: str) -> str:
        """Re-extract the JSON payload with brace-depth awareness.

        The shared ``LOOKUP_PA_REQUIREMENT_REGEX`` only matches up to the
        first ``}``, so payloads with nested objects
        (e.g. ``{"filters": {"lob": "commercial"}}``) capture as a
        truncated string. We re-scan from the start of the match to
        recover the full balanced ``{...}`` block. Falls back to the
        captured group if balancing fails so behavior degrades gracefully.
        """
        extracted = extract_balanced_json(response_text, match.start())
        if extracted:
            return extracted
        return match.group(1).strip()

    async def run(
        self, params: dict, *, current_message_for_llm: str = ""
    ) -> Tuple[str, str]:
        pa_block, summary = await sync_to_async(_run_lookup)(params)
        # Empty pa_block means the lookup found nothing usable; honor the
        # JsonFollowupTool short-circuit contract by returning an empty
        # note so the LLM follow-up pass is skipped.
        if not pa_block:
            return "", summary
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
    Returns ``(formatted_context, status_summary)``.

    When a payer name is supplied but cannot be resolved we refuse the
    lookup outright (rather than searching across every indexed payer)
    so we never present another carrier's rule as if it applied here.
    """
    from fighthealthinsurance.pa_requirements import (
        extract_cpt_hcpcs_codes,
        format_pa_context,
        lookup_pa_requirements,
    )

    import re

    raw_codes = params.get("codes") or params.get("code") or []
    if isinstance(raw_codes, str):
        # First try the strict extractor (handles "(95810)", " J0490 " etc).
        codes = extract_cpt_hcpcs_codes(raw_codes)
        if not codes:
            # Fall back to the bare token only when it actually looks like
            # a CPT (5 digits) or HCPCS Level II (active prefix + 4 digits)
            # code. The prefix subset must match the strict extractor's so
            # we don't reintroduce OCR'd ICD-10 false positives (M/N/U/etc.
            # would otherwise re-match as HCPCS).
            stripped = raw_codes.strip().upper()
            if re.fullmatch(r"\d{5}|[ABCDEGHJKLPQRSTV]\d{4}", stripped):
                codes = [stripped]
    elif isinstance(raw_codes, list):
        codes = [str(c).strip().upper() for c in raw_codes if str(c).strip()]
    else:
        codes = []

    if not codes:
        return "", "No valid CPT/HCPCS codes were provided."

    payer_input = params.get("payer") or params.get("insurer")
    company = _resolve_insurance_company(payer_input)
    state = (params.get("state") or "").strip().upper() or None
    # Normalize the LOB to the lowercase keys used by ``LineOfBusiness`` so
    # callers can pass "Commercial", "commercial", or "COMMERCIAL"
    # interchangeably without silently breaking the filter match.
    lob = (
        params.get("line_of_business") or params.get("lob") or ""
    ).strip().lower() or None

    if payer_input and company is None:
        return (
            "",
            f"Could not resolve payer '{payer_input}' to a known insurance company.",
        )

    requirements = lookup_pa_requirements(
        codes=codes,
        insurance_company=company,
        state=state,
        line_of_business=lob,
        # Chat lookups treat an unknown LOB as "show me everything that
        # could apply" rather than "narrow to LOB-agnostic rules only" —
        # the LLM frequently emits ``"line_of_business": ""``, and
        # silently hiding commercial/MA rows there was misleading.
        broaden_unknown_lob=True,
    )

    block = format_pa_context(requirements, requested_codes=codes)
    summary = (
        f"Found {len(requirements)} PA requirement entr"
        f"{'y' if len(requirements) == 1 else 'ies'} for "
        f"{company.name if company else 'all payers'}."
    )
    return block, summary
