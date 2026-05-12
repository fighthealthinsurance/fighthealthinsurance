"""
Parsers for payer prior-authorization requirement lists.

Each parser takes raw content (HTML text, PDF bytes, or CSV/Excel bytes)
and returns a list of ``ParsedPARequirement`` records. The orchestrator
(``pa_requirement_fetcher``) writes these as ``PayerPriorAuthRequirement``
rows that ``pa_requirements`` later looks up at appeal time.

Adding support for a new payer means:
  1. Identify the payer's publicly accessible PA requirement list URL.
  2. Set ``pa_requirement_list_url`` and ``pa_requirement_list_url_is_parseable=True``
     on the InsuranceCompany fixture/admin row.
  3. Write a parser here returning ``ParsedPARequirement`` records.
  4. Register it in ``PARSERS_BY_HOST`` keyed on the URL hostname, or add a
     content-sniffing entry to ``PARSERS_BY_CONTENT_TYPE``.

Content-type dispatch:
  - HTML  → ``parse_html_pa_table`` (generic) or a host-specific parser
  - PDF   → ``parse_pdf_pa_list`` (generic text extraction + code scanning)
  - Excel → ``parse_excel_pa_list`` (generic column-mapping heuristic)
  - CSV   → ``parse_csv_pa_list`` (same heuristic as Excel)
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------


@dataclass
class ParsedPARequirement:
    """One row of a parsed PA requirement list, ready to upsert into the DB.

    Exactly one of ``cpt_hcpcs_code`` or the ``code_range_start`` /
    ``code_range_end`` pair is populated; the fetcher writes whichever is
    set into the matching ``PayerPriorAuthRequirement`` column.
    """

    cpt_hcpcs_code: str = ""
    code_range_start: str = ""
    code_range_end: str = ""
    code_description: str = ""
    requires_pa: bool = True
    notification_only: bool = False
    pa_category: str = ""
    criteria_reference: str = ""
    criteria_url: str = ""
    submission_channel: str = ""
    line_of_business: str = "all"
    state: str = ""
    notes: str = ""
    source_document: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CPT_BARE = re.compile(r"^\s*(\d{5})\s*$")
_HCPCS_BARE = re.compile(r"^\s*([ABCDEGHJKLPQRSTV]\d{4})\s*$", re.IGNORECASE)
_CODE_RANGE = re.compile(
    r"^\s*(\d{5}|\b[ABCDEGHJKLPQRSTV]\d{4})\s*[-–]\s*(\d{5}|[ABCDEGHJKLPQRSTV]\d{4})\s*$",
    re.IGNORECASE,
)
_INLINE_CODE = re.compile(
    r"\b(\d{5}|[ABCDEGHJKLPQRSTV]\d{4})(?:[-\s][A-Z0-9]{2})?\b", re.IGNORECASE
)

# Column header keywords → canonical field names
_COLUMN_ALIASES: Dict[str, str] = {
    # code column
    "procedure code": "code",
    "proc code": "code",
    "cpt": "code",
    "hcpcs": "code",
    "cpt/hcpcs": "code",
    "cpt code": "code",
    "hcpcs code": "code",
    "service code": "code",
    "code": "code",
    # description
    "procedure description": "description",
    "description": "description",
    "service description": "description",
    "service name": "description",
    # PA required
    "prior authorization required": "requires_pa",
    "prior auth required": "requires_pa",
    "pa required": "requires_pa",
    "pa": "requires_pa",
    "requires pa": "requires_pa",
    "auth required": "requires_pa",
    # notification only
    "advance notification": "notification_only",
    "advance notification required": "notification_only",
    "notification only": "notification_only",
    "advance notice": "notification_only",
    # category
    "category": "category",
    "pa category": "category",
    "authorization category": "category",
    # criteria / policy
    "criteria": "criteria",
    "criteria reference": "criteria",
    "coverage criteria": "criteria",
    "medical policy": "criteria",
    "clinical policy": "criteria",
    "policy name": "criteria",
    # submission
    "submission channel": "submission",
    "how to submit": "submission",
    "submit via": "submission",
    # line of business
    "line of business": "lob",
    "lob": "lob",
    "plan type": "lob",
    # state
    "state": "state",
}

# Boolean-like cell values
_TRUTHY = {"yes", "y", "true", "1", "required", "req", "x", "✓", "✔"}
_FALSY = {"no", "n", "false", "0", "not required", "not req", "excluded"}


def _bool_cell(cell: str) -> Optional[bool]:
    """Convert a cell value to bool; returns None when the value is ambiguous."""
    cleaned = cell.strip().lower()
    if cleaned in _TRUTHY:
        return True
    if cleaned in _FALSY:
        return False
    return None


def _normalize_header(raw: str) -> str:
    return raw.strip().lower().replace("\n", " ").replace("  ", " ")


def _map_columns(headers: List[str]) -> Dict[int, str]:
    """Map column indices to canonical field names based on header text."""
    mapping: Dict[int, str] = {}
    for i, h in enumerate(headers):
        key = _normalize_header(h)
        canonical = _COLUMN_ALIASES.get(key)
        if canonical:
            mapping[i] = canonical
    return mapping


def _rows_to_requirements(
    column_map: Dict[int, str],
    rows: List[List[str]],
    source_document: str = "",
) -> List[ParsedPARequirement]:
    """Convert tabular rows (already header-mapped) into ParsedPARequirement records."""
    out: List[ParsedPARequirement] = []
    has_code_col = any(v == "code" for v in column_map.values())
    if not has_code_col:
        logger.debug("No code column identified in column map; skipping table")
        return out

    for row in rows:
        if not any(cell.strip() for cell in row):
            continue

        code_raw = ""
        description = ""
        requires_pa = True
        notification_only = False
        category = ""
        criteria = ""
        submission = ""
        lob = "all"
        state = ""
        notes_parts: List[str] = []

        for idx, canonical in column_map.items():
            if idx >= len(row):
                continue
            cell = (row[idx] or "").strip()
            if canonical == "code":
                code_raw = cell
            elif canonical == "description":
                description = cell
            elif canonical == "requires_pa":
                v = _bool_cell(cell)
                if v is not None:
                    requires_pa = v
            elif canonical == "notification_only":
                v = _bool_cell(cell)
                if v is not None:
                    notification_only = v
            elif canonical == "category":
                category = cell
            elif canonical == "criteria":
                criteria = cell
            elif canonical == "submission":
                submission = cell
            elif canonical == "lob":
                lob = cell.lower() if cell else "all"
            elif canonical == "state":
                state = cell.upper()[:2] if cell else ""

        if not code_raw:
            continue

        range_m = _CODE_RANGE.match(code_raw)
        if range_m:
            out.append(
                ParsedPARequirement(
                    code_range_start=range_m.group(1).upper(),
                    code_range_end=range_m.group(2).upper(),
                    code_description=description,
                    requires_pa=requires_pa,
                    notification_only=notification_only,
                    pa_category=category,
                    criteria_reference=criteria,
                    submission_channel=submission,
                    line_of_business=lob,
                    state=state,
                    notes="; ".join(notes_parts),
                    source_document=source_document,
                )
            )
            continue

        for m in _INLINE_CODE.finditer(code_raw):
            code = m.group(1).upper()
            out.append(
                ParsedPARequirement(
                    cpt_hcpcs_code=code,
                    code_description=description,
                    requires_pa=requires_pa,
                    notification_only=notification_only,
                    pa_category=category,
                    criteria_reference=criteria,
                    submission_channel=submission,
                    line_of_business=lob,
                    state=state,
                    notes="; ".join(notes_parts),
                    source_document=source_document,
                )
            )
    return out


# ---------------------------------------------------------------------------
# HTML table parser (generic)
# ---------------------------------------------------------------------------


def parse_html_pa_table(html: str) -> List[ParsedPARequirement]:
    """
    Extract PA requirements from an HTML page that contains one or more
    ``<table>`` elements with CPT/HCPCS code columns. JS-rendered content
    is not supported.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: List[ParsedPARequirement] = []

    for table in soup.find_all("table"):
        headers: List[str] = []
        rows_data: List[List[str]] = []

        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [th.get_text(" ", strip=True) for th in header_row.find_all(["th", "td"])]

        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row_texts = [c.get_text(" ", strip=True) for c in cells]
            if not headers:
                if any(_normalize_header(t) in _COLUMN_ALIASES for t in row_texts):
                    headers = row_texts
                    continue
            rows_data.append(row_texts)

        if not headers or not rows_data:
            continue
        col_map = _map_columns(headers)
        out.extend(_rows_to_requirements(col_map, rows_data))

    return out


# ---------------------------------------------------------------------------
# PDF parser (generic, using pymupdf)
# ---------------------------------------------------------------------------


def parse_pdf_pa_list(pdf_bytes: bytes, source_name: str = "") -> List[ParsedPARequirement]:
    """
    Extract PA requirements from a PDF document.

    Uses pymupdf to extract text, then scans for CPT/HCPCS codes. When the
    PDF contains tables (detected by pymupdf's table-extraction feature),
    attempts structured column mapping; otherwise falls back to code scanning
    with surrounding-text heuristics.
    """
    try:
        import pymupdf  # type: ignore[import]
    except ImportError:
        logger.warning("pymupdf not installed; cannot parse PDF PA lists")
        return []

    out: List[ParsedPARequirement] = []
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                tables = page.find_tables()
                if tables and tables.tables:
                    for table in tables.tables:
                        rows = table.extract()
                        if not rows or len(rows) < 2:
                            continue
                        headers = [str(c or "") for c in rows[0]]
                        col_map = _map_columns(headers)
                        if any(v == "code" for v in col_map.values()):
                            data_rows = [[str(c or "") for c in r] for r in rows[1:]]
                            out.extend(_rows_to_requirements(col_map, data_rows, source_name))
                            continue
                text = page.get_text()
                # Dedupe per-page so a 100-page PDF doesn't accumulate
                # thousands of near-duplicate context-snippet records before
                # the final dedup at the bottom.
                out.extend(_dedupe(_scan_text_for_codes(text, source_name)))
    except Exception:
        logger.opt(exception=True).warning(f"Failed to parse PDF PA list: {source_name!r}")

    return _dedupe(out)


def _scan_text_for_codes(
    text: str, source_document: str = ""
) -> List[ParsedPARequirement]:
    """
    Scan free-form text (PDF page text) for CPT/HCPCS codes accompanied by
    PA-requirement keywords. Returns one record per distinct code found.
    """
    out: List[ParsedPARequirement] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in _INLINE_CODE.finditer(line):
            code = m.group(1).upper()
            context = " ".join(lines[max(0, i - 1) : i + 3])
            ctx_lower = context.lower()
            requires_pa = True
            notification_only = False
            if any(neg in ctx_lower for neg in ("not required", "no pa", "excluded", "does not require")):
                requires_pa = False
            elif "notification" in ctx_lower and "authorization" not in ctx_lower:
                notification_only = True
            description = line.replace(m.group(0), "").strip()[:200]
            out.append(
                ParsedPARequirement(
                    cpt_hcpcs_code=code,
                    code_description=description,
                    requires_pa=requires_pa,
                    notification_only=notification_only,
                    source_document=source_document,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Excel / CSV parsers (generic)
# ---------------------------------------------------------------------------


def parse_excel_pa_list(
    excel_bytes: bytes, source_name: str = ""
) -> List[ParsedPARequirement]:
    """
    Parse a PA requirement Excel workbook. Uses openpyxl for .xlsx and falls
    back to pandas when available. Works through each sheet and picks the one
    with the most recognised PA columns.
    """
    try:
        import openpyxl  # type: ignore[import]
    except ImportError:
        logger.warning("openpyxl not installed; trying pandas for Excel parsing")
        return _parse_excel_pandas(excel_bytes, source_name)

    out: List[ParsedPARequirement] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), read_only=True, data_only=True)
        for sheet in wb.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            # Find header row: first row with at least one recognised column alias
            header_row_idx = None
            for ri, row in enumerate(rows[:10]):
                row_texts = [str(c or "").strip() for c in row]
                if sum(
                    1 for t in row_texts if _normalize_header(t) in _COLUMN_ALIASES
                ) >= 1:
                    header_row_idx = ri
                    break
            if header_row_idx is None:
                continue
            headers = [str(c or "").strip() for c in rows[header_row_idx]]
            col_map = _map_columns(headers)
            if not any(v == "code" for v in col_map.values()):
                continue
            data_rows = [
                [str(c or "").strip() for c in row]
                for row in rows[header_row_idx + 1 :]
            ]
            out.extend(_rows_to_requirements(col_map, data_rows, source_name))
        wb.close()
    except Exception:
        logger.opt(exception=True).warning(f"openpyxl failed for {source_name!r}")

    return _dedupe(out)


def _parse_excel_pandas(excel_bytes: bytes, source_name: str = "") -> List[ParsedPARequirement]:
    try:
        import pandas as pd  # type: ignore[import]
    except ImportError:
        logger.warning("Neither openpyxl nor pandas is available; cannot parse Excel PA list")
        return []
    out: List[ParsedPARequirement] = []
    try:
        xl = pd.ExcelFile(io.BytesIO(excel_bytes))
        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name, dtype=str).fillna("")
            headers = list(df.columns)
            col_map = _map_columns(headers)
            if not any(v == "code" for v in col_map.values()):
                continue
            rows = df.values.tolist()
            rows_str = [[str(c) for c in row] for row in rows]
            out.extend(_rows_to_requirements(col_map, rows_str, source_name))
    except Exception:
        logger.opt(exception=True).warning(f"pandas Excel parse failed for {source_name!r}")
    return _dedupe(out)


def parse_csv_pa_list(csv_bytes: bytes, source_name: str = "") -> List[ParsedPARequirement]:
    """Parse a CSV file with a header row containing CPT/HCPCS code columns."""
    import csv

    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    col_map = _map_columns(rows[0])
    if not any(v == "code" for v in col_map.values()):
        return []
    return _dedupe(_rows_to_requirements(col_map, rows[1:], source_name))


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _dedupe(requirements: List[ParsedPARequirement]) -> List[ParsedPARequirement]:
    """Remove duplicate codes (same code + LOB + state)."""
    seen: set = set()
    out: List[ParsedPARequirement] = []
    for req in requirements:
        key = (req.cpt_hcpcs_code, req.line_of_business, req.state)
        if key in seen:
            continue
        seen.add(key)
        out.append(req)
    return out


# ---------------------------------------------------------------------------
# Dispatch — generic parsers keyed by content-type, payer enrichment by host
# ---------------------------------------------------------------------------

# A raw parser takes the response body (str for HTML/CSV, bytes for PDF/Excel)
# plus an optional source-name tag, and returns ParsedPARequirement records.
RawParser = Callable[..., List[ParsedPARequirement]]

# (raw_parser, expects_bytes)
ParserSpec = Tuple[RawParser, bool]

PARSERS_BY_CONTENT_TYPE: Dict[str, ParserSpec] = {
    "text/html": (parse_html_pa_table, False),
    "application/xhtml+xml": (parse_html_pa_table, False),
    "application/pdf": (parse_pdf_pa_list, True),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        parse_excel_pa_list,
        True,
    ),
    "application/vnd.ms-excel": (parse_excel_pa_list, True),
    "text/csv": (parse_csv_pa_list, False),
}


@dataclass(frozen=True)
class HostEnrichment:
    """Payer-specific defaults applied after a generic parser runs.

    ``submission_channel`` is written into any record whose channel is empty;
    ``default_lob`` replaces an empty / ``"all"`` line_of_business so a
    generic commercial list gets correctly tagged for downstream lookups.
    """

    submission_channel: str = ""
    default_lob: str = ""


HOST_ENRICHMENTS: Dict[str, HostEnrichment] = {
    "uhcprovider.com": HostEnrichment(
        submission_channel="UHCprovider.com or 1-866-889-8054",
        default_lob="commercial",
    ),
    "aetna.com": HostEnrichment(
        submission_channel="Aetna provider portal (NaviMedix AuthPortal) or 1-800-AETNA-PA",
        default_lob="commercial",
    ),
    "cigna.com": HostEnrichment(
        submission_channel="eviCore (evicore.com) or 1-888-564-3650",
    ),
    "humana.com": HostEnrichment(
        submission_channel="Availity (availity.com) or 1-800-626-2741",
    ),
    "fepblue.org": HostEnrichment(
        submission_channel="BlueCard program portal or 1-800-972-8382",
    ),
}


def enrichment_for_host(host: str) -> HostEnrichment:
    """Look up payer defaults by exact host or registrable-domain suffix."""
    host = (host or "").lower()
    if host in HOST_ENRICHMENTS:
        return HOST_ENRICHMENTS[host]
    # Match registrable domain (strip leading "www." or other subdomain levels)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in HOST_ENRICHMENTS:
            return HOST_ENRICHMENTS[candidate]
    return HostEnrichment()


def apply_enrichment(
    requirements: List[ParsedPARequirement], enrichment: HostEnrichment
) -> None:
    """Mutate records in place to fill empty submission channel / LOB."""
    if not (enrichment.submission_channel or enrichment.default_lob):
        return
    for req in requirements:
        if enrichment.submission_channel and not req.submission_channel:
            req.submission_channel = enrichment.submission_channel
        if enrichment.default_lob and req.line_of_business in ("", "all"):
            req.line_of_business = enrichment.default_lob
