"""
Fetch and index payer prior-authorization requirement lists.

Iterates ``InsuranceCompany`` rows with
``pa_requirement_list_url_is_parseable=True``, downloads each company's PA
requirement document, picks a parser by Content-Type, applies any per-host
payer enrichment (submission channel / default LOB), and replaces that
company's :class:`PayerPriorAuthRequirement` rows marked ``"auto:"`` with
the freshly parsed entries.

Manually seeded rows (no ``"auto:"`` prefix on ``source_document``) are
left untouched so handcrafted seed data is never clobbered.

Invoked by the ``ingest_pa_requirements`` management command at deploy time
and by ``PARequirementRefreshActor`` daily at 1 AM Pacific time.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union
from urllib.parse import urlparse

import aiohttp
from asgiref.sync import sync_to_async
from django.db import transaction
from loguru import logger

from fighthealthinsurance.models import InsuranceCompany, PayerPriorAuthRequirement
from fighthealthinsurance.pa_requirement_parsers import (
    PARSERS_BY_CONTENT_TYPE,
    ParsedPARequirement,
    apply_enrichment,
    enrichment_for_host,
)

FETCH_TIMEOUT_SEC = 120
MAX_BYTES = 50 * 1024 * 1024  # 50 MB — PA lists can be large Excel/PDF files
BULK_CREATE_BATCH = 500

# Rows whose source_document starts with this prefix are managed by the
# auto-ingest pipeline; ``_replace_requirements`` only ever deletes those.
AUTO_SOURCE_PREFIX = "auto:"


class PARequirementFetcher:
    """Fetch payer PA requirement documents and persist parsed requirements."""

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

    async def __aenter__(self) -> "PARequirementFetcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def ingest_all(self) -> Dict[str, int]:
        """Fetch + parse + persist for every company flagged as parseable."""
        stats: Dict[str, int] = {"fetched": 0, "failed": 0, "entries": 0}
        companies = await sync_to_async(
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
            try:
                count = await self.ingest_company(company)
                stats["fetched"] += 1
                stats["entries"] += count
            except Exception:
                logger.opt(exception=True).warning(
                    f"Failed PA ingest for {company.name} ({company.pa_requirement_list_url})"
                )
                stats["failed"] += 1

        logger.info(
            f"PA requirement ingest complete: {stats['fetched']} payers fetched, "
            f"{stats['failed']} failed, {stats['entries']} requirements written"
        )
        return stats

    async def parse_company(
        self, company: InsuranceCompany
    ) -> List[ParsedPARequirement]:
        """Fetch + parse a company's PA list without writing to the DB.

        Used by ``ingest_company`` and the management command's ``--dry-run``
        mode so both share the same dispatch / enrichment pipeline.
        Raises ``LookupError`` when no parser matches the response Content-Type.
        """
        url = company.pa_requirement_list_url
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

        requirements = parser_fn(body, source_name)
        apply_enrichment(
            requirements, enrichment_for_host(urlparse(url).hostname or "")
        )
        return requirements

    async def ingest_company(self, company: InsuranceCompany) -> int:
        """Fetch, parse, and persist; returns count of rows written."""
        requirements = await self.parse_company(company)
        if not requirements:
            logger.info(f"No PA requirements parsed for {company.name}")
            return 0
        source_name = f"{AUTO_SOURCE_PREFIX}{company.pa_requirement_list_url}"
        count = await self._replace_requirements(company, requirements, source_name)
        logger.info(f"Ingested {count} PA requirements for {company.name}")
        return count

    async def _get_content(self, url: str) -> "tuple[str, Union[str, bytes]]":
        """Download ``url``; return ``(content_type, body)`` (str for text MIME)."""
        assert self._session is not None, "use 'async with PARequirementFetcher()'"
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

    @staticmethod
    @sync_to_async
    def _replace_requirements(
        company: InsuranceCompany,
        parsed: List[ParsedPARequirement],
        source_name: str,
    ) -> int:
        """Atomically replace this company's ``auto:`` rows.

        Manually seeded rows (without the ``auto:`` source prefix) are
        preserved so handcrafted entries survive re-ingestion.
        """
        from fighthealthinsurance.models import LineOfBusiness

        valid_lobs = {choice[0] for choice in LineOfBusiness.choices}

        with transaction.atomic():
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=company,
                source_document__startswith=AUTO_SOURCE_PREFIX,
            ).delete()

            to_create: List[PayerPriorAuthRequirement] = []
            for req in parsed:
                code = req.cpt_hcpcs_code.upper() if req.cpt_hcpcs_code else ""
                range_start = (
                    req.code_range_start.upper() if req.code_range_start else ""
                )
                range_end = req.code_range_end.upper() if req.code_range_end else ""
                if not code and not (range_start and range_end):
                    continue

                lob = req.line_of_business or LineOfBusiness.ALL
                if lob not in valid_lobs:
                    lob = LineOfBusiness.ALL

                to_create.append(
                    PayerPriorAuthRequirement(
                        insurance_company=company,
                        cpt_hcpcs_code=code,
                        code_range_start=range_start,
                        code_range_end=range_end,
                        code_description=req.code_description[:500],
                        requires_pa=req.requires_pa,
                        notification_only=req.notification_only,
                        pa_category=req.pa_category[:200],
                        criteria_reference=req.criteria_reference[:500],
                        submission_channel=req.submission_channel[:500],
                        line_of_business=lob,
                        state=req.state[:2].upper(),
                        notes=req.notes[:1000],
                        source_document=source_name[:300],
                    )
                )

            PayerPriorAuthRequirement.objects.bulk_create(
                to_create, batch_size=BULK_CREATE_BATCH
            )
            return len(to_create)
