"""
Payer Policy Context Helper.

Builds short context blocks describing major commercial payers' public
medical-policy frameworks (Aetna's CPBs, Cigna's Coverage Policies,
Anthem's Medical Policies, etc.) and -- where the
``ingest_payer_policy_indexes`` management command has populated
:class:`PayerPolicyEntry` rows -- surfaces the *actual* matched policy
titles and URLs for the patient's procedure/diagnosis. The output is
intended for inclusion in appeal prompts as comparative industry evidence.

A standard caveat is always included so neither the model nor the
generated appeal implies that another payer's policy binds the user's
specific plan.
"""

import re
from typing import List, Optional

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.models import InsuranceCompany, PayerPolicyEntry

# Caveat that must accompany any reference to other payers' policies in an
# appeal. Other payers' policies are comparative evidence only; they don't
# bind the user's plan.
COMPARATIVE_PAYER_CAVEAT = (
    "Important caveat: each insurance plan is governed by its own policy "
    "documents (the Evidence of Coverage, Summary Plan Description, or "
    "applicable medical policy). Other payers' public medical policies are "
    "cited here only as comparative industry evidence that recognized "
    "insurers treat the service as medically necessary under defined "
    "criteria; they do not bind, and may not match, the patient's specific "
    "plan."
)

# Reminder used when we have a URL/notes pointer but no parsed entries: the
# prompt may only point at the index, not its specific contents.
NO_ENTRIES_GUARD = (
    "IMPORTANT: only the index name/URL/notes are provided here -- the "
    "actual policy text and its specific medical-necessity criteria have "
    "NOT been retrieved. Do NOT invent, paraphrase, or assert specific "
    "criteria, thresholds, or covered indications from the payer's policy. "
    "You may say the policy exists and point to it; do not claim the "
    "patient's record meets criteria from a document that has not been "
    "read."
)

# Reminder used when parsed entries ARE provided: we have title + URL but
# still not the full policy text.
WITH_ENTRIES_GUARD = (
    "IMPORTANT: each entry below is a real, specific policy title and URL "
    "matched against the patient's procedure/diagnosis. The full criteria "
    "from each policy have NOT been retrieved into this prompt -- only the "
    "title and link. The appeal may cite that the payer publishes a policy "
    "covering this service and link to it; the appeal must NOT invent or "
    "paraphrase specific criteria from the underlying document."
)


def resolve_company_from_text(name_text: Optional[str]) -> Optional[InsuranceCompany]:
    """
    Best-effort lookup of an InsuranceCompany from a free-text payer name.

    Used to back-fill from the legacy ``Denial.insurance_company`` text field
    when the structured ``Denial.insurance_company_obj`` is null. Without
    this, the denying payer can show up in the comparative-evidence section
    as an "other major payer", which is wrong.

    Tries case-insensitive exact match first, then a case-insensitive search
    of the ``alt_names`` lines. Returns None if no confident match.
    """
    if not name_text:
        return None
    text = name_text.strip()
    if not text:
        return None
    try:
        match = InsuranceCompany.objects.filter(name__iexact=text).first()
        if match is not None:
            return match
        for company in InsuranceCompany.objects.filter(alt_names__icontains=text):
            for alt in (company.alt_names or "").splitlines():
                if alt.strip().lower() == text.lower():
                    return company
    except Exception:
        logger.exception("Error resolving InsuranceCompany from text")
    return None


# --- query helpers ---------------------------------------------------------


# Short tokens are usually noise (the, for) -- but a few medical
# abbreviations are worth keeping even at <4 chars.
#
# Note: cms_coverage_api and ecri_guidelines_helper have similar
# tokenizers, but their min_len is 2 (they match against larger texts,
# where 2-char clinical abbreviations like RA/MI/CT are useful). Here we
# match against short payer-policy *titles*, where 2- and 3-char tokens
# yield mostly false positives -- hence the higher floor and an explicit
# allowlist for the abbreviations we still want to keep.
_SHORT_KEEP = frozenset({"mri", "ct", "pet", "cbc", "cpap", "tms", "ivf"})

_ALNUM_RUN_RE = re.compile(r"[a-z0-9]+")


def _keyword_terms(*texts: Optional[str]) -> List[str]:
    """Return distinct lowercase alphanumeric token runs (>=4 chars or in
    :data:`_SHORT_KEEP`) extracted from the given free-text inputs.

    Splits on any non-alphanumeric character so that inputs like
    ``"MRI/PET-scan"`` yield ``["mri", "pet", "scan"]`` rather than a
    single mashed token.
    """
    out: List[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for tok in _ALNUM_RUN_RE.findall(text.lower()):
            if tok in seen:
                continue
            if len(tok) < 4 and tok not in _SHORT_KEEP:
                continue
            seen.add(tok)
            out.append(tok)
    return out


def get_relevant_policy_entries(
    procedure: Optional[str],
    diagnosis: Optional[str],
    company_id: Optional[int] = None,
    exclude_company_id: Optional[int] = None,
    max_entries: int = 5,
) -> List[PayerPolicyEntry]:
    """Return ``PayerPolicyEntry`` rows whose title contains any procedure or
    diagnosis keyword.

    Args:
        procedure / diagnosis: free-text strings from the denial.
        company_id: if set, restrict to entries from this company.
        exclude_company_id: if set, drop entries from this company (used to
            keep the denying payer out of the comparative section).
        max_entries: cap on returned rows.
    """
    terms = _keyword_terms(procedure, diagnosis)
    if not terms:
        return []
    try:
        title_q = Q()
        for t in terms:
            title_q |= Q(title__icontains=t)
        qs = PayerPolicyEntry.objects.filter(title_q).select_related(
            "insurance_company"
        )
        if company_id is not None:
            qs = qs.filter(insurance_company_id=company_id)
        if exclude_company_id is not None:
            qs = qs.exclude(insurance_company_id=exclude_company_id)
        return list(qs.order_by("insurance_company__name", "title")[:max_entries])
    except Exception:
        logger.exception("Error querying PayerPolicyEntry")
        return []


# --- rendering -------------------------------------------------------------


def _format_company_block(company: InsuranceCompany) -> Optional[str]:
    """Render a single payer's medical-policy info as a short markdown block.

    Distinguishes URLs we know are static, directly-browsable indexes (which
    we cite directly) from URLs that are interactive search portals or that
    require state/region selection (which we mark explicitly so the appeal
    doesn't suggest the link itself contains the policy text).
    """
    if not company.has_public_medical_policy:
        return None
    policy_name = company.medical_policy_name or "medical policies"
    line = f"- **{company.name}** publishes {policy_name}"
    if company.medical_policy_url:
        if company.medical_policy_url_is_static_index:
            line += f" ({company.medical_policy_url})"
        else:
            line += (
                f" -- the public landing page is at {company.medical_policy_url}, "
                "but it is an interactive search/state-selection portal, not a "
                "browsable index of policy text"
            )
    line += "."
    if company.medical_policy_notes:
        return f"{line}\n  {company.medical_policy_notes}"
    return line


def _format_entry_line(entry: PayerPolicyEntry) -> str:
    suffix = f" [{entry.payer_policy_id}]" if entry.payer_policy_id else ""
    return f"- **{entry.insurance_company.name}**: {entry.title}{suffix} -- {entry.url}"


# --- public API ------------------------------------------------------------


def get_payer_policy_context(
    company: Optional[InsuranceCompany],
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    max_entries: int = 4,
) -> str:
    """Context block describing the user's own payer's medical-policy framework.

    If ``ingest_payer_policy_indexes`` has populated ``PayerPolicyEntry``
    rows for ``company`` and any of them title-match the procedure/
    diagnosis, those concrete (title, URL) pairs are surfaced. Otherwise
    we fall back to a generic pointer at the company's published index.
    """
    if company is None:
        return ""
    block = _format_company_block(company)
    if not block:
        return ""

    matched = get_relevant_policy_entries(
        procedure=procedure,
        diagnosis=diagnosis,
        company_id=company.id,
        max_entries=max_entries,
    )

    lines = [
        "## Payer's Public Medical Policy",
        (
            f"The denying payer ({company.name}) publishes a public "
            "medical-policy framework. The appeal may cite that framework "
            "as a pointer; below is the published index, plus any "
            "specifically-matched policies for this case."
        ),
        "",
        block,
        "",
    ]
    if matched:
        lines.append("Matched policies for this procedure/diagnosis:")
        lines.extend(_format_entry_line(e) for e in matched)
        lines.append("")
        lines.append(WITH_ENTRIES_GUARD)
    else:
        lines.append(NO_ENTRIES_GUARD)

    lines.append("")
    lines.append(
        "Note: a payer's published medical policy is general guidance; the "
        "patient's specific plan may have additional terms in its Evidence "
        "of Coverage or Summary Plan Description that govern the actual "
        "coverage decision."
    )
    return "\n".join(lines)


def get_comparative_payer_policy_context(
    exclude_company_id: Optional[int] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    max_payers: int = 4,
    max_entries: int = 4,
) -> str:
    """Comparative-evidence block listing other major payers' policies.

    Surfaces real matched ``PayerPolicyEntry`` rows when available so the
    appeal can point at concrete policy URLs from other carriers; falls
    back to a generic per-payer pointer (name/URL/notes) when no matches
    exist. Always emits ``COMPARATIVE_PAYER_CAVEAT``.
    """
    try:
        # Match has_public_medical_policy / _format_company_block: include
        # any payer with at least one of url or name populated. Django's
        # .exclude(a="", b="") means NOT (a="" AND b=""), i.e. drop rows
        # where both fields are empty.
        qs = InsuranceCompany.objects.exclude(
            medical_policy_url="", medical_policy_name=""
        )
        if exclude_company_id is not None:
            qs = qs.exclude(id=exclude_company_id)
        # Surface major national commercial carriers ahead of regional BCBS
        # plans so the default top-N is representative.
        companies: List[InsuranceCompany] = list(
            qs.order_by("-is_major_commercial_payer", "name")[:max_payers]
        )
        # Keep matched entries scoped to the same payers we list in this
        # block; otherwise we could surface a Cigna-only policy URL while
        # the listed-payer block above features only Aetna and UHC, which
        # is confusing for the reader.
        allowed_company_ids = {c.id for c in companies}
        matched_entries = get_relevant_policy_entries(
            procedure=procedure,
            diagnosis=diagnosis,
            exclude_company_id=exclude_company_id,
            max_entries=max_entries,
        )
        matched_entries = [
            e for e in matched_entries if e.insurance_company_id in allowed_company_ids
        ]

        if not companies and not matched_entries:
            return ""

        lines: List[str] = [
            "## Comparative Payer Medical Policies",
            (
                "The following major commercial payers publish public "
                "medical-policy indices for services like this one. The "
                "appeal may point to the existence of these published "
                "policies as evidence that the requested kind of service "
                "is addressed in industry medical-policy literature."
            ),
            "",
        ]
        for c in companies:
            block = _format_company_block(c)
            if block:
                lines.append(block)

        if matched_entries:
            lines.append("")
            lines.append(
                "Specific matched policies (real titles and URLs from "
                "indexed payer policy lists):"
            )
            lines.extend(_format_entry_line(e) for e in matched_entries)
            lines.append("")
            lines.append(WITH_ENTRIES_GUARD)
        else:
            lines.append("")
            lines.append(NO_ENTRIES_GUARD)

        lines.append("")
        lines.append(COMPARATIVE_PAYER_CAVEAT)
        return "\n".join(lines)

    except Exception:
        logger.exception("Error building comparative payer policy context")
        return ""


def get_combined_payer_policy_context(
    company: Optional[InsuranceCompany],
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    include_comparative: bool = True,
    max_comparative_payers: int = 4,
    max_comparative_entries: int = 4,
) -> str:
    """Combine own-payer and comparative blocks; always honors the caveat."""
    parts: List[str] = []

    own = get_payer_policy_context(company, procedure=procedure, diagnosis=diagnosis)
    if own:
        parts.append(own)

    if include_comparative:
        comparative = get_comparative_payer_policy_context(
            exclude_company_id=company.id if company is not None else None,
            procedure=procedure,
            diagnosis=diagnosis,
            max_payers=max_comparative_payers,
            max_entries=max_comparative_entries,
        )
        if comparative:
            parts.append(comparative)

    return "\n\n".join(parts)
