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
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from loguru import logger

# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------


@dataclass
class ParsedPARequirement:
    """One row of a parsed PA requirement list, ready to upsert into the DB."""

    cpt_hcpcs_code: str
    code_description: str = ""
    requires_pa: bool = True
    notification_only: bool = False
    pa_category: str = ""
    criteria_reference: str = ""
    criteria_url: str = ""
    submission_channel: str = ""
    # LOB defaults to "all" so a generic commercial list doesn't need explicit tagging
    line_of_business: str = "all"
    state: str = ""
    notes: str = ""
    # Populated by the fetcher, not the parser
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

        # Handle ranges
        range_m = _CODE_RANGE.match(code_raw)
        if range_m:
            # Emit a synthetic single row; the fetcher will store this as a range
            req = ParsedPARequirement(
                cpt_hcpcs_code="",  # fetcher will fill code_range_start/end
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
            # Stash range info in notes so fetcher can parse it
            req = ParsedPARequirement(
                cpt_hcpcs_code=f"RANGE:{range_m.group(1).upper()}-{range_m.group(2).upper()}",
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
            out.append(req)
            continue

        # Single code (or multiple codes separated by commas or spaces)
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


def parse_html_pa_table(html: str, base_url: str = "") -> List[ParsedPARequirement]:
    """
    Extract PA requirements from an HTML page that contains one or more
    ``<table>`` elements with CPT/HCPCS code columns.

    Works best on simple, static tables. JS-rendered content is not supported.
    Uses ``BeautifulSoup`` for robust HTML handling; falls back to regex if bs4
    is unavailable.
    """
    try:
        from bs4 import BeautifulSoup

        return _parse_html_bs4(html, base_url)
    except ImportError:
        logger.warning("beautifulsoup4 not installed; falling back to regex HTML parser")
        return _parse_html_regex(html)


def _parse_html_bs4(html: str, base_url: str = "") -> List[ParsedPARequirement]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: List[ParsedPARequirement] = []

    for table in soup.find_all("table"):
        headers: List[str] = []
        rows_data: List[List[str]] = []

        # Prefer <thead><tr><th> but fall back to first <tr> with <th>
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
                # First data row becomes headers
                if any(_normalize_header(t) in _COLUMN_ALIASES for t in row_texts):
                    headers = row_texts
                    continue
            rows_data.append(row_texts)

        if not headers or not rows_data:
            continue
        col_map = _map_columns(headers)
        out.extend(_rows_to_requirements(col_map, rows_data))

    return out


def _parse_html_regex(html: str) -> List[ParsedPARequirement]:
    """Minimal regex fallback — captures tables as raw text, scans for CPT codes."""
    out: List[ParsedPARequirement] = []
    # Strip tags to get plain text and look for CPT-like codes with surrounding context
    text = re.sub(r"<[^>]+>", " ", html)
    for m in _INLINE_CODE.finditer(text):
        code = m.group(1).upper()
        # Grab ~100 chars of context for the description
        start = max(0, m.start() - 80)
        end = min(len(text), m.end() + 80)
        snippet = text[start:end].strip()
        out.append(
            ParsedPARequirement(
                cpt_hcpcs_code=code,
                notes=f"Extracted from HTML: {snippet[:200]}",
            )
        )
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
                # Try structured table extraction first
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
                # Fall back to plain text scanning
                text = page.get_text()
                page_reqs = _scan_text_for_codes(text, source_name)
                out.extend(page_reqs)
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
# UHC-specific parser
# ---------------------------------------------------------------------------

# UHC publishes their commercial PA requirement list as a downloadable document at
# https://www.uhcprovider.com/content/dam/provider/docs/public/prior-auth/
# comm-medical-drug-prior-auth-list.pdf  (PDF)
# and also as a searchable HTML tool. The PDF has consistent columnar layout:
# CPT Code | Description | PA Required | Advance Notification | Category | Criteria
#
# The generic PDF parser handles most of this, but UHC's document also has
# a clear text header structure we can use for smarter extraction.


_UHC_SUBMISSION_CHANNEL = "UHCprovider.com or 1-866-889-8054"
_UHC_LOB_MAP = {
    "commercial": "commercial",
    "medicare advantage": "medicare_advantage",
    "medicaid": "medicaid",
    "community plan": "medicaid",
    "dsnp": "dsnp",
    "dual": "dsnp",
}


def _infer_uhc_lob(text: str) -> str:
    lower = text.lower()
    for key, lob in _UHC_LOB_MAP.items():
        if key in lower:
            return lob
    return "commercial"


def parse_uhc_pa_pdf(pdf_bytes: bytes, source_name: str = "") -> List[ParsedPARequirement]:
    """
    UHC-specific PDF parser. Falls through to the generic PDF parser and then
    post-processes results to fill in the submission channel and LOB.
    """
    requirements = parse_pdf_pa_list(pdf_bytes, source_name)
    # Enrich with UHC-specific defaults for any record missing submission channel
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _UHC_SUBMISSION_CHANNEL
        if req.line_of_business == "all" and source_name:
            req.line_of_business = _infer_uhc_lob(source_name)
    return requirements


def parse_uhc_pa_html(html: str, source_name: str = "") -> List[ParsedPARequirement]:
    """
    UHC PA requirement HTML page parser.

    UHC's provider portal publishes PA requirement summaries as HTML tables.
    Applies the generic HTML parser then enriches with UHC-specific defaults.
    """
    requirements = parse_html_pa_table(html)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _UHC_SUBMISSION_CHANNEL
        if req.line_of_business == "all" and source_name:
            req.line_of_business = _infer_uhc_lob(source_name)
    return requirements


# ---------------------------------------------------------------------------
# Aetna-specific parser
# ---------------------------------------------------------------------------

_AETNA_SUBMISSION = "Aetna provider portal (NaviMedix AuthPortal) or 1-800-AETNA-PA"


def parse_aetna_pa_html(html: str, source_name: str = "") -> List[ParsedPARequirement]:
    """
    Parse Aetna's published prior-authorization code lists.

    Aetna publishes PA requirement HTML tables grouped by clinical category.
    The generic HTML table parser handles most layouts; this wrapper adds
    Aetna-specific submission channel and LOB defaults.
    """
    requirements = parse_html_pa_table(html)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _AETNA_SUBMISSION
        if req.line_of_business == "all":
            req.line_of_business = "commercial"
    return requirements


def parse_aetna_pa_pdf(pdf_bytes: bytes, source_name: str = "") -> List[ParsedPARequirement]:
    requirements = parse_pdf_pa_list(pdf_bytes, source_name)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _AETNA_SUBMISSION
    return requirements


# ---------------------------------------------------------------------------
# Cigna-specific parser
# ---------------------------------------------------------------------------

_CIGNA_SUBMISSION = "eviCore (evicore.com) or 1-888-564-3650"


def parse_cigna_pa_html(html: str, source_name: str = "") -> List[ParsedPARequirement]:
    requirements = parse_html_pa_table(html)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _CIGNA_SUBMISSION
    return requirements


# ---------------------------------------------------------------------------
# BCBS Federal Employee Program (FEP) parser
# ---------------------------------------------------------------------------

_BCBS_FEP_SUBMISSION = "BlueCard program portal or 1-800-972-8382"


def parse_bcbs_fep_pa_html(html: str, source_name: str = "") -> List[ParsedPARequirement]:
    requirements = parse_html_pa_table(html)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _BCBS_FEP_SUBMISSION
    return requirements


# ---------------------------------------------------------------------------
# Humana parser
# ---------------------------------------------------------------------------

_HUMANA_SUBMISSION = "Availity (availity.com) or 1-800-626-2741"


def parse_humana_pa_html(html: str, source_name: str = "") -> List[ParsedPARequirement]:
    requirements = parse_html_pa_table(html)
    for req in requirements:
        if not req.submission_channel:
            req.submission_channel = _HUMANA_SUBMISSION
    return requirements


# ---------------------------------------------------------------------------
# Content-type and host dispatch maps
# ---------------------------------------------------------------------------

# Map from URL hostname → (parser_fn, expects_bytes: bool)
# ``True`` for binary parsers (PDF/Excel); ``False`` for text parsers (HTML/CSV).
ParserSpec = Tuple[Callable, bool]

PARSERS_BY_HOST: Dict[str, ParserSpec] = {
    # UHC — provider.com hosts both HTML PA pages and downloadable PDFs
    "www.uhcprovider.com": (parse_uhc_pa_html, False),
    "uhcprovider.com": (parse_uhc_pa_html, False),
    # Aetna
    "www.aetna.com": (parse_aetna_pa_html, False),
    "aetna.com": (parse_aetna_pa_html, False),
    # Cigna
    "www.cigna.com": (parse_cigna_pa_html, False),
    "cigna.com": (parse_cigna_pa_html, False),
    # Humana
    "www.humana.com": (parse_humana_pa_html, False),
    "humana.com": (parse_humana_pa_html, False),
    # BCBS FEP
    "www.fepblue.org": (parse_bcbs_fep_pa_html, False),
    "fepblue.org": (parse_bcbs_fep_pa_html, False),
}

# Map from MIME content-type prefix → binary parser
PARSERS_BY_CONTENT_TYPE: Dict[str, Callable] = {
    "application/pdf": parse_pdf_pa_list,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": parse_excel_pa_list,
    "application/vnd.ms-excel": parse_excel_pa_list,
    "text/csv": parse_csv_pa_list,
}
