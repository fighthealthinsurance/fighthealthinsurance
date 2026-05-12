"""
Fetch and index payer prior-authorization requirement lists.

Iterates ``InsuranceCompany`` rows with
``pa_requirement_list_url_is_parseable=True``, downloads each company's PA
requirement document over HTTP, dispatches it to the parser registered for the
URL's host in :mod:`fighthealthinsurance.pa_requirement_parsers`, and replaces
that company's :class:`PayerPriorAuthRequirement` rows that came from
auto-ingestion (``source_document`` prefix ``"auto:"``) with the freshly
parsed entries.

Rows loaded by the fixture seed or entered manually (no ``"auto:"`` prefix on
``source_document``) are left untouched so handcrafted seed data is never
clobbered.

This is invoked by the ``ingest_pa_requirements`` management command, which
can run at deploy time alongside the existing payer-policy ingest command.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from asgiref.sync import sync_to_async
from django.db import transaction
from loguru import logger

from fighthealthinsurance.models import InsuranceCompany, PayerPriorAuthRequirement
from fighthealthinsurance.pa_requirement_parsers import (
    PARSERS_BY_CONTENT_TYPE,
    PARSERS_BY_HOST,
    ParsedPARequirement,
)

FETCH_TIMEOUT_SEC = 120
MAX_BYTES = 50 * 1024 * 1024  # 50 MB — PA lists can be large Excel/PDF files

# Source-document prefix that marks auto-ingested rows so we can replace them
# on re-runs without touching manually seeded rows.
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
        """Fetch + parse + persist for every company flagged as parseable.

        Returns a ``{fetched, failed, entries}`` summary dict.
        """
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

    async def ingest_company(self, company: InsuranceCompany) -> int:
        """Fetch ``company``'s PA requirement document, parse, and replace auto rows.

        Returns the number of ``PayerPriorAuthRequirement`` rows written.
        Raises ``LookupError`` when no parser is registered for the URL.
        """
        url = company.pa_requirement_list_url
        if not url:
            return 0

        host = (urlparse(url).hostname or "").lower()
        parser_spec = PARSERS_BY_HOST.get(host)

        # Fetch content; content-type sniffing may override the parser
        content_type, raw = await self._get_content(url)

        # Host-registered parser takes priority; fall back to content-type
        if parser_spec is not None:
            parser_fn, is_binary = parser_spec
        else:
            ct_key = (content_type or "").split(";")[0].strip().lower()
            fallback = PARSERS_BY_CONTENT_TYPE.get(ct_key)
            if fallback is None:
                raise LookupError(
                    f"No PA parser for host {host!r} (content-type {ct_key!r}); "
                    f"company {company.name}, url {url}"
                )
            parser_fn = fallback
            is_binary = True  # content-type parsers all expect bytes

        source_name = f"{AUTO_SOURCE_PREFIX}{url}"

        if is_binary:
            assert isinstance(raw, bytes)
            requirements = parser_fn(raw, source_name)
        else:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            requirements = parser_fn(text, source_name)

        if not requirements:
            logger.info(f"No PA requirements parsed from {url}")
            return 0

        count = await self._replace_requirements(company, requirements, source_name)
        logger.info(f"Ingested {count} PA requirements for {company.name}")
        return count

    async def _get_content(self, url: str):
        """Download ``url`` and return ``(content_type, body)``.

        ``body`` is ``bytes`` for non-text MIME types and ``str`` for HTML/text.
        """
        assert self._session is not None, "use 'async with PARequirementFetcher()'"
        headers = {
            "User-Agent": "fighthealthinsurance-pa-ingest/1.0",
            "Accept": "text/html,application/xhtml+xml,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        }
        async with self._session.get(url, headers=headers, allow_redirects=True) as resp:
            resp.raise_for_status()
            content_type = resp.content_type or ""
            data = await resp.content.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                raise ValueError(f"PA requirement doc at {url} exceeded {self._max_bytes} bytes")

            # Return as str for text types, bytes for binary
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
        """
        Atomically replace auto-ingested rows for ``company`` with newly
        parsed entries. Manual/seeded rows (no ``AUTO_SOURCE_PREFIX``) are
        left untouched.

        Returns the count of rows written.
        """
        from fighthealthinsurance.models import LineOfBusiness

        with transaction.atomic():
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=company,
                source_document__startswith=AUTO_SOURCE_PREFIX,
            ).delete()

            to_create: List[PayerPriorAuthRequirement] = []
            for req in parsed:
                code = req.cpt_hcpcs_code or ""
                range_start = ""
                range_end = ""

                # Handle the RANGE: sentinel emitted by _rows_to_requirements
                if code.startswith("RANGE:"):
                    range_part = code[len("RANGE:"):]
                    parts = re.split(r"[-–]", range_part, maxsplit=1)
                    if len(parts) == 2:
                        range_start, range_end = parts[0].upper(), parts[1].upper()
                    code = ""

                lob = req.line_of_business or LineOfBusiness.ALL
                # Validate against known LOB choices; fall back to ALL
                valid_lobs = {choice[0] for choice in LineOfBusiness.choices}
                if lob not in valid_lobs:
                    lob = LineOfBusiness.ALL

                if not code and not (range_start and range_end):
                    continue  # Skip rows with no usable code

                to_create.append(
                    PayerPriorAuthRequirement(
                        insurance_company=company,
                        cpt_hcpcs_code=code,
                        code_range_start=range_start,
                        code_range_end=range_end,
                        code_description=req.code_description[:500] if req.code_description else "",
                        requires_pa=req.requires_pa,
                        notification_only=req.notification_only,
                        pa_category=req.pa_category[:200] if req.pa_category else "",
                        criteria_reference=req.criteria_reference[:500] if req.criteria_reference else "",
                        criteria_url="",
                        submission_channel=req.submission_channel[:500] if req.submission_channel else "",
                        line_of_business=lob,
                        state=(req.state or "")[:2].upper(),
                        notes=req.notes[:1000] if req.notes else "",
                        source_document=source_name[:300],
                    )
                )

            PayerPriorAuthRequirement.objects.bulk_create(to_create)
            return len(to_create)
