"""Ingestion for the monthly worst-insurance-by-state rankings artifact.

The worst-insurance-pipeline-by-state repo publishes a schema-versioned
``rankings.json`` (see that repo's METHODOLOGY.md) computed from public
regulator data. This loader validates the document and idempotently upserts
one ``WorstInsuranceReport`` per period plus its ``WorstInsuranceRanking``
rows, mirroring the structure of ``imr_ingest.py``.
"""

import re
from datetime import date, datetime
from typing import Dict, Optional, Tuple

from django.db import transaction

from loguru import logger

from fighthealthinsurance.models import (
    InsuranceCompany,
    WorstInsuranceRanking,
    WorstInsuranceReport,
)

# The pipeline bumps schema_version only on breaking shape changes; refuse
# anything else rather than mis-render public accusations.
SUPPORTED_SCHEMA_VERSION = 1


def fetch_json(source_url: str, timeout: int = 120) -> Dict:
    """Download the rankings JSON. Imported lazily to keep import cost low."""
    import requests

    response = requests.get(source_url, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("rankings document must be a JSON object")
    return result


def validate_report_dict(data: Dict) -> None:
    """Raise ``ValueError`` when the document isn't an ingestable report."""
    if not isinstance(data, dict):
        raise ValueError("rankings document must be a JSON object")
    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported rankings schema_version {version!r} "
            f"(supported: {SUPPORTED_SCHEMA_VERSION})"
        )
    for key in ("period", "states"):
        if not data.get(key):
            raise ValueError(f"rankings document missing required key: {key}")
    # period must be zero-padded YYYY-MM: it is stored in a max_length=7 field
    # and ordered lexicographically to pick the latest report, so a malformed
    # value (e.g. "2026-9" or "2026-07-01") would sort wrong.
    if not re.fullmatch(r"\d{4}-\d{2}", str(data["period"])):
        raise ValueError(f"rankings 'period' must be YYYY-MM, got {data['period']!r}")
    if not isinstance(data["states"], dict):
        raise ValueError("rankings 'states' must be an object")


def match_insurance_company(issuer_name: str) -> Optional[InsuranceCompany]:
    """Best-effort match of a pipeline issuer name to an InsuranceCompany.

    Sync port of the strategy in
    ``DenialCreatorHelper._match_insurance_company`` (common_view_logic.py),
    which is async and coupled to denial text: exact name match, then
    specificity-scored substring/alt_name match, then each company's
    ``regex``/``negative_regex`` applied to the issuer name.
    """
    if not issuer_name:
        return None
    text_lower = issuer_name.lower()

    exact = InsuranceCompany.objects.filter(name__iexact=issuer_name).first()
    if exact:
        return exact

    matches: list[Tuple[InsuranceCompany, float]] = []
    companies = list(
        InsuranceCompany.objects.only(
            "id", "name", "alt_names", "regex", "negative_regex"
        )
    )
    for company in companies:
        company_lower = company.name.lower()
        if company_lower in text_lower:
            matches.append((company, len(company_lower) / len(text_lower) * 90))
        elif text_lower in company_lower:
            matches.append((company, len(text_lower) / len(company_lower) * 80))
        if company.alt_names:
            for alt in company.alt_names.split("\n"):
                alt = alt.strip().lower()
                if not alt:
                    continue
                if alt == text_lower:
                    matches.append((company, 95.0))
                elif alt in text_lower:
                    matches.append((company, len(alt) / len(text_lower) * 85))
                elif text_lower in alt:
                    matches.append((company, len(text_lower) / len(alt) * 75))

    if not matches:
        for company in companies:
            if not company.regex or not company.regex.pattern:
                continue
            try:
                if company.regex.search(issuer_name):
                    if (
                        company.negative_regex
                        and company.negative_regex.pattern
                        and company.negative_regex.search(issuer_name)
                    ):
                        continue
                    matches.append((company, 60.0))
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Error applying regex for company {company.id}: {e}"
                )

    if not matches:
        return None
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[0][0]


def _parse_iso_date(raw: object) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _parse_iso_datetime(raw: object) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_report_dict(data: Dict, source_url: str = "") -> Tuple[int, int, int, int]:
    """Upsert a validated rankings document, atomically.

    Returns ``(created, updated, deleted, match_failures)`` ranking-row
    counts; ``match_failures`` counts rows with no InsuranceCompany match
    (they still load — the FK is a nice-to-have for cross-linking).

    The whole upsert runs in a single transaction: if any row is malformed
    the transaction rolls back and the previously-ingested report is left
    untouched, rather than publishing a partially-updated report.
    """
    validate_report_dict(data)
    window = data.get("window") or {}
    states = data["states"]

    created = 0
    updated = 0
    match_failures = 0
    state_statuses = {
        state: {
            "status": block.get("status", "insufficient_data"),
            "reason": block.get("reason", ""),
            "notes": block.get("notes", []),
        }
        for state, block in states.items()
        if block.get("status") != "ok"
    }

    # Cache matches per issuer name: the same brand appears in many states.
    company_cache: Dict[str, Optional[InsuranceCompany]] = {}

    with transaction.atomic():
        report, _ = WorstInsuranceReport.objects.update_or_create(
            period=str(data["period"]),
            defaults={
                "methodology_version": str(data.get("methodology_version", "")),
                "generated_at": _parse_iso_datetime(data.get("generated_at")),
                "window_start": _parse_iso_date(window.get("start")),
                "window_end": _parse_iso_date(window.get("end")),
                "weights": data.get("weights"),
                "thresholds": data.get("thresholds"),
                "sources": data.get("sources"),
                "state_statuses": state_statuses,
                "source_url": source_url,
                "raw_data": data,
            },
        )

        seen_keys: set = set()
        for state, block in states.items():
            for issuer in block.get("issuers", []):
                issuer_name = str(issuer.get("issuer", "")).strip()
                issuer_slug = str(issuer.get("issuer_slug", "")).strip()
                if not issuer_name or not issuer_slug:
                    continue
                if issuer_name not in company_cache:
                    company_cache[issuer_name] = match_insurance_company(issuer_name)
                company = company_cache[issuer_name]
                if company is None:
                    match_failures += 1
                # No per-row try/except: a malformed row must abort the whole
                # transaction.atomic() below so a partial report is never
                # committed and the previous report (and its rows) survive
                # intact. This data is public-facing.
                _, was_created = WorstInsuranceRanking.objects.update_or_create(
                    report=report,
                    state=state,
                    issuer_slug=issuer_slug,
                    defaults={
                        "rank": int(issuer.get("rank", 0)),
                        "issuer_name": issuer_name,
                        "group_affiliation": str(
                            issuer.get("group_affiliation", "") or ""
                        ),
                        "composite_score": float(issuer.get("composite_score", 0.0)),
                        "weight_coverage": issuer.get("weight_coverage"),
                        "member_months": issuer.get("member_months"),
                        "metrics": issuer.get("metrics"),
                        "metrics_available": issuer.get("metrics_available"),
                        "insurance_company": company,
                    },
                )
                seen_keys.add((state, issuer_slug))
                if was_created:
                    created += 1
                else:
                    updated += 1

        # Issuers renamed or dropped between re-ingests of the same period
        # must not linger.
        stale = [
            row.id
            for row in report.rankings.only("id", "state", "issuer_slug")
            if (row.state, row.issuer_slug) not in seen_keys
        ]
        deleted = 0
        if stale:
            deleted, _ = WorstInsuranceRanking.objects.filter(id__in=stale).delete()

    return created, updated, deleted, match_failures
