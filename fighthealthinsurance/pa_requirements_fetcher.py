"""
Fetch and re-parse carrier-published prior-authorization requirement lists.

Where a carrier publishes a structured PA requirement document (CSV, XLSX,
or HTML table), a parser registered in ``PA_PARSERS`` knows how to fetch
the document, normalise each row, and yield it in the shape consumed by
``PaRequirementsFetcher._upsert``. The fetcher upserts via
``update_or_create`` keyed on the same tuple the lookup queries on, so
re-running an ingest is idempotent.

Rows previously seen for a carrier but absent from this run are
soft-retired (``end_date`` set to today) rather than deleted, so historical
denials still resolve to the rule that applied at the time.

This module ships the framework + an empty registry. Per-carrier parsers
land in follow-on PRs (one parser + matching fixture per carrier so the
parser output can be reviewed against a known-good file).
"""

from __future__ import annotations

import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
)

from asgiref.sync import sync_to_async
from loguru import logger

from fighthealthinsurance.models import (
    InsuranceCompany,
    PayerPriorAuthRequirement,
)


class PaParser(Protocol):
    """Protocol every per-carrier parser must satisfy.

    ``carrier_aliases`` is the priority-ordered list of name strings used
    to look up the canonical ``InsuranceCompany`` row by case-insensitive
    exact match (mirroring the ``_get_uhc`` selection logic from the
    historical seed migration). Returning ``None`` from ``parse`` signals
    a transient fetch failure so the surrounding code can warn but keep
    going for other parsers.
    """

    carrier_aliases: Tuple[str, ...]

    async def parse(self) -> Optional[List[Dict[str, Any]]]: ...


# Registry of registered parsers. Keys are the canonical alias used by the
# management command (``ingest_pa_requirements --carrier <key>``). Values
# are zero-arg callables returning a parser instance — this avoids
# importing every parser module at process start.
PA_PARSERS: Dict[str, Callable[[], PaParser]] = {}


def register_parser(
    alias: str,
) -> Callable[[Callable[[], PaParser]], Callable[[], PaParser]]:
    def decorator(factory: Callable[[], PaParser]) -> Callable[[], PaParser]:
        PA_PARSERS[alias] = factory
        return factory

    return decorator


# Same key shape lookup_pa_requirements queries on. A row is "the same row"
# when these fields all match; everything else is overwritten on upsert.
_UPSERT_KEY_FIELDS = (
    "insurance_company",
    "line_of_business",
    "state",
    "cpt_hcpcs_code",
    "code_range_start",
    "code_range_end",
)


def resolve_carrier_by_aliases(
    aliases: Tuple[str, ...],
) -> Optional[InsuranceCompany]:
    """Resolve the canonical ``InsuranceCompany`` row for a parser.

    Lifted from the historical ``_get_uhc`` helper in the now-removed
    seed migration so per-carrier parsers all use the same selector. We
    deliberately *only* match exact (case-insensitive) name strings —
    substring matches across closely-named carriers
    (e.g. "UnitedHealthcare" vs "UnitedHealthcare Community Plan") would
    silently mis-attach rows.

    Raises ``RuntimeError`` if multiple aliases match different rows so a
    data-integrity issue isn't quietly papered over.
    """
    matched: Optional[InsuranceCompany] = None
    for alias in aliases:
        row = InsuranceCompany.objects.filter(name__iexact=alias).first()
        if row is None:
            continue
        if matched is not None and row.pk != matched.pk:
            raise RuntimeError(
                f"Ambiguous carrier resolution: aliases match both "
                f"id={matched.pk!r} ({matched.name!r}) and "
                f"id={row.pk!r} ({row.name!r}). Resolve duplicate "
                "InsuranceCompany rows before re-running ingest."
            )
        matched = row
    return matched


class PaRequirementsFetcher:
    """Async context manager + ingestion driver.

    Mirrors ``PayerPolicyFetcher``'s shape so the ingest command and
    refresh actor have the same contract as the existing payer-policy
    pipeline (``ingest_payer_policy_indexes`` / its actor).
    """

    async def __aenter__(self) -> "PaRequirementsFetcher":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def ingest_all(self) -> Dict[str, int]:
        stats = {"fetched": 0, "failed": 0, "entries": 0, "retired": 0}
        for alias in list(PA_PARSERS):
            sub = await self.ingest_carrier(alias)
            stats["fetched"] += sub["fetched"]
            stats["failed"] += sub["failed"]
            stats["entries"] += sub["entries"]
            stats["retired"] += sub["retired"]
        return stats

    async def ingest_carrier(self, alias: str) -> Dict[str, int]:
        stats = {"fetched": 0, "failed": 0, "entries": 0, "retired": 0}

        factory = PA_PARSERS.get(alias)
        if factory is None:
            logger.warning(f"No PA parser registered for alias '{alias}'")
            stats["failed"] += 1
            return stats

        parser = factory()

        try:
            company = await sync_to_async(resolve_carrier_by_aliases)(
                parser.carrier_aliases
            )
        except RuntimeError as e:
            logger.error(f"Carrier resolution failed for '{alias}': {e}")
            stats["failed"] += 1
            return stats

        if company is None:
            logger.warning(
                f"No InsuranceCompany matched aliases {parser.carrier_aliases!r} "
                f"for parser '{alias}'; skipping ingest."
            )
            stats["failed"] += 1
            return stats

        try:
            rows = await parser.parse()
        except Exception as e:
            logger.opt(exception=True).warning(
                f"PA parser '{alias}' raised while fetching: {e}"
            )
            stats["failed"] += 1
            return stats

        if rows is None:
            stats["failed"] += 1
            return stats

        stats["fetched"] += 1
        seen_pks, written = await sync_to_async(self._upsert_rows)(company, rows)
        stats["entries"] += written
        stats["retired"] += await sync_to_async(self._retire_missing)(company, seen_pks)
        return stats

    @staticmethod
    def _upsert_rows(
        company: InsuranceCompany, rows: List[Dict[str, Any]]
    ) -> Tuple[List[int], int]:
        """Upsert each row; return ``(pks_seen_this_run, count)``."""
        seen: List[int] = []
        for row in rows:
            key = {
                "insurance_company": company,
                "line_of_business": row.get("line_of_business", ""),
                "state": row.get("state", ""),
                "cpt_hcpcs_code": row.get("cpt_hcpcs_code", ""),
                "code_range_start": row.get("code_range_start", ""),
                "code_range_end": row.get("code_range_end", ""),
            }
            defaults = {k: v for k, v in row.items() if k not in _UPSERT_KEY_FIELDS}
            # A re-fetched row that was previously soft-retired is now
            # current again; clear end_date unless the parser explicitly
            # set one.
            defaults.setdefault("end_date", None)
            obj, _ = PayerPriorAuthRequirement.objects.update_or_create(
                **key, defaults=defaults
            )
            seen.append(obj.pk)
        return seen, len(seen)

    @staticmethod
    def _retire_missing(company: InsuranceCompany, seen_pks: List[int]) -> int:
        """Soft-retire rows for this carrier that weren't seen this run.

        Operates only on rows whose ``source_document`` indicates an
        ingestion-owned origin (looking for the ``[ingest:...]`` marker
        the upsert path writes), so manually-curated and fixture-loaded
        rows are never touched.
        """
        today = datetime.date.today()
        qs = (
            PayerPriorAuthRequirement.objects.filter(insurance_company=company)
            .filter(source_document__contains="[ingest:")
            .exclude(pk__in=seen_pks)
            .filter(end_date__isnull=True)
        )
        return qs.update(end_date=today)
