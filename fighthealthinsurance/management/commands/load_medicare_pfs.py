"""Load CMS Medicare Physician Fee Schedule (PFS) data into UCRRate.

Generates one row per percentile in UCR_PERCENTILES per (HCPCS, locality, year)
via UCR_MEDICARE_PERCENTILE_MULTIPLIERS.

Idempotent: re-running the command upserts on the
(procedure_code, modifier, geographic_area, percentile, source, effective_date)
unique key on UCRRate.

Two input formats are supported, auto-detected by the extracted filename:

1. **CMS PFREV (the National Payment Amount File)** — what cms.gov ships at
   https://www.cms.gov/medicare/payment/fee-schedules/physician/national-payment-amount-file.
   Distributed as a ZIP that nests another ZIP that contains
   `PFALL{year}AR.txt` (or similar `PFALL*` / `PFREV*` member). Headerless,
   quoted-positional CSV with columns: year, carrier, locality, HCPCS, modifier,
   non-facility price, facility price, plus admin flags. Locality codes are
   derived from the data itself; no separate locality file is required.

2. **Simple flat CSV** with `hcpcs, locality, allowed_cents` headers, paired
   with a locality CSV (`locality, description`). Used by tests and any
   pre-transformed data sets.

CLI for ad-hoc loads from local files or URLs. The actor's source-refresh loop
calls `refresh_medicare_pfs` directly so periodic auto-download works without
invoking the management command.
"""

import asyncio
import csv
import datetime
import io
import zipfile
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from loguru import logger

from fighthealthinsurance.models import UCRGeographicArea, UCRRate
from fighthealthinsurance.ucr_constants import (
    UCR_PERCENTILES,
    UCRAreaKind,
    UCRSource,
)


class Command(BaseCommand):
    help = "Load CMS Medicare PFS data into UCRRate (multiplier-derived percentiles)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--rvu-file",
            type=str,
            help="Path to local RVU input (PFREV ZIP or simple CSV). "
            "Required unless --rvu-url is given.",
        )
        parser.add_argument(
            "--locality-file",
            type=str,
            help="Path to local locality CSV (locality, description). Required "
            "for the simple-CSV format; ignored when the RVU input is PFREV "
            "(CMS embeds carrier+locality directly in the data rows).",
        )
        parser.add_argument(
            "--rvu-url",
            type=str,
            help="URL to fetch the RVU input from (PFREV ZIP or simple CSV).",
        )
        parser.add_argument(
            "--locality-url",
            type=str,
            help="URL to fetch the locality CSV from. Optional for PFREV input.",
        )
        parser.add_argument(
            "--effective-year",
            type=int,
            default=datetime.date.today().year,
            help="Effective year for the loaded rows (default: current year).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report counts but do not write to the database.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rvu_file = options.get("rvu_file")
        locality_file = options.get("locality_file")
        rvu_url = options.get("rvu_url")
        locality_url = options.get("locality_url")
        if not (rvu_file or rvu_url):
            raise CommandError("Provide --rvu-file or --rvu-url")

        result = asyncio.run(
            refresh_medicare_pfs(
                rvu_file=rvu_file,
                rvu_url=rvu_url,
                locality_file=locality_file,
                locality_url=locality_url,
                effective_year=options["effective_year"],
                dry_run=bool(options.get("dry_run")),
            )
        )
        self.stdout.write(
            f"Parsed {result.localities} localities, {result.rates} HCPCS rows "
            f"(format: {result.input_format})."
        )
        if result.dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: would write {result.would_write} derived percentile rows."
                )
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {result.written} UCRRate rows ({result.skipped} skipped as already-current)."
            )
        )


# --------------------------------------------------------------- public API


class LoaderInput(NamedTuple):
    """Result of fetching one input — text plus the filename hint we used to
    decide which parser to apply."""

    text: str
    filename: str


class RefreshResult:
    """Plain results bag for refresh_medicare_pfs callers."""

    __slots__ = (
        "localities",
        "rates",
        "written",
        "skipped",
        "dry_run",
        "would_write",
        "input_format",
    )

    def __init__(
        self,
        *,
        localities: int,
        rates: int,
        written: int,
        skipped: int,
        dry_run: bool,
        would_write: int,
        input_format: str,
    ):
        self.localities = localities
        self.rates = rates
        self.written = written
        self.skipped = skipped
        self.dry_run = dry_run
        self.would_write = would_write
        self.input_format = input_format


async def refresh_medicare_pfs(
    *,
    rvu_url: Optional[str] = None,
    locality_url: Optional[str] = None,
    rvu_file: Optional[str] = None,
    locality_file: Optional[str] = None,
    effective_year: Optional[int] = None,
    dry_run: bool = False,
) -> RefreshResult:
    """Download (or read) and upsert the CMS PFS rate set.

    URL inputs win over file paths if both are set. For PFREV input the
    locality file is optional — localities are derived from the data rows.
    For the simple-CSV format both inputs are required. Idempotent upsert
    keyed on UCRRate's unique constraint, so safe to invoke from the
    source-refresh actor loop on every cycle.
    """
    if not (rvu_url or rvu_file):
        raise ValueError("refresh_medicare_pfs requires an RVU source")

    rvu = await _fetch_input(url=rvu_url, path=rvu_file)
    is_pfrev = _looks_like_pfrev(rvu.filename)
    if not is_pfrev and not (locality_url or locality_file):
        raise ValueError(
            "Simple-CSV format requires a locality source; pass "
            "--locality-url/--locality-file or use a PFREV input."
        )

    if is_pfrev:
        rate_rows = list(_parse_pfrev_rows(rvu.text))
        localities = {row["locality"]: "" for row in rate_rows}
    else:
        loc_input = await _fetch_input(url=locality_url, path=locality_file)
        localities = _parse_localities(loc_input.text)
        rate_rows = list(_parse_rvu_rows(rvu.text))

    effective_date = datetime.date(effective_year or datetime.date.today().year, 1, 1)
    multipliers = settings.UCR_MEDICARE_PERCENTILE_MULTIPLIERS
    fmt = "pfrev" if is_pfrev else "simple-csv"

    if dry_run:
        active_percentiles = sum(1 for p in UCR_PERCENTILES if p in multipliers)
        return RefreshResult(
            localities=len(localities),
            rates=len(rate_rows),
            written=0,
            skipped=0,
            dry_run=True,
            would_write=len(rate_rows) * active_percentiles,
            input_format=fmt,
        )

    # Bridge to sync ORM. thread_sensitive=True keeps the DB connection
    # local to one thread so a TestCase's atomic wrapper stays in scope
    # and rollbacks behave; asyncio.to_thread would escape to a worker
    # connection that commits independently.
    def _persist() -> tuple[int, int]:
        with transaction.atomic():
            area_by_code = _ensure_localities(localities)
            return _upsert_rates(
                rates=rate_rows,
                area_by_code=area_by_code,
                effective_date=effective_date,
                percentiles=UCR_PERCENTILES,
                multipliers=multipliers,
            )

    written, skipped = await sync_to_async(_persist, thread_sensitive=True)()
    return RefreshResult(
        localities=len(localities),
        rates=len(rate_rows),
        written=written,
        skipped=skipped,
        dry_run=False,
        would_write=0,
        input_format=fmt,
    )


# --------------------------------------------------------------- I/O helpers


async def _fetch_input(*, url: Optional[str], path: Optional[str]) -> LoaderInput:
    """Read a URL or path and return its decoded text + the effective filename
    after any ZIP unwrapping. URL wins over path if both are set."""
    if url:
        data = await _fetch_url(url)
        filename = url.rsplit("/", 1)[-1]
    elif path:
        data = await asyncio.to_thread(Path(path).read_bytes)
        filename = Path(path).name
    else:
        raise ValueError("CSV path or URL required")
    return _unwrap_to_text(data, filename)


async def _fetch_url(url: str) -> bytes:
    # 2-minute total cap so a slow or unresponsive cms.gov mirror can't hang
    # the management command indefinitely. CMS is gated to common UA strings.
    timeout = aiohttp.ClientTimeout(total=120)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; fhi-ucr-loader/1.0)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()


_ZIP_MAGIC = b"PK\x03\x04"


def _unwrap_to_text(data: bytes, filename: str) -> LoaderInput:
    """Recursively unwrap nested ZIPs (CMS ships pfrev26a.zip → PFREV*.zip →
    PFALL*.txt) and return the deepest text payload + its filename."""
    if data.startswith(_ZIP_MAGIC):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [
                m
                for m in zf.namelist()
                if not m.endswith("/") and not m.lower().endswith(".pdf")
            ]
            if not members:
                raise ValueError(f"ZIP {filename!r} has no usable members")
            # Prefer PFALL/PFREV members, then any nested ZIP, then the
            # largest leftover (covers single-file ZIPs of either CSV or TXT).
            members.sort(
                key=lambda m: (
                    0 if "pfall" in m.lower() or "pfrev" in m.lower() else 1,
                    -zf.getinfo(m).file_size,
                )
            )
            member = members[0]
            inner = zf.read(member)
        return _unwrap_to_text(inner, Path(member).name)
    # CMS files are latin-1; falling back to that beats blowing up on a
    # stray non-UTF8 byte in a 100MB file.
    text = data.decode("utf-8", errors="replace")
    return LoaderInput(text=text, filename=filename)


# ----------------------------------------------------------------- parsing


def _looks_like_pfrev(filename: str) -> bool:
    name = filename.lower()
    return name.startswith("pfall") or name.startswith("pfrev")


def _normalize_row(row: dict[str, Any]) -> dict[str, str]:
    """Lowercase and BOM-strip CSV headers so subsequent .get() calls can use
    canonical lowercase keys regardless of how CMS happened to capitalize the
    export this year."""
    return {(k or "").strip().lstrip("﻿").lower(): (v or "") for k, v in row.items()}


def _parse_localities(csv_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    reader = csv.DictReader(csv_text.splitlines())
    for row in reader:
        norm = _normalize_row(row)
        code = norm.get("locality", "").strip()
        if not code:
            continue
        desc = norm.get("description", "").strip()
        out[code] = desc
    return out


def _parse_rvu_rows(csv_text: str) -> Iterable[dict[str, Any]]:
    reader = csv.DictReader(csv_text.splitlines())
    for row in reader:
        norm = _normalize_row(row)
        hcpcs = norm.get("hcpcs", "").strip().upper()
        locality = norm.get("locality", "").strip()
        allowed_raw = norm.get("allowed_cents", "").strip()
        if not hcpcs or not locality or not allowed_raw:
            continue
        try:
            allowed_cents = int(allowed_raw)
        except ValueError:
            logger.warning(
                "Skipping malformed allowed_cents={!r} for hcpcs={}",
                allowed_raw,
                hcpcs,
            )
            continue
        if allowed_cents < 0:
            # CMS files shouldn't contain negative allowed amounts; skip rather
            # than propagate them through multipliers into derived rates.
            logger.warning(
                "Skipping negative allowed_cents={} for hcpcs={} locality={}",
                allowed_cents,
                hcpcs,
                locality,
            )
            continue
        yield {
            "hcpcs": hcpcs,
            "locality": locality,
            "allowed_cents": allowed_cents,
        }


# Column positions in the PFREV / PFALL files. Headerless, quoted, positional.
# Verified against PFALL26AR.txt (CY 2026 release).
_PFREV_COL_CARRIER = 1
_PFREV_COL_LOCALITY = 2
_PFREV_COL_HCPCS = 3
_PFREV_COL_MODIFIER = 4
_PFREV_COL_NONFAC_PRICE = 5  # dollars (decimal); we use this as "allowed".
_PFREV_EXPECTED_COLS = 16


def _parse_pfrev_rows(text: str) -> Iterable[dict[str, Any]]:
    """Yield dicts shaped like _parse_rvu_rows from CMS PFREV/PFALL data.

    Locality keys combine carrier + locality so identical locality numbers
    across different carriers stay distinct (CMS reuses small-int locality
    codes within each MAC). Modifier is intentionally dropped to match the
    rest of the loader's modifier="" upsert path.
    """
    reader = csv.reader(text.splitlines())
    for row in reader:
        if len(row) < _PFREV_EXPECTED_COLS:
            continue
        hcpcs = row[_PFREV_COL_HCPCS].strip().upper()
        carrier = row[_PFREV_COL_CARRIER].strip()
        locality = row[_PFREV_COL_LOCALITY].strip()
        modifier = row[_PFREV_COL_MODIFIER].strip()
        price_raw = row[_PFREV_COL_NONFAC_PRICE].strip()
        if not hcpcs or not carrier or not locality:
            continue
        # Skip modifier-specific rows so we only ingest the canonical
        # (HCPCS, locality) row. Matches the rest of the loader's
        # modifier="" assumption.
        if modifier:
            continue
        try:
            allowed_cents = int(round(float(price_raw) * 100))
        except (ValueError, TypeError):
            continue
        if allowed_cents <= 0:
            # Many "carrier-priced" rows are 0.00 placeholders; skip.
            continue
        yield {
            "hcpcs": hcpcs,
            "locality": f"{carrier}-{locality}",
            "allowed_cents": allowed_cents,
        }


# ----------------------------------------------------------------- writers


def _ensure_localities(
    localities: dict[str, str],
) -> dict[str, UCRGeographicArea]:
    out: dict[str, UCRGeographicArea] = {}
    for code, desc in localities.items():
        area, _ = UCRGeographicArea.objects.get_or_create(
            kind=UCRAreaKind.MEDICARE_LOCALITY,
            code=code,
            defaults={"description": desc},
        )
        out[code] = area
    return out


def _upsert_rates(
    *,
    rates: list[dict[str, Any]],
    area_by_code: dict[str, UCRGeographicArea],
    effective_date: datetime.date,
    percentiles: tuple[int, ...],
    multipliers: dict[int, float],
) -> tuple[int, int]:
    written = 0
    skipped = 0
    for row in rates:
        area = area_by_code.get(row["locality"])
        if area is None:
            logger.warning("Skipping row for unknown locality {}", row["locality"])
            continue

        # We only persist percentile rows the helper actually queries
        # (UCR_PERCENTILES). The raw Medicare-allowed amount is preserved as
        # `metadata.medicare_allowed_cents` on each derived row so it remains
        # auditable without using a 0-percentile sentinel rejected by the
        # 1..100 CheckConstraint on UCRRate.
        for percentile in percentiles:
            multiplier = multipliers.get(percentile)
            if multiplier is None:
                continue
            derived_cents = int(round(row["allowed_cents"] * multiplier))
            if _upsert_one(
                procedure_code=row["hcpcs"],
                area=area,
                percentile=percentile,
                amount_cents=derived_cents,
                source=UCRSource.MEDICARE_PFS,
                effective_date=effective_date,
                metadata={
                    "derived_from": "medicare_pfs",
                    "multiplier": multiplier,
                    "medicare_allowed_cents": row["allowed_cents"],
                },
            ):
                written += 1
            else:
                skipped += 1
    return written, skipped


def _upsert_one(
    *,
    procedure_code: str,
    area: UCRGeographicArea,
    percentile: int,
    amount_cents: int,
    source: str,
    effective_date: datetime.date,
    metadata: dict,
) -> bool:
    """Returns True if a row was created OR an existing row's amount changed."""
    existing = UCRRate.objects.filter(
        procedure_code=procedure_code,
        modifier="",
        geographic_area=area,
        percentile=percentile,
        source=source,
        effective_date=effective_date,
    ).first()
    if existing is None:
        UCRRate.objects.create(
            procedure_code=procedure_code,
            modifier="",
            geographic_area=area,
            percentile=percentile,
            amount_cents=amount_cents,
            source=source,
            effective_date=effective_date,
            metadata=metadata,
        )
        return True
    if existing.amount_cents == amount_cents and existing.metadata == metadata:
        return False
    existing.amount_cents = amount_cents
    existing.metadata = metadata
    existing.save(update_fields=["amount_cents", "metadata"])
    return True
