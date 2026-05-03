"""Load CMS Medicare Physician Fee Schedule (PFS) data into UCRRate.

Generates one row per percentile in UCR_PERCENTILES per (HCPCS, locality, year)
via UCR_MEDICARE_PERCENTILE_MULTIPLIERS.

Idempotent: re-running the command upserts on the
(procedure_code, modifier, geographic_area, percentile, source, effective_date)
unique key on UCRRate.

CLI for ad-hoc loads from local files or URLs. The actor's source-refresh
loop calls `refresh_medicare_pfs` directly so periodic auto-download is
wired without invoking the management command.

CSV headers expected (case-insensitive):
  RVU file: hcpcs, locality, allowed_cents
  Locality file: locality, description
"""

import asyncio
import csv
import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

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
            help="Path to local RVU CSV (hcpcs, locality, allowed_cents). "
            "Required unless --rvu-url is given.",
        )
        parser.add_argument(
            "--locality-file",
            type=str,
            help="Path to local locality CSV (locality, description). "
            "Required unless --locality-url is given.",
        )
        parser.add_argument(
            "--rvu-url",
            type=str,
            help="Optional URL to fetch RVU CSV from (e.g. cms.gov annual file).",
        )
        parser.add_argument(
            "--locality-url",
            type=str,
            help="Optional URL to fetch locality CSV from.",
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
        if not (rvu_file or rvu_url) or not (locality_file or locality_url):
            raise CommandError(
                "Provide --rvu-file or --rvu-url AND --locality-file or --locality-url"
            )

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
        if result.dry_run:
            self.stdout.write(
                f"Parsed {result.localities} localities, {result.rates} HCPCS rows."
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: would write {result.would_write} derived percentile rows."
                )
            )
            return
        self.stdout.write(
            f"Parsed {result.localities} localities, {result.rates} HCPCS rows."
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {result.written} UCRRate rows ({result.skipped} skipped as already-current)."
            )
        )


# --------------------------------------------------------------- public API


class RefreshResult:
    """Plain results bag for refresh_medicare_pfs callers."""

    __slots__ = ("localities", "rates", "written", "skipped", "dry_run", "would_write")

    def __init__(
        self,
        *,
        localities: int,
        rates: int,
        written: int,
        skipped: int,
        dry_run: bool,
        would_write: int,
    ):
        self.localities = localities
        self.rates = rates
        self.written = written
        self.skipped = skipped
        self.dry_run = dry_run
        self.would_write = would_write


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

    URL inputs win over file paths if both are set. Both an RVU source and a
    locality source must be supplied (URL or path). Returns a RefreshResult
    with parse counts and write counts. Safe to invoke from the source-refresh
    actor loop (idempotent upsert keyed on UCRRate's unique constraint).
    """
    if not (rvu_url or rvu_file) or not (locality_url or locality_file):
        raise ValueError(
            "refresh_medicare_pfs requires both an RVU and a locality source"
        )

    rvu_csv, locality_csv = await _fetch_inputs(
        rvu_file=rvu_file,
        rvu_url=rvu_url,
        locality_file=locality_file,
        locality_url=locality_url,
    )

    effective_date = datetime.date(effective_year or datetime.date.today().year, 1, 1)
    localities = _parse_localities(locality_csv)
    rates = list(_parse_rvu_rows(rvu_csv))
    multipliers = settings.UCR_MEDICARE_PERCENTILE_MULTIPLIERS

    if dry_run:
        active_percentiles = sum(1 for p in UCR_PERCENTILES if p in multipliers)
        return RefreshResult(
            localities=len(localities),
            rates=len(rates),
            written=0,
            skipped=0,
            dry_run=True,
            would_write=len(rates) * active_percentiles,
        )

    # Bridge to sync ORM. thread_sensitive=True keeps the DB connection
    # local to one thread so a TestCase's atomic wrapper stays in scope
    # and rollbacks behave; asyncio.to_thread would escape to a worker
    # connection that commits independently.
    def _persist() -> tuple[int, int]:
        with transaction.atomic():
            area_by_code = _ensure_localities(localities)
            return _upsert_rates(
                rates=rates,
                area_by_code=area_by_code,
                effective_date=effective_date,
                percentiles=UCR_PERCENTILES,
                multipliers=multipliers,
            )

    written, skipped = await sync_to_async(_persist, thread_sensitive=True)()
    return RefreshResult(
        localities=len(localities),
        rates=len(rates),
        written=written,
        skipped=skipped,
        dry_run=False,
        would_write=0,
    )


# --------------------------------------------------------------- I/O helpers


async def _fetch_inputs(
    *,
    rvu_file: Optional[str],
    rvu_url: Optional[str],
    locality_file: Optional[str],
    locality_url: Optional[str],
) -> tuple[str, str]:
    """Read both inputs concurrently. URLs win over file paths if both are set."""
    rvu_task = _fetch_url(rvu_url) if rvu_url else _fetch_path(rvu_file)
    locality_task = (
        _fetch_url(locality_url) if locality_url else _fetch_path(locality_file)
    )
    rvu_csv, locality_csv = await asyncio.gather(rvu_task, locality_task)
    return rvu_csv, locality_csv


async def _fetch_path(path: Optional[str]) -> str:
    if not path:
        raise ValueError("CSV path required")
    return await asyncio.to_thread(Path(path).read_text)


async def _fetch_url(url: str) -> str:
    # 2-minute total cap so a slow or unresponsive cms.gov mirror can't hang
    # the management command indefinitely.
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            text: str = await response.text()
            return text


# ----------------------------------------------------------------- parsing


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
