"""
Fetch and index payer medical-policy pages.

Iterates ``InsuranceCompany`` rows with
``medical_policy_url_is_static_index=True``, downloads each company's index
page over HTTP, dispatches it to the parser registered for the URL's host
in :mod:`fighthealthinsurance.payer_policy_parsers`, and replaces that
company's :class:`PayerPolicyEntry` rows with the parsed entries.

This is invoked by the ``ingest_payer_policy_indexes`` management command,
which can run at deploy time alongside the existing extralink prefetch
actor.
"""

from typing import Optional
from urllib.parse import urlparse

import aiohttp
from asgiref.sync import sync_to_async
from django.db import transaction
from loguru import logger

from fighthealthinsurance.models import InsuranceCompany, PayerPolicyEntry
from fighthealthinsurance.payer_policy_parsers import (
    PARSERS_BY_HOST,
    ParsedPolicyEntry,
)

# Conservative defaults. These pages are 100KB - a few MB; we don't want a
# slow payer site to wedge a deploy job.
FETCH_TIMEOUT_SEC = 60
MAX_BYTES = 10 * 1024 * 1024


class PayerPolicyFetcher:
    """Fetch payer index pages and persist parsed policy entries."""

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

    async def __aenter__(self) -> "PayerPolicyFetcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def ingest_all(self) -> dict[str, int]:
        """Fetch + parse + persist for every company flagged as a static index.

        Returns a small ``{fetched, failed, entries}`` summary.
        """
        stats = {"fetched": 0, "failed": 0, "entries": 0}
        companies = await sync_to_async(
            lambda: list(
                InsuranceCompany.objects.filter(
                    medical_policy_url_is_static_index=True,
                ).exclude(medical_policy_url="")
            )
        )()

        if not companies:
            logger.info("No insurance companies flagged for policy ingestion")
            return stats

        for company in companies:
            try:
                count = await self.ingest_company(company)
                stats["fetched"] += 1
                stats["entries"] += count
            except Exception:
                logger.opt(exception=True).warning(
                    f"Failed ingest for {company.name} ({company.medical_policy_url})"
                )
                stats["failed"] += 1
        logger.info(
            f"Policy ingest complete: {stats['fetched']} payers fetched, "
            f"{stats['failed']} failed, {stats['entries']} entries written"
        )
        return stats

    async def ingest_company(self, company: InsuranceCompany) -> int:
        """Fetch ``company``'s index, parse, and replace its PayerPolicyEntry rows.

        Returns the number of entries written. Raises ``LookupError`` if the
        URL host has no registered parser -- ``ingest_all`` treats this as a
        failed ingest so unsupported hosts surface in the run summary
        instead of being silently counted as fetched.
        """
        url = company.medical_policy_url
        if not url:
            return 0
        host = (urlparse(url).hostname or "").lower()
        parser = PARSERS_BY_HOST.get(host)
        if parser is None:
            raise LookupError(
                f"No parser registered for host {host!r} "
                f"(company {company.name}, url {url})"
            )

        body = await self._get_text(url)
        entries = parser(body)
        await self._replace_entries(company, entries)
        logger.info(f"Ingested {len(entries)} entries for {company.name}")
        return len(entries)

    async def _get_text(self, url: str) -> str:
        assert self._session is not None, "use 'async with PayerPolicyFetcher()'"
        async with self._session.get(
            url,
            headers={"User-Agent": "fighthealthinsurance-policy-ingest/1.0"},
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            data = await resp.content.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                raise ValueError(
                    f"Policy index at {url} exceeded {self._max_bytes} bytes"
                )
            # Most payer index pages serve text/html; respect the response's
            # encoding when set, otherwise let aiohttp pick a default.
            encoding = resp.charset or "utf-8"
            return data.decode(encoding, errors="replace")

    @staticmethod
    @sync_to_async
    def _replace_entries(
        company: InsuranceCompany,
        entries: list[ParsedPolicyEntry],
    ) -> None:
        # Replace atomically so consumers never see a partial index. The
        # delete-then-bulk_create pair within a single transaction also
        # makes ingest re-runs idempotent for both fresh and existing rows.
        with transaction.atomic():
            PayerPolicyEntry.objects.filter(insurance_company=company).delete()
            PayerPolicyEntry.objects.bulk_create(
                [
                    PayerPolicyEntry(
                        insurance_company=company,
                        title=e.title[:500],
                        url=e.url[:2000],
                        payer_policy_id=e.payer_policy_id[:100],
                    )
                    for e in entries
                ]
            )
