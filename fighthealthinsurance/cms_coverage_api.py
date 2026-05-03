"""Client for the CMS Medicare Coverage Database (MCD) Coverage API.

Fetches National Coverage Determinations (NCDs) so we can point at relevant
Medicare coverage policy in appeal letters. Medicare rules are not
automatically binding on commercial plans, but they are strong evidence for
medical-necessity standards, and Medicare/Medicare Advantage plans are
generally required to follow them.

We deliberately do not assert what each NCD says — NCDs come in coverage,
non-coverage, and limited-coverage varieties, and we don't fetch the
determination text. Citations are framed as neutral pointers ("see this
NCD") so the appeal-generation LLM can read the URL without us inserting
a factually wrong claim.

The Coverage API is public (no auth required) as of 2024-02-08.
Docs: https://api.coverage.cms.gov/docs/swagger/index.html
"""

import re
import time
from dataclasses import dataclass
from typing import List, Optional

import httpx
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable

# CMS publishes NCD documents on the Medicare Coverage Database. The API
# returns a relative `url` like "/data/ncd?ncdid=108&ncdver=1"; prepending
# this host yields the human-readable page.
CMS_MCD_WEB_BASE = "https://www.cms.gov/medicare-coverage-database"

# Generic English words that show up in many medical-procedure phrases but
# carry no signal for matching against NCD titles.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def _tokenize(text: str) -> List[str]:
    """Split text into lowercase alphanumeric tokens, dropping stopwords."""
    return [
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _STOPWORDS and len(t) >= 2
    ]


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
        """Format as a neutral pointer to the NCD document.

        We don't assert what the NCD says (some NCDs are non-coverage or
        narrowly limited determinations). The citation is a reference: the
        appeal-generation LLM can use the linked policy as authority for
        Medicare's medical-necessity criteria, but we don't preemptively
        characterize it as favorable.
        """
        url = self.public_url
        url_part = f" ({url})" if url else ""
        if medicare_plan:
            tail = (
                "; Medicare-administered plans are bound by this NCD's "
                "coverage standard."
            )
        else:
            tail = (
                "; Medicare's coverage standard is widely treated as "
                "evidence of medical necessity."
            )
        return (
            f"CMS National Coverage Determination {self.document_display_id} — "
            f'"{self.title}"{url_part}: see this Medicare coverage policy for '
            f"the applicable coverage criteria{tail}"
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


def _filter_by_keyword(
    records: List[NCDDocument], keyword: str, max_results: int
) -> List[NCDDocument]:
    """Token-based keyword filter on NCD titles.

    Tokenizes both the input keyword and each NCD title (dropping stopwords
    and short tokens), then returns titles ranked by how many input tokens
    they share. This lets multi-word inputs like "MRI brain" match titles
    like "MRI of the Brain" or "Magnetic Resonance Imaging — Brain".

    The MCD reports endpoints don't reliably accept a keyword filter, so we
    pull the report and filter locally. The full report is small enough
    (~hundreds of NCDs) that this is cheap.
    """
    tokens = _tokenize(keyword) if keyword else []
    if not tokens:
        return []
    scored: List[tuple] = []
    for r in records:
        title_tokens = set(_tokenize(r.title))
        if not title_tokens:
            continue
        match_count = sum(1 for t in tokens if t in title_tokens)
        if match_count == 0:
            continue
        # Sort key: more matches first, then shorter title (more specific).
        scored.append((-match_count, len(r.title), r))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [r for _, _, r in scored[:max_results]]


class CMSCoverageClient:
    """Async client for the CMS Medicare Coverage Database Coverage API."""

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

    async def _fetch_ncd_report(self) -> List[NCDDocument]:
        """Fetch (and cache) the full NCD report.

        On any error, returns the previously-cached report if we have one,
        otherwise an empty list. This means transient CMS outages don't
        zero out our citations as long as the in-process cache still has
        data from a prior successful fetch.
        """
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
        """Search NCDs whose title shares meaningful tokens with the keyword.

        Empty list if the API is unavailable and we have no cached report.
        """
        if not keyword or not keyword.strip():
            return []
        records = await self._fetch_ncd_report()
        return _filter_by_keyword(records, keyword, max_results)


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

    `medicare_plan=True` triggers framing emphasizing that
    Medicare-administered plans are bound by these policies.

    Returns an empty list if no usable keywords are present or if the CMS
    API is unreachable AND we have no cached report. Transient outages
    where the in-process report cache is still warm continue to return
    citations from the cached data.
    """
    keywords = _candidate_keywords(procedure, diagnosis)
    if not keywords:
        return []
    client = get_cms_coverage_client()
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
