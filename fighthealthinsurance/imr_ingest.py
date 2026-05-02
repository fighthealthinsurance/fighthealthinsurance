"""Parsers and ingestion helpers for public IMR / external-appeal datasets.

The CA DMHC IMR dataset is published on the California CHHS open data portal as
a CSV with a stable column schema (Reference ID, Report Year, Diagnosis
Category/Sub-Category, Treatment Category/Sub-Category, Determination, Type,
Age Range, Patient Gender, Findings). The NY DFS External Appeals database is
served as a search UI; we accept the same kind of normalized CSV export so the
loader can be reused.

The loader is idempotent: each source row maps to a single ``IMRDecision`` row
keyed by ``(source, case_id)``.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.models import IMRDecision


@dataclass
class ParsedIMRRow:
    """Normalized representation of a single IMR / external-appeal record."""

    source: str
    case_id: str
    state: str
    decision_year: Optional[int] = None
    decision_date: Optional[date] = None
    diagnosis: str = ""
    diagnosis_category: str = ""
    treatment: str = ""
    treatment_category: str = ""
    treatment_subcategory: str = ""
    determination: str = IMRDecision.DETERMINATION_OTHER
    decision_type: str = ""
    insurance_type: str = ""
    age_range: str = ""
    gender: str = ""
    findings: str = ""
    summary: str = ""
    source_url: str = ""
    raw_data: Optional[Dict] = None


# --- Header normalization ----------------------------------------------------


def _norm_header(h: str) -> str:
    return "".join(ch for ch in h.lower() if ch.isalnum())


def _normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of the row keyed by normalized (alnum-lowercase) headers."""
    return {_norm_header(k): v for k, v in row.items() if k}


def _lookup(normalized: Dict[str, str], *candidates: str) -> str:
    """First non-empty value among ``candidates`` in a pre-normalized row."""
    for c in candidates:
        v = normalized.get(_norm_header(c))
        if v:
            return v.strip()
    return ""


# --- Determination normalization --------------------------------------------

_DETERMINATION_MAP = {
    "overturned": IMRDecision.DETERMINATION_OVERTURNED,
    "overturneddecision": IMRDecision.DETERMINATION_OVERTURNED,
    "overturnedinpart": IMRDecision.DETERMINATION_OVERTURNED_IN_PART,
    "overturnedmodified": IMRDecision.DETERMINATION_OVERTURNED_IN_PART,
    "overturnedpartial": IMRDecision.DETERMINATION_OVERTURNED_IN_PART,
    "upheld": IMRDecision.DETERMINATION_UPHELD,
    "uphelddecision": IMRDecision.DETERMINATION_UPHELD,
    "upholddecision": IMRDecision.DETERMINATION_UPHELD,
    "upheldfinaldetermination": IMRDecision.DETERMINATION_UPHELD,
}


def _normalize_determination(raw: str) -> str:
    if not raw:
        return IMRDecision.DETERMINATION_OTHER
    key = "".join(ch for ch in raw.lower() if ch.isalnum())
    return _DETERMINATION_MAP.get(key, IMRDecision.DETERMINATION_OTHER)


def _parse_year(raw: str) -> Optional[int]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())[:4]
    if len(digits) == 4:
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _parse_date(raw: str) -> Optional[date]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


# --- Source-specific parsers -------------------------------------------------


def parse_ca_dmhc_row(row: Dict[str, str]) -> Optional[ParsedIMRRow]:
    """Parse a single row from the CA DMHC IMR CSV.

    Returns None when the row lacks a usable case identifier.
    """
    n = _normalize_row(row)
    case_id = _lookup(n, "Reference ID", "ReferenceID", "Case Number", "Case Id")
    if not case_id:
        return None
    treatment_subcategory = _lookup(
        n, "Treatment Sub Category", "TreatmentSubCategory", "Treatment SubCategory"
    )
    treatment_category = _lookup(n, "Treatment Category", "TreatmentCategory")
    return ParsedIMRRow(
        source=IMRDecision.SOURCE_CA_DMHC,
        case_id=case_id,
        state="CA",
        decision_year=_parse_year(_lookup(n, "Report Year", "ReportYear", "Year")),
        decision_date=_parse_date(
            _lookup(n, "Decision Date", "DecisionDate", "Determination Date")
        ),
        diagnosis_category=_lookup(n, "Diagnosis Category", "DiagnosisCategory"),
        diagnosis=_lookup(
            n,
            "Diagnosis Sub Category",
            "DiagnosisSubCategory",
            "Diagnosis SubCategory",
            "Diagnosis",
        ),
        treatment_category=treatment_category,
        treatment_subcategory=treatment_subcategory,
        # CA dataset has no top-level "Treatment" column; fall back from
        # subcategory to category so the searchable treatment field is set.
        treatment=treatment_subcategory or treatment_category,
        determination=_normalize_determination(
            _lookup(n, "Determination", "Outcome", "Decision")
        ),
        decision_type=_lookup(n, "Type", "IMR Type", "IMRType", "Decision Type"),
        age_range=_lookup(n, "Age Range", "AgeRange"),
        gender=_lookup(n, "Patient Gender", "PatientGender", "Gender"),
        findings=_lookup(n, "Findings", "Determination Rationale", "Rationale"),
        raw_data=dict(row),
    )


def parse_ny_dfs_row(row: Dict[str, str]) -> Optional[ParsedIMRRow]:
    """Parse a single row from a NY DFS external-appeals CSV export."""
    n = _normalize_row(row)
    case_id = _lookup(n, "Appeal Number", "AppealNumber", "Case ID", "Reference ID")
    if not case_id:
        return None
    return ParsedIMRRow(
        source=IMRDecision.SOURCE_NY_DFS,
        case_id=case_id,
        state="NY",
        decision_year=_parse_year(_lookup(n, "Year", "Decision Year", "DecisionYear")),
        decision_date=_parse_date(_lookup(n, "Decision Date", "DecisionDate", "Date")),
        diagnosis=_lookup(n, "Diagnosis", "Patient Diagnosis"),
        diagnosis_category=_lookup(n, "Diagnosis Category", "DiagnosisCategory"),
        treatment=_lookup(n, "Treatment", "Service", "Procedure"),
        treatment_category=_lookup(
            n, "Treatment Category", "TreatmentCategory", "Service Category"
        ),
        treatment_subcategory=_lookup(
            n, "Treatment Sub Category", "TreatmentSubCategory"
        ),
        determination=_normalize_determination(
            _lookup(n, "Determination", "Decision", "Outcome")
        ),
        decision_type=_lookup(
            n, "Appeal Type", "AppealType", "Reason", "Decision Reason"
        ),
        insurance_type=_lookup(n, "Plan Type", "PlanType", "Insurer Type"),
        age_range=_lookup(n, "Age Range", "AgeRange", "Age Group", "Age"),
        gender=_lookup(n, "Gender", "Patient Gender"),
        findings=_lookup(n, "Description", "Summary", "Findings", "Decision Detail"),
        raw_data=dict(row),
    )


# --- Loader ------------------------------------------------------------------


def iter_csv_rows(text: str) -> Iterator[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        yield {(k or ""): (v or "") for k, v in row.items()}


def load_parsed_rows(rows: Iterable[ParsedIMRRow]) -> Tuple[int, int]:
    """Upsert parsed rows. Returns ``(created, updated)`` counts."""
    created = 0
    updated = 0
    for row in rows:
        defaults = {
            "state": row.state,
            "decision_year": row.decision_year,
            "decision_date": row.decision_date,
            "diagnosis": row.diagnosis,
            "diagnosis_category": row.diagnosis_category,
            "treatment": row.treatment,
            "treatment_category": row.treatment_category,
            "treatment_subcategory": row.treatment_subcategory,
            "determination": row.determination,
            "decision_type": row.decision_type,
            "insurance_type": row.insurance_type,
            "age_range": row.age_range,
            "gender": row.gender,
            "findings": row.findings,
            "summary": row.summary,
            "source_url": row.source_url,
            "raw_data": row.raw_data,
        }
        try:
            _, was_created = IMRDecision.objects.update_or_create(
                source=row.source,
                case_id=row.case_id,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to upsert IMR decision {row.source}/{row.case_id}: {e}"
            )
    return created, updated


def fetch_csv(source_url: str, timeout: int = 120) -> str:
    """Download a CSV from ``source_url``. Imported lazily to keep import cost low."""
    import requests

    response = requests.get(source_url, timeout=timeout)
    response.raise_for_status()
    return response.text


def load_csv_text(
    csv_text: str,
    source: str,
    source_url: str = "",
) -> Tuple[int, int, int]:
    """Parse and load a CSV body. Returns ``(created, updated, skipped)``."""
    if source == IMRDecision.SOURCE_CA_DMHC:
        parser = parse_ca_dmhc_row
    elif source == IMRDecision.SOURCE_NY_DFS:
        parser = parse_ny_dfs_row
    else:
        raise ValueError(f"Unknown IMR source: {source}")

    parsed: List[ParsedIMRRow] = []
    skipped = 0
    for row in iter_csv_rows(csv_text):
        result = parser(row)
        if result is None:
            skipped += 1
            continue
        if source_url:
            result.source_url = source_url
        parsed.append(result)
    created, updated = load_parsed_rows(parsed)
    return created, updated, skipped
