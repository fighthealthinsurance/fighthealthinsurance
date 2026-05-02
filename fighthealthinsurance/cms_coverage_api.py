"""Client for the CMS Medicare Coverage Database (MCD) Coverage API.

Fetches National Coverage Determinations (NCDs) and Local Coverage
Determinations (LCDs) so we can cite Medicare coverage policy in appeal
letters. Medicare rules are not automatically binding on commercial
plans, but they are strong evidence for medical necessity and
non-experimental status, and Medicare Advantage plans are generally
required to follow them.

The Coverage API is public (no auth required) as of 2024-02-08.
Docs: https://api.coverage.cms.gov/docs/swagger/index.html
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable

# CMS publishes NCD documents on the Medicare Coverage Database. The API
# returns a relative `url` like "/data/ncd?ncdid=108&ncdver=1"; prepending
# this host yields the human-readable page.
CMS_MCD_WEB_BASE = "https://www.cms.gov/medicare-coverage-database"


@dataclass
class NCDDocument:
    """A single National Coverage Determination summary record."""

    document_id: int
    document_display_id: str
    title: str
    last_updated: Optional[str] = None
    url_path: Optional[str] = None
    document_version: Optional[int] = None
    chapter: Optional[str] = None

    @property
    def public_url(self) -> str:
        """Human-readable URL for the NCD document."""
        if not self.url_path:
            return ""
        path = self.url_path if self.url_path.startswith("/") else f"/{self.url_path}"
        return f"{CMS_MCD_WEB_BASE}{path}"

    def as_citation(self, medicare_plan: bool = False) -> str:
        """Format as a single citation string suitable for an appeal letter."""
        binding = (
            "Medicare coverage policy that this plan must follow"
            if medicare_plan
            else "Medicare coverage policy"
        )
        url = self.public_url
        url_part = f" ({url})" if url else ""
        return (
            f"CMS National Coverage Determination {self.document_display_id} — "
            f'"{self.title}"{url_part}: {binding} recognizing this service as '
            "covered when the Medicare medical-necessity criteria are met."
        )


@dataclass
class LCDDocument:
    """A single Local Coverage Determination summary record."""

    document_id: int
    document_display_id: str
    title: str
    last_updated: Optional[str] = None
    url_path: Optional[str] = None
    contractor: Optional[str] = None
    state: Optional[str] = None
    jurisdictions: List[str] = field(default_factory=list)

    @property
    def public_url(self) -> str:
        if not self.url_path:
            return ""
        path = self.url_path if self.url_path.startswith("/") else f"/{self.url_path}"
        return f"{CMS_MCD_WEB_BASE}{path}"

    def as_citation(self, medicare_plan: bool = False) -> str:
        binding = (
            "Local Medicare coverage policy that this plan must follow"
            if medicare_plan
            else "Local Medicare coverage policy"
        )
        url = self.public_url
        url_part = f" ({url})" if url else ""
        scope = f" ({self.contractor})" if self.contractor else ""
        return (
            f"CMS Local Coverage Determination {self.document_display_id}{scope} — "
            f'"{self.title}"{url_part}: {binding} recognizing this service '
            "as reasonable and necessary under Medicare criteria."
        )


def _extract_records(payload: dict) -> List[dict]:
    """Pull the data array out of a Coverage API JSON response."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _parse_ncd_record(record: dict) -> Optional[NCDDocument]:
    try:
        document_id = record.get("document_id")
        title = record.get("title")
        if document_id is None or not title:
            return None
        return NCDDocument(
            document_id=int(document_id),
            document_display_id=str(record.get("document_display_id", "")),
            title=str(title),
            last_updated=record.get("last_updated"),
            url_path=record.get("url"),
            document_version=record.get("document_version"),
            chapter=record.get("chapter"),
        )
    except (TypeError, ValueError) as e:
        logger.debug(f"Skipping malformed NCD record: {e}")
        return None


def _parse_lcd_record(record: dict) -> Optional[LCDDocument]:
    try:
        document_id = record.get("document_id")
        title = record.get("title")
        if document_id is None or not title:
            return None
        jurisdictions_raw = record.get("jurisdictions") or []
        if isinstance(jurisdictions_raw, str):
            jurisdictions = [jurisdictions_raw]
        else:
            jurisdictions = [str(j) for j in jurisdictions_raw]
        return LCDDocument(
            document_id=int(document_id),
            document_display_id=str(record.get("document_display_id", "")),
            title=str(title),
            last_updated=record.get("last_updated"),
            url_path=record.get("url"),
            contractor=record.get("contractor"),
            state=record.get("state"),
            jurisdictions=jurisdictions,
        )
    except (TypeError, ValueError) as e:
        logger.debug(f"Skipping malformed LCD record: {e}")
        return None


def _filter_by_keyword(
    records: List[NCDDocument], keyword: str, max_results: int
) -> List[NCDDocument]:
    """Keyword-filter NCD titles client-side.

    The MCD reports endpoints don't reliably accept a keyword filter, so we
    pull the report and filter title matches locally. The full report is
    small enough (~hundreds of NCDs) that this is cheap.
    """
    if not keyword:
        return records[:max_results]
    needle = keyword.strip().lower()
    if not needle:
        return records[:max_results]
    matches: List[NCDDocument] = []
    for r in records:
        if needle in r.title.lower():
            matches.append(r)
            if len(matches) >= max_results:
                break
    return matches


class CMSCoverageClient:
    """Async client for the CMS Medicare Coverage Database Coverage API."""

    _HEALTH_CACHE_TTL = 300.0  # 5 min on success
    _HEALTH_CACHE_TTL_FAILURE = 30.0  # 30s on failure

    # The full NCD report is small and stable; keep it in-process for the
    # life of the client to avoid refetching for every denial.
    _NCD_REPORT_TTL = 3600.0

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url or get_env_variable(
            "CMS_COVERAGE_API_URL", "https://api.coverage.cms.gov"
        )
        self.timeout = httpx.Timeout(timeout, connect=5.0)
        self._client: Optional[httpx.AsyncClient] = None
        self._health_ok: Optional[bool] = None
        self._health_checked_at: float = 0.0
        self._ncd_report_cache: Optional[List[NCDDocument]] = None
        self._ncd_report_cached_at: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        now = time.monotonic()
        if self._health_ok is not None:
            ttl = (
                self._HEALTH_CACHE_TTL
                if self._health_ok
                else self._HEALTH_CACHE_TTL_FAILURE
            )
            if (now - self._health_checked_at) < ttl:
                return self._health_ok
        try:
            client = await self._get_client()
            # The reports endpoint always returns a JSON envelope with meta;
            # we don't depend on a dedicated /health/ route the API doesn't
            # advertise.
            response = await client.get(
                "/v1/reports/national-coverage-ncd", params={"page_size": 1}
            )
            self._health_ok = response.status_code == 200
        except Exception as e:
            logger.debug(f"CMS Coverage API health check failed: {e}")
            self._health_ok = False
        self._health_checked_at = now
        return self._health_ok

    async def _fetch_ncd_report(self) -> List[NCDDocument]:
        """Fetch (and cache) the full NCD report."""
        now = time.monotonic()
        if (
            self._ncd_report_cache is not None
            and (now - self._ncd_report_cached_at) < self._NCD_REPORT_TTL
        ):
            return self._ncd_report_cache
        try:
            client = await self._get_client()
            response = await client.get("/v1/reports/national-coverage-ncd")
            if response.status_code != 200:
                logger.warning(f"CMS NCD report returned status {response.status_code}")
                return self._ncd_report_cache or []
            payload = response.json()
            parsed = [
                doc
                for doc in (_parse_ncd_record(r) for r in _extract_records(payload))
                if doc is not None
            ]
            self._ncd_report_cache = parsed
            self._ncd_report_cached_at = now
            return parsed
        except httpx.TimeoutException:
            logger.warning("CMS Coverage API request timed out")
            return self._ncd_report_cache or []
        except httpx.RequestError as e:
            logger.warning(f"CMS Coverage API request failed: {e}")
            return self._ncd_report_cache or []
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Unexpected error fetching CMS NCD report: {e}"
            )
            return self._ncd_report_cache or []

    async def search_ncds(
        self, keyword: str, max_results: int = 5
    ) -> List[NCDDocument]:
        """Search NCDs whose title contains the given keyword.

        Returns up to `max_results` documents ordered as the API returned
        them. Empty list if the API is unavailable.
        """
        if not keyword or not keyword.strip():
            return []
        records = await self._fetch_ncd_report()
        return _filter_by_keyword(records, keyword, max_results)

    async def search_lcds(
        self, keyword: str, state: Optional[str] = None, max_results: int = 5
    ) -> List[LCDDocument]:
        """Search final LCDs whose title contains the given keyword.

        Optionally restricts to a state (two-letter code).
        """
        if not keyword or not keyword.strip():
            return []
        try:
            client = await self._get_client()
            params: dict = {}
            if state:
                params["state"] = state.upper()
            response = await client.get(
                "/v1/reports/local-coverage-final-lcds", params=params
            )
            if response.status_code != 200:
                logger.debug(f"CMS LCD report returned status {response.status_code}")
                return []
            payload = response.json()
            records = [
                doc
                for doc in (_parse_lcd_record(r) for r in _extract_records(payload))
                if doc is not None
            ]
            needle = keyword.strip().lower()
            matches = [r for r in records if needle in r.title.lower()]
            return matches[:max_results]
        except httpx.TimeoutException:
            logger.warning("CMS LCD request timed out")
            return []
        except httpx.RequestError as e:
            logger.warning(f"CMS LCD request failed: {e}")
            return []
        except Exception as e:
            logger.opt(exception=True).warning(f"Unexpected error fetching LCDs: {e}")
            return []


_cms_client: Optional[CMSCoverageClient] = None


def get_cms_coverage_client() -> CMSCoverageClient:
    """Return the process-global CMS Coverage client.

    A brief race creating two instances is harmless; the constructor is
    side-effect-free and the last one wins.
    """
    global _cms_client
    if _cms_client is None:
        _cms_client = CMSCoverageClient()
    return _cms_client


def _candidate_keywords(
    procedure: Optional[str], diagnosis: Optional[str]
) -> List[str]:
    """Build the ordered keyword list used to query the CMS API."""
    keywords: List[str] = []
    seen: set = set()
    for raw in (procedure, diagnosis):
        if not raw:
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(cleaned)
    return keywords


async def get_cms_coverage_citations(
    procedure: Optional[str],
    diagnosis: Optional[str],
    medicare_plan: bool = False,
    max_results: int = 3,
) -> List[str]:
    """Return formatted CMS coverage citations for a procedure/diagnosis.

    `medicare_plan=True` triggers stronger framing emphasizing that
    Medicare-administered plans are bound by these policies.
    """
    keywords = _candidate_keywords(procedure, diagnosis)
    if not keywords:
        return []
    client = get_cms_coverage_client()
    if not await client.health_check():
        logger.info("CMS Coverage API not reachable, skipping enrichment")
        return []
    citations: List[str] = []
    seen_ids: set = set()
    for keyword in keywords:
        if len(citations) >= max_results:
            break
        ncds = await client.search_ncds(keyword=keyword, max_results=max_results)
        for ncd in ncds:
            if ncd.document_id in seen_ids:
                continue
            seen_ids.add(ncd.document_id)
            citations.append(ncd.as_citation(medicare_plan=medicare_plan))
            if len(citations) >= max_results:
                break
    return citations
