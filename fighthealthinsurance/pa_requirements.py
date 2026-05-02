"""
Payer prior authorization requirement lookup.

This module helps Fight Health Insurance answer two questions for any denial
that includes CPT/HCPCS codes:

  1. Was prior authorization actually required for this code under this
     payer's published rules?
  2. If so, what coverage criteria document and submission channel applied?

It indexes payer-published PA requirement / advance notification PDFs (e.g.,
UnitedHealthcare's plan-specific PA requirement lists) into the
``PayerPriorAuthRequirement`` model. Callers either pass an explicit list of
codes or hand in a ``Denial`` and we extract the codes from its text.

The output is rendered as a context block that can be injected into appeal-
generation prompts or chat tool responses, so the LLM can ground its
arguments in the carrier's own published rules (e.g., "UHC's published list
shows code 95810 requires PA via UHCprovider.com").
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, List, Optional, Sequence, Set

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    LineOfBusiness,
    PayerPriorAuthRequirement,
)

# CPT (5 digits, e.g. 95810) or HCPCS Level II (A-V + 4 digits, e.g. J0490).
# Excludes ICD-10 codes (which start with W, X, Y, or Z) by construction.
# An optional trailing modifier (e.g. "-26") is tolerated but not captured.
_CPT_HCPCS_PATTERN = re.compile(
    r"\b(?:(?P<cpt>\d{5})|(?P<hcpcs>[A-V]\d{4}))(?:[-\s]?[A-Z0-9]{2})?\b"
)

_LOB_PATTERN_MAP = (
    (
        LineOfBusiness.DSNP,
        re.compile(r"\bdsnp\b|dual[\s-]*special[\s-]*needs", re.IGNORECASE),
    ),
    (
        LineOfBusiness.MEDICARE_ADVANTAGE,
        re.compile(r"medicare\s*(?:advantage|adv)\b|\bma\s*plan\b", re.IGNORECASE),
    ),
    (
        LineOfBusiness.MEDICAID,
        re.compile(r"\bmedicaid\b|community\s*plan", re.IGNORECASE),
    ),
    (
        LineOfBusiness.EXCHANGE,
        re.compile(
            r"marketplace|exchange|individual\s*&?\s*family|aca\s*plan",
            re.IGNORECASE,
        ),
    ),
    (
        LineOfBusiness.COMMERCIAL,
        re.compile(
            r"\bcommercial\b|employer[-\s]*sponsored|group\s*health\s*plan",
            re.IGNORECASE,
        ),
    ),
)


def extract_cpt_hcpcs_codes(text: Optional[str]) -> List[str]:
    """
    Extract CPT and HCPCS Level II codes from free-form text.

    Returns a de-duplicated list preserving first-occurrence order so the
    caller can prioritize the codes most prominent in the denial.
    """
    if not text:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for match in _CPT_HCPCS_PATTERN.finditer(text):
        code = (match.group("cpt") or match.group("hcpcs") or "").upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def infer_line_of_business(denial: Denial) -> Optional[str]:
    """
    Best-effort inference of line of business from denial text and plan
    metadata. Returns None when we cannot tell — callers should treat that
    as 'do not filter by LOB' rather than 'commercial'.
    """
    candidates: List[str] = []
    for value in (
        getattr(denial, "denial_text", None),
        getattr(denial, "plan_context", None),
        getattr(denial, "employer_name", None),
        getattr(denial, "insurance_company", None),
    ):
        if value:
            candidates.append(str(value))
    blob = "\n".join(candidates)
    if not blob:
        return None
    for lob, pat in _LOB_PATTERN_MAP:
        if pat.search(blob):
            return lob
    return None


def lookup_pa_requirements(
    codes: Sequence[str],
    insurance_company: Optional[InsuranceCompany] = None,
    state: Optional[str] = None,
    line_of_business: Optional[str] = None,
    on_date: Optional[date] = None,
) -> List[PayerPriorAuthRequirement]:
    """
    Return ``PayerPriorAuthRequirement`` rows that match any of the given
    codes for the supplied payer / state / LOB context.

    Filtering rules:
      * Codes match either via exact ``cpt_hcpcs_code`` or
        ``code_range_start..code_range_end`` (inclusive, alphanumeric compare).
      * ``state`` matches the requirement's state OR rules with no state set
        (national rules apply everywhere).
      * ``line_of_business`` matches the requirement's LOB OR rules marked
        as ``"all"``.
      * Date filter (defaults to today) excludes expired or not-yet-effective
        rules.
      * If ``insurance_company`` is None, all payers are searched.
    """
    if not codes:
        return []

    on_date = on_date or date.today()
    qs = PayerPriorAuthRequirement.objects.all()

    if insurance_company is not None:
        qs = qs.filter(insurance_company=insurance_company)

    code_q = Q()
    has_clause = False
    for code in codes:
        normalized = (code or "").upper().strip()
        if not normalized:
            continue
        code_q |= Q(cpt_hcpcs_code=normalized)
        code_q |= Q(
            code_range_start__lte=normalized,
            code_range_end__gte=normalized,
            code_range_start__gt="",
        )
        has_clause = True
    if not has_clause:
        return []
    qs = qs.filter(code_q)

    if state:
        qs = qs.filter(Q(state__iexact=state) | Q(state=""))

    if line_of_business:
        qs = qs.filter(
            Q(line_of_business=line_of_business)
            | Q(line_of_business=LineOfBusiness.ALL)
        )

    qs = qs.filter(Q(effective_date__isnull=True) | Q(effective_date__lte=on_date))
    qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=on_date))

    # Avoid select_related on insurance_company / plan: the regex_field
    # library raises ValidationError when those rows have a NULL regex
    # column (a real possibility because some seed data omits negative_regex).
    # Result sets here are small (typically a handful of rules per denial),
    # so the extra per-row fetches are negligible.
    return list(qs.distinct())


def _matches_any_requirement(
    code: str, requirements: Iterable[PayerPriorAuthRequirement]
) -> bool:
    return any(req.covers_code(code) for req in requirements)


def format_pa_context(
    requirements: Iterable[PayerPriorAuthRequirement],
    requested_codes: Optional[Sequence[str]] = None,
) -> str:
    """
    Render a list of PA requirements as a readable context block for ML
    prompts. Returns an empty string when there is nothing actionable to say.
    """
    requirements_list = list(requirements)
    unmatched: List[str] = []
    if requested_codes:
        unmatched = [
            code
            for code in requested_codes
            if not _matches_any_requirement(code, requirements_list)
        ]

    if not requirements_list and not unmatched:
        return ""

    lines: List[str] = [
        "Payer prior authorization requirements (from carrier-published PA lists):"
    ]

    for req in requirements_list:
        code_label = req.cpt_hcpcs_code or (
            f"{req.code_range_start}-{req.code_range_end}"
            if req.code_range_start and req.code_range_end
            else "(category)"
        )
        if not req.requires_pa:
            verb = "does NOT require"
        elif req.notification_only:
            verb = "requires NOTIFICATION (not full PA)"
        else:
            verb = "REQUIRES"
        scope_parts = [f"LOB={req.get_line_of_business_display()}"]
        if req.state:
            scope_parts.append(f"state={req.state}")
        if req.plan_id:
            try:
                scope_parts.append(f"plan={req.plan.plan_name}")
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Could not load plan {req.plan_id} for PA requirement {req.id}: {e}"
                )
        scope = ", ".join(scope_parts)
        lines.append(
            f"- {req.insurance_company.name}: code {code_label} {verb} prior authorization ({scope})."
        )
        if req.code_description:
            lines.append(f"    Description: {req.code_description}")
        if req.pa_category:
            lines.append(f"    Category: {req.pa_category}")
        if req.criteria_reference:
            lines.append(f"    Coverage criteria: {req.criteria_reference}")
        if req.criteria_url:
            lines.append(f"    Criteria URL: {req.criteria_url}")
        if req.submission_channel:
            lines.append(f"    Submission channel: {req.submission_channel}")
        if req.source_document:
            date_part = (
                f" (effective {req.source_document_date.isoformat()})"
                if req.source_document_date
                else ""
            )
            lines.append(f"    Source: {req.source_document}{date_part}")
        if req.notes:
            lines.append(f"    Notes: {req.notes}")

    if unmatched:
        lines.append(
            "- No published PA rule was found in this payer's indexed list for: "
            + ", ".join(sorted(set(c.upper() for c in unmatched)))
            + ". Treat this as 'not in our index' rather than confirmation that PA was not required."
        )

    return "\n".join(lines)


def get_pa_context_for_denial(denial: Denial) -> str:
    """
    Compute a PA-requirement context string for a denial.

    Extracts CPT/HCPCS codes from the denial text and procedure fields,
    looks them up against the payer's indexed PA rules, and returns a
    formatted context block. Returns an empty string when no codes are
    found or no rules match.
    """
    sources: List[str] = []
    for value in (
        getattr(denial, "denial_text", None),
        getattr(denial, "procedure", None),
        getattr(denial, "candidate_procedure", None),
        getattr(denial, "verified_procedure", None),
    ):
        if value:
            sources.append(str(value))
    if not sources:
        return ""

    codes = extract_cpt_hcpcs_codes("\n".join(sources))
    if not codes:
        return ""

    insurance_company = getattr(denial, "insurance_company_obj", None)
    line_of_business = infer_line_of_business(denial)
    state = (getattr(denial, "state", "") or "").upper().strip() or None

    try:
        requirements = lookup_pa_requirements(
            codes=codes,
            insurance_company=insurance_company,
            state=state,
            line_of_business=line_of_business,
        )
    except Exception as e:
        logger.opt(exception=True).debug(f"PA requirement lookup failed: {e}")
        return ""

    return format_pa_context(requirements, requested_codes=codes)
