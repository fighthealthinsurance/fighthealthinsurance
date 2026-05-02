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
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    LineOfBusiness,
    PayerPriorAuthRequirement,
)

# CPT (5 digits, e.g. 95810) or HCPCS Level II.
#
# HCPCS Level II is restricted to the prefix subset that's actually in active
# use (A,B,C,D,E,G,H,J,K,L,P,Q,R,S,T,V). F/I/M/N/O/U are intentionally
# excluded — they're either retired, never assigned, or overlap heavily with
# ICD-10 musculoskeletal/respiratory diagnosis codes (e.g. ICD-10 ``M54.50``
# becomes ``M5450`` when OCR drops the period, which would otherwise look
# like a HCPCS code). Adding F/I/M/N/O back would re-introduce that
# false-positive risk.
_CPT_HCPCS_PATTERN = re.compile(
    r"\b(?:(?P<cpt>\d{5})|(?P<hcpcs>[ABCDEGHJKLPQRSTV]\d{4}))(?:[-\s]?[A-Z0-9]{2})?\b"
)

# Used to detect ICD-10 codes with their canonical period so we can strip
# them from text before HCPCS extraction (defends against e.g. ``J45.20``
# being mis-extracted as HCPCS J4520 if the period survives the regex).
_ICD10_DOTTED_PATTERN = re.compile(r"\b[A-TV-Z][0-9][0-9AB]\.[0-9A-TV-Z]{1,4}\b")

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

    ICD-10 diagnosis codes that share the HCPCS shape are filtered out two
    ways: dotted ICD-10 codes (e.g. ``J45.20``) are removed from the input
    before scanning, and any HCPCS-shaped match whose compact form also
    appears as an ICD-10 code in the original text is dropped. This prevents
    OCR'd diagnoses like ``M54.50`` → ``M5450`` from being misread as
    procedure codes.
    """
    if not text:
        return []

    icd10_compact: Set[str] = {
        m.group(0).replace(".", "").upper()
        for m in _ICD10_DOTTED_PATTERN.finditer(text)
    }
    cleaned_text = _ICD10_DOTTED_PATTERN.sub(" ", text)

    seen: Set[str] = set()
    out: List[str] = []
    for match in _CPT_HCPCS_PATTERN.finditer(cleaned_text):
        hcpcs = match.group("hcpcs")
        code = (match.group("cpt") or hcpcs or "").upper()
        if not code or code in seen:
            continue
        if hcpcs and code in icd10_compact:
            continue
        seen.add(code)
        out.append(code)
    return out


def resolve_insurance_company_by_name(
    payer: Optional[str],
) -> Optional[InsuranceCompany]:
    """
    Resolve a free-form payer name to an ``InsuranceCompany`` row by
    iexact match on ``name``, then by alt-name match, then by the
    company's stored ``regex`` pattern. Returns None when the name is
    empty or no row matches.

    Matching deliberately skips substring matches on ``name`` to avoid
    false positives across closely-named carriers (for example, "United
    Health Group" vs "UnitedHealthcare Community Plan").
    """
    if not payer:
        return None

    payer_clean = payer.strip()
    if not payer_clean:
        return None

    company = InsuranceCompany.objects.filter(name__iexact=payer_clean).first()
    if company is not None:
        return company

    payer_lower = payer_clean.lower()
    candidates = InsuranceCompany.objects.exclude(alt_names="")
    for candidate in candidates.iterator():
        if not candidate.alt_names:
            continue
        for alt in candidate.alt_names.splitlines():
            alt = alt.strip().lower()
            if alt and (alt == payer_lower or alt in payer_lower):
                return candidate

    # Last resort: try compiled `regex` field on each company.
    for candidate in InsuranceCompany.objects.exclude(regex="").iterator():
        try:
            if candidate.regex and re.search(candidate.regex, payer_clean):
                return candidate
        except re.error:
            continue
    return None


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
                plan = req.plan
                if plan is not None:
                    scope_parts.append(f"plan={plan.plan_name}")
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


def _denial_lookup(
    denial: Denial,
) -> Tuple[List[PayerPriorAuthRequirement], List[str]]:
    """
    Run a PA lookup against a Denial and return ``(requirements, codes)``.

    Returns ``([], [])`` when the denial has no procedure codes, no
    identifiable payer, or the lookup fails. The payer guard is critical:
    without it we would search across all carriers and surface rules from
    the wrong insurer in payer-attributed appeal context.
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
        return [], []

    codes = extract_cpt_hcpcs_codes("\n".join(sources))
    if not codes:
        return [], []

    insurance_company = getattr(denial, "insurance_company_obj", None)
    if insurance_company is None:
        # Fall back to the free-text insurer field; if that doesn't resolve
        # to a known carrier, refuse the lookup so we don't bleed rules
        # across payers. Return empty codes too so the caller doesn't
        # emit a misleading "no rule found in this payer's list" message.
        insurance_company = resolve_insurance_company_by_name(
            getattr(denial, "insurance_company", None)
        )
        if insurance_company is None:
            return [], []

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
        return [], codes

    return requirements, codes


def get_pa_context_for_denial(denial: Denial) -> str:
    """
    Compute a PA-requirement context string for a denial.

    Extracts CPT/HCPCS codes from the denial text and procedure fields,
    looks them up against the payer's indexed PA rules, and returns a
    formatted context block. Returns an empty string when no codes are
    found or no rules match.
    """
    requirements, codes = _denial_lookup(denial)
    if not codes:
        return ""
    return format_pa_context(requirements, requested_codes=codes)


# Category-keyed clarifying questions. Each entry is a list of
# ``(question, default_answer)`` tuples in the same shape used by
# ``MLAppealQuestionsHelper`` so they can be merged into the denial's
# ``generated_questions`` directly. Keys match ``pa_category`` values used
# in the seed data — adding a new category here is the only step needed
# to surface PA-aware questions for it.
_PA_CATEGORY_QUESTIONS: Dict[str, List[Tuple[str, str]]] = {
    "Genetic and Molecular Testing": [
        (
            "Did the patient receive genetic counseling from a qualified provider before testing?",
            "",
        ),
        (
            "What personal or family history of cancer triggered the test order?",
            "",
        ),
        (
            "Will the result change clinical management (treatment, surveillance, or screening)?",
            "",
        ),
    ],
    "Sleep Medicine": [
        (
            "Has the patient completed a validated sleep questionnaire (Epworth, STOP-BANG)?",
            "",
        ),
        (
            "Does the patient have witnessed apneas, loud snoring, or daytime sleepiness?",
            "",
        ),
        (
            "Has a home sleep study been attempted, or is one contraindicated?",
            "",
        ),
    ],
    "Durable Medical Equipment - CGM": [
        (
            "Is the patient on intensive insulin therapy (3+ injections/day or insulin pump)?",
            "",
        ),
        (
            "Has the patient documented frequent self-monitoring (4+ fingersticks/day)?",
            "",
        ),
        (
            "Is there a history of hypoglycemia unawareness or recurrent severe hypoglycemia?",
            "",
        ),
    ],
    "Outpatient Injectable Medication": [
        (
            "Which formulary alternatives have been tried, and what was the response or adverse reaction?",
            "",
        ),
        (
            "Is there an FDA-approved indication or established off-label use that supports this drug?",
            "",
        ),
        (
            "Will the drug be administered in a site of care covered by the plan (office vs. infusion center vs. hospital outpatient)?",
            "",
        ),
    ],
    "Advanced Outpatient Imaging": [
        (
            "What lower-cost imaging (e.g., x-ray, ultrasound) has already been performed and what did it show?",
            "",
        ),
        (
            "Are there red-flag symptoms (neurologic deficit, suspected malignancy, trauma) that justify advanced imaging?",
            "",
        ),
        (
            "How will the imaging result change management?",
            "",
        ),
    ],
    "Spine Surgery": [
        (
            "What conservative therapies (physical therapy, NSAIDs, injections) have been trialed and for how long?",
            "",
        ),
        (
            "Are there imaging findings (compression fracture, instability) that match the patient's symptoms?",
            "",
        ),
        (
            "Is there evidence of progressive neurologic deficit or intractable pain?",
            "",
        ),
    ],
    "Evaluation and Management": [
        (
            "Was the office visit billed as a routine E&M code that does not require PA under this plan?",
            "",
        ),
    ],
}

# Generic question templates used when a matched rule has no recognised
# category — keyed by sub-shape of the rule (criteria reference present,
# negative rule, notification-only, etc.).
_GENERIC_PA_QUESTION_TEMPLATES = {
    "criteria": (
        "Does the patient meet the medical-necessity criteria in {criteria}?",
        "",
    ),
    "negative_rule": (
        "Was this code billed under the same plan and line of business listed in the payer's PA list (where PA is not required)?",
        "",
    ),
    "notification": (
        "Was the payer notified per its advance-notification requirement (typically before the date of service)?",
        "",
    ),
    "submission_channel": (
        "Was the PA request submitted through the payer's published channel ({channel})?",
        "",
    ),
}


def generate_pa_questions(
    requirements: Iterable[PayerPriorAuthRequirement],
    max_questions: int = 4,
) -> List[Tuple[str, str]]:
    """
    Derive clarifying questions from matched PA requirements.

    Picks category-specific questions when the rule's ``pa_category`` matches
    a known template, otherwise falls back to generic templates keyed off
    the rule's coverage criteria, submission channel, and PA-required flag.
    De-duplicates questions and caps the list at ``max_questions``.
    """
    requirements_list = list(requirements)
    if not requirements_list:
        return []

    seen: Set[str] = set()
    out: List[Tuple[str, str]] = []

    def add(question: str, default: str = "") -> None:
        key = question.strip().lower()
        if key in seen or len(out) >= max_questions:
            return
        seen.add(key)
        out.append((question, default))

    # Prefer category-specific questions first.
    for req in requirements_list:
        if len(out) >= max_questions:
            break
        for question, default in _PA_CATEGORY_QUESTIONS.get(req.pa_category, []):
            add(question, default)

    # Fill any remaining slots with generic, rule-shape-driven questions.
    for req in requirements_list:
        if len(out) >= max_questions:
            break
        if not req.requires_pa:
            q, d = _GENERIC_PA_QUESTION_TEMPLATES["negative_rule"]
            add(q, d)
            continue
        if req.notification_only:
            q, d = _GENERIC_PA_QUESTION_TEMPLATES["notification"]
            add(q, d)
        if req.criteria_reference:
            q, d = _GENERIC_PA_QUESTION_TEMPLATES["criteria"]
            add(q.format(criteria=req.criteria_reference), d)
        if req.submission_channel and len(out) < max_questions:
            q, d = _GENERIC_PA_QUESTION_TEMPLATES["submission_channel"]
            # Trim very long channel strings so the question stays readable.
            channel = req.submission_channel.split(";")[0].strip()
            add(q.format(channel=channel), d)

    return out


def get_pa_questions_for_denial(
    denial: Denial, max_questions: int = 4
) -> List[Tuple[str, str]]:
    """
    Convenience entry point: run a PA lookup against the denial and return
    a list of clarifying questions. Returns an empty list when no rules
    match.
    """
    requirements, codes = _denial_lookup(denial)
    if not requirements:
        return []
    return generate_pa_questions(requirements, max_questions=max_questions)
