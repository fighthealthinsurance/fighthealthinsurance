"""
Fetch + index payer prior-authorization requirement lists.

Iterates every ``InsuranceCompany`` with
``pa_requirement_list_url_is_parseable=True``, downloads each company's PA
requirement document, picks a parser from ``pa_requirement_parsers``
keyed on the response Content-Type, applies any per-host enrichment
(submission channel / default LOB), and upserts rows via
``update_or_create``.

Rows whose ``source_document`` starts with ``"auto:"`` are managed by
this pipeline: a row that was previously ingested but is absent from a
fresh fetch is soft-retired (``end_date`` set to today) rather than
deleted, so historical denials still resolve to the rule that applied
at the time. Manually-seeded rows (no ``"auto:"`` prefix — e.g. fixture-
loaded UHC starter set) are never touched.

Invoked by:
  * ``ingest_pa_requirements`` management command (deploy / dev)
  * ``PaRefreshActor`` (periodic refresh)
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import aiohttp
from channels.db import database_sync_to_async
from django.db import transaction
from loguru import logger

from fighthealthinsurance.models import (
    InsuranceCompany,
    LineOfBusiness,
    PayerPriorAuthRequirement,
)
from fighthealthinsurance.pa_requirement_parsers import (
    PARSERS_BY_CONTENT_TYPE,
    ParsedPARequirement,
    apply_enrichment,
    enrichment_for_host,
)

FETCH_TIMEOUT_SEC = 120
MAX_BYTES = 50 * 1024 * 1024  # 50 MB — PA lists can be large Excel/PDF files

# Rows whose ``source_document`` starts with this prefix are managed by the
# auto-ingest pipeline; ``_retire_missing`` only marks those as retired.
AUTO_SOURCE_PREFIX = "auto:"


class PaRequirementsFetcher:
    """Async context manager + ingestion driver.

    Mirrors ``PayerPolicyFetcher``'s shape so the management command and
    refresh actor have the same contract as the existing payer-policy
    pipeline (``ingest_payer_policy_indexes`` / its actor).
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: int = FETCH_TIMEOUT_SEC,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._max_bytes = max_bytes

    async def __aenter__(self) -> "PaRequirementsFetcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def ingest_all(self) -> Dict[str, int]:
        """Fetch + parse + persist for every parseable company."""
        stats: Dict[str, int] = {
            "fetched": 0,
            "failed": 0,
            "entries": 0,
            "retired": 0,
        }
        companies = await database_sync_to_async(
            lambda: list(
                InsuranceCompany.objects.filter(
                    pa_requirement_list_url_is_parseable=True,
                ).exclude(pa_requirement_list_url="")
            )
        )()

        if not companies:
            logger.info("No insurance companies flagged for PA requirement ingestion")
            return stats

        for company in companies:
            # Isolate per-company failures so one misbehaving payer
            # endpoint doesn't abort the rest of the run.
            try:
                sub = await self.ingest_company(company)
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"PA ingest failed for {company.name} "
                    f"({company.pa_requirement_list_url}): {e}"
                )
                stats["failed"] += 1
                continue
            stats["fetched"] += sub["fetched"]
            stats["failed"] += sub["failed"]
            stats["entries"] += sub["entries"]
            stats["retired"] += sub["retired"]

        logger.info(
            f"PA requirement ingest complete: {stats['fetched']} fetched, "
            f"{stats['failed']} failed, {stats['entries']} rows upserted, "
            f"{stats['retired']} retired"
        )
        return stats

    async def ingest_company(self, company: InsuranceCompany) -> Dict[str, int]:
        """Fetch, parse, and persist for one company. Returns per-run stats."""
        stats = {"fetched": 0, "failed": 0, "entries": 0, "retired": 0}
        try:
            parsed = await self.parse_company(company)
        except LookupError as e:
            logger.warning(str(e))
            stats["failed"] += 1
            return stats

        if not parsed:
            logger.info(f"No PA requirements parsed for {company.name}")
            stats["fetched"] += 1
            return stats

        stats["fetched"] += 1
        source_name = f"{AUTO_SOURCE_PREFIX}{company.pa_requirement_list_url}"
        written, retired = await database_sync_to_async(self._persist_company)(
            company, parsed, source_name
        )
        stats["entries"] += written
        stats["retired"] += retired
        return stats

    async def parse_company(
        self, company: InsuranceCompany
    ) -> List[ParsedPARequirement]:
        """Fetch + parse a company's PA list without writing to the DB.

        Used by ``ingest_company`` and the management command's
        ``--dry-run`` mode so both share the same dispatch / enrichment
        pipeline. Raises ``LookupError`` when no parser matches the
        response Content-Type.
        """
        # Bind to a plain str — django-stubs types URLField access as
        # ``str | Combinable``, which mypy won't accept where ``str`` is
        # expected (``_get_content``, ``urlparse``).
        url: str = str(company.pa_requirement_list_url or "")
        if not url:
            return []

        content_type, raw = await self._get_content(url)
        ct_key = (content_type or "").split(";")[0].strip().lower()
        parser_spec = PARSERS_BY_CONTENT_TYPE.get(ct_key)
        if parser_spec is None:
            raise LookupError(
                f"No PA parser registered for Content-Type {ct_key!r} "
                f"(company {company.name}, url {url})"
            )

        parser_fn, expects_bytes = parser_spec
        source_name = f"{AUTO_SOURCE_PREFIX}{url}"

        body: Union[str, bytes]
        if expects_bytes:
            body = (
                raw if isinstance(raw, bytes) else raw.encode("utf-8", errors="replace")
            )
        else:
            body = (
                raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            )

        def _parse_and_enrich() -> List[ParsedPARequirement]:
            reqs = parser_fn(body, source_name)
            apply_enrichment(reqs, enrichment_for_host(urlparse(url).hostname or ""))
            return reqs

        result: List[ParsedPARequirement] = await database_sync_to_async(
            _parse_and_enrich, thread_sensitive=False
        )()
        return result

    async def _get_content(self, url: str) -> "tuple[str, Union[str, bytes]]":
        """Download ``url``; return ``(content_type, body)``."""
        if self._session is None:
            raise RuntimeError(
                "PaRequirementsFetcher must be used as 'async with PaRequirementsFetcher()'"
            )
        headers = {
            "User-Agent": "fighthealthinsurance-pa-ingest/1.0",
            "Accept": (
                "text/html,application/xhtml+xml,application/pdf,"
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"
            ),
        }
        async with self._session.get(
            url, headers=headers, allow_redirects=True
        ) as resp:
            resp.raise_for_status()
            content_type = resp.content_type or ""
            data = await resp.content.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                raise ValueError(
                    f"PA requirement doc at {url} exceeded {self._max_bytes} bytes"
                )

            if content_type.startswith("text/"):
                encoding = resp.charset or "utf-8"
                return content_type, data.decode(encoding, errors="replace")
            return content_type, data

    @classmethod
    def _persist_company(
        cls,
        company: InsuranceCompany,
        parsed: List[ParsedPARequirement],
        source_name: str,
    ) -> "tuple[int, int]":
        """Upsert + soft-retire for one company in a single transaction.

        Wrapping both phases keeps the whole carrier refresh atomic for
        ``lookup_pa_requirements`` readers — they'll see either the old
        state or the new state, never a half-rewritten one. It also
        coalesces ``update_or_create``'s per-row autocommits into one
        transaction, materially cutting commit overhead on the hundreds-
        to-thousands-of-rows lists carriers publish.
        """
        with transaction.atomic():
            seen_pks, written = cls._upsert_rows(company, parsed, source_name)
            retired = cls._retire_missing(company, seen_pks)
        return written, retired

    @staticmethod
    def _upsert_rows(
        company: InsuranceCompany,
        parsed: List[ParsedPARequirement],
        source_name: str,
    ) -> "tuple[List[int], int]":
        """Upsert parsed rows; return ``(pks_seen_this_run, written)``.

        Keys each row on the same tuple ``lookup_pa_requirements`` queries
        on (see ``PayerPriorAuthRequirement.UPSERT_KEY_FIELDS``) so re-running
        an ingest produces no duplicates. A re-fetched row that was
        previously soft-retired has its ``end_date`` cleared so it becomes
        current again.

        Manual-row protection: if a row with the same scoping key already
        exists and is NOT auto-ingested (no ``"auto:"`` prefix on
        ``source_document``), we leave it alone — admin / fixture-seeded
        rows are authoritative and an auto-ingest must never overwrite
        them.
        """
        valid_lobs = {choice[0] for choice in LineOfBusiness.choices}
        seen: List[int] = []

        for req in parsed:
            code = (req.cpt_hcpcs_code or "").upper()
            range_start = (req.code_range_start or "").upper()
            range_end = (req.code_range_end or "").upper()
            if not code and not (range_start and range_end):
                continue

            lob = req.line_of_business or LineOfBusiness.ALL
            if lob not in valid_lobs:
                lob = LineOfBusiness.ALL

            state = (req.state or "")[:2].upper()

            # Skip if a manually-curated row already owns this key.
            manual_row_exists = (
                PayerPriorAuthRequirement.objects.filter(
                    insurance_company=company,
                    line_of_business=lob,
                    state=state,
                    cpt_hcpcs_code=code,
                    code_range_start=range_start,
                    code_range_end=range_end,
                )
                .exclude(source_document__startswith=AUTO_SOURCE_PREFIX)
                .exists()
            )
            if manual_row_exists:
                continue

            defaults: Dict[str, Any] = {
                "code_description": req.code_description[:500],
                "requires_pa": req.requires_pa,
                "notification_only": req.notification_only,
                "pa_category": req.pa_category[:200],
                "criteria_reference": req.criteria_reference[:500],
                "criteria_url": req.criteria_url[:200],
                "submission_channel": req.submission_channel[:500],
                "notes": req.notes[:1000],
                "source_document": source_name[:300],
                # Clear any prior end_date — this row is current again.
                "end_date": None,
            }
            obj, _ = PayerPriorAuthRequirement.objects.update_or_create(
                insurance_company=company,
                line_of_business=lob,
                state=state,
                cpt_hcpcs_code=code,
                code_range_start=range_start,
                code_range_end=range_end,
                defaults=defaults,
            )
            seen.append(obj.pk)
        return seen, len(seen)

    @staticmethod
    def _retire_missing(company: InsuranceCompany, seen_pks: List[int]) -> int:
        """Soft-retire ``auto:``-owned rows absent from this run.

        Operates only on rows whose ``source_document`` starts with
        ``"auto:"`` so manually-curated and fixture-loaded rows are never
        touched. Setting ``end_date=today`` rather than deleting preserves
        historical lookups: a denial with a past ``denial_date`` can still
        resolve to the rule that applied at the time.
        """
        today = datetime.date.today()
        qs = (
            PayerPriorAuthRequirement.objects.filter(insurance_company=company)
            .filter(source_document__startswith=AUTO_SOURCE_PREFIX)
            .exclude(pk__in=seen_pks)
            .filter(end_date__isnull=True)
        )
        retired: int = qs.update(end_date=today)
        return retired
