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
from typing import Iterable, List, Optional, Tuple

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


# Words that name a kind of medical work rather than what it is done to or
# for. They appear in many unrelated NCD titles ("Hair Analysis", "Applied
# Behavior Analysis"; "Knee Replacement Surgery", "valve surgery";
# "Speech Generating Devices", "speech therapy"), so a
# title sharing only one of them with the query is not a match. They are
# dropped from both sides before matching.
_GENERIC_MEDICAL_WORDS = frozenset(
    {
        "analysis",
        "care",
        "disorder",
        "disorders",
        "evaluation",
        "management",
        "procedure",
        "procedures",
        "service",
        "services",
        "surgeries",
        "surgery",
        "surgical",
        "test",
        "testing",
        "tests",
        "therapies",
        "therapy",
        "treatment",
        "treatments",
    }
)


def _tokenize(text: str) -> List[str]:
    """Split text into lowercase alphanumeric tokens, dropping stopwords."""
    return [
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _STOPWORDS and len(t) >= 2
    ]


# Singular words that end in "s" and must not lose it.
_KEEP_TRAILING_S = frozenset(
    {
        "diabetes",
        "herpes",
        "measles",
        "mumps",
        "rabies",
        "rickets",
        "scabies",
        "series",
        "species",
    }
)


# Words the suffix rules below would get wrong, mapped to their singular.
_IRREGULAR_SINGULARS = {
    "analyses": "analysis",
    "axes": "axis",
    "diagnoses": "diagnosis",
    "lens": "lens",
    "lenses": "lens",
    "prostheses": "prosthesis",
    "viruses": "virus",
}


def _singular(token: str) -> str:
    """A light, conservative singular form, so "replacements" and
    "Replacement" count as the same word. Only plain plural endings are
    touched: words ending in -is, -us, -ss, -as or -os ("analysis",
    "lupus", "bypass", "pancreas") and the listed singulars ("diabetes")
    stay as they are, and a few irregular words ("lenses") are mapped."""
    if token in _IRREGULAR_SINGULARS:
        return _IRREGULAR_SINGULARS[token]
    if len(token) <= 3 or token in _KEEP_TRAILING_S:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("is", "us", "ss", "as", "os")):
        return token
    if token.endswith(("sses", "xes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


_GENERIC_MEDICAL_STEMS = frozenset(_singular(w) for w in _GENERIC_MEDICAL_WORDS)


def _meaningful_tokens(text: str) -> List[str]:
    """``_tokenize`` in singular form without the generic medical words,
    deduplicated in order."""
    out: List[str] = []
    for t in _tokenize(text):
        t = _singular(t)
        if t not in _GENERIC_MEDICAL_STEMS and t not in out:
            out.append(t)
    return out


# The phrase every NCD citation carries since titles had to match the query
# closely. A cached citation without it was written by the older matcher,
# which accepted one shared word, so it may be off topic and is looked up
# again (ml_citations_helper).
NCD_CITATION_MARKER = "may apply to this service"


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
        characterize it as favorable. The match is on title words, so the
        citation says the policy may apply and leaves the model to use it
        only when it fits the case.
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
            f'"{self.title}"{url_part}: a Medicare coverage policy that '
            f"{NCD_CITATION_MARKER}; cite it only if it fits this case, for "
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


def _title_matches(query_tokens: List[str], title: str):
    """Score an NCD title against the query's meaningful tokens, or None.

    A title matches when the query names its subject closely, in one of two
    ways:

    * Most of the query is in the title: more than half of the query's
      meaningful tokens, and at least two.
    * Most of the title is in the query: at least two shared tokens that
      are more than half the title's, so a long procedure description
      still finds a short title ("Cardiac Rehabilitation Programs" for
      cardiac rehabilitation after valve surgery).
    * All of the title is in the query: every meaningful title token
      appears in it ("Ambulance Services" for "air ambulance").

    A one-word query can only match the last way, a title that is that one
    word. One shared word is not enough otherwise, which is what let "air
    ambulance" match "Air-Fluidized Bed" and "applied behavior analysis"
    match "Hair Analysis". Returns a sort key (best first) or None.

    The ECRI guideline lookup applies the same rule to its keyword phrases
    (ecri_guidelines_helper), so both sources agree on what a match is.
    """
    title_tokens = set(_meaningful_tokens(title))
    if not title_tokens:
        return None
    matched = sum(1 for t in query_tokens if t in title_tokens)
    if matched == 0:
        return None
    most_of_query = matched >= 2 and matched * 2 > len(query_tokens)
    most_of_title = matched >= 2 and matched * 2 > len(title_tokens)
    all_of_title = title_tokens.issubset(query_tokens)
    if not (most_of_query or most_of_title or all_of_title):
        return None
    query_share = matched / len(query_tokens)
    title_share = matched / len(title_tokens)
    # Best first: more of the query covered, then more of the title, then
    # more shared words, then the shorter title.
    return (-query_share, -title_share, -matched, len(title))


def _filter_by_keyword(
    records: List[NCDDocument], keyword: str, max_results: int
) -> List[NCDDocument]:
    """Token-based keyword filter on NCD titles.

    Tokenizes both the input keyword and each NCD title (dropping stopwords,
    short tokens and generic medical words), then keeps only titles that
    match the query closely (see ``_title_matches``) and ranks them by how
    much of the query they cover. This lets multi-word inputs like "MRI
    brain" match titles like "MRI of the Brain" without a single shared
    word pulling in an unrelated policy. Nothing matching means no NCD: an
    off-topic citation is worse than none.

    The MCD reports endpoints don't reliably accept a keyword filter, so we
    pull the report and filter locally. The full report is small enough
    (~hundreds of NCDs) that this is cheap.
    """
    if not keyword:
        return []
    query_tokens = _meaningful_tokens(keyword)
    if not query_tokens:
        return []
    scored: List[tuple] = []
    for r in records:
        key = _title_matches(query_tokens, r.title)
        if key is not None:
            scored.append((key, r))
    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored[:max_results]]


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
        # Whether every record in the cached report parsed. A report that
        # lost some is still searched, but an empty search of it can't say
        # nothing applies: the dropped record might have been the match.
        self._ncd_report_complete: bool = False

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

    async def load_report(self) -> Tuple[List[NCDDocument], bool]:
        """The NCD report, and whether it is a fresh, complete one.

        Serves the in-process copy while it is within its TTL; otherwise
        fetches it. On any error, or a response that isn't a report, returns
        the previously-cached report if we have one (otherwise an empty
        list) with False, so transient CMS outages don't zero out our
        citations but nobody mistakes the result for a finished search. A
        data list whose every record fails to parse isn't a report either,
        and one that lost only some records is kept and searched but
        reported as incomplete, on later cache hits too.
        """
        now = time.monotonic()
        if (
            self._ncd_report_cache is not None
            and (now - self._ncd_report_cached_at) < self._NCD_REPORT_TTL
        ):
            return self._ncd_report_cache, self._ncd_report_complete
        fallback = self._ncd_report_cache or []
        try:
            client = await self._get_client()
            response = await client.get("/v1/reports/national-coverage-ncd")
            if response.status_code != 200:
                logger.warning(f"CMS NCD report returned status {response.status_code}")
                return fallback, False
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                # Not a report: don't let it stand in for a finished search.
                logger.warning("CMS NCD report response had no data list")
                return fallback, False
            records = _extract_records(payload)
            parsed = [
                doc
                for doc in (_parse_ncd_record(r) for r in records)
                if doc is not None
            ]
            if records and not parsed:
                logger.warning("CMS NCD report had no record that could be parsed")
                return fallback, False
            complete = len(parsed) == len(records)
            if not complete:
                logger.warning(
                    f"CMS NCD report: {len(records) - len(parsed)} record(s) "
                    f"could not be parsed"
                )
            self._ncd_report_cache = parsed
            self._ncd_report_cached_at = now
            self._ncd_report_complete = complete
            return parsed, complete
        except httpx.TimeoutException:
            logger.warning("CMS Coverage API request timed out")
            return fallback, False
        except httpx.RequestError as e:
            logger.warning(f"CMS Coverage API request failed: {e}")
            return fallback, False
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Unexpected error fetching CMS NCD report: {e}"
            )
            return fallback, False

    async def _fetch_ncd_report(self) -> List[NCDDocument]:
        """The NCD report, fresh or the last good one (see ``load_report``)."""
        records, _valid = await self.load_report()
        return records

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


class CMSCoverageCitations(List[str]):
    """The citations one lookup found, a plain list to its callers, plus
    whether the lookup was complete: every keyword searched against one
    fresh, valid NCD report. Only a complete empty lookup means no NCD
    applies; an incomplete one may just be an outage."""

    def __init__(self, citations: Iterable[str] = (), complete: bool = False):
        super().__init__(citations)
        self.complete = complete


async def get_cms_coverage_citations(
    procedure: Optional[str],
    diagnosis: Optional[str],
    medicare_plan: bool = False,
    max_results: int = 3,
) -> CMSCoverageCitations:
    """Return formatted CMS coverage citations for a procedure/diagnosis.

    `medicare_plan=True` triggers framing emphasizing that
    Medicare-administered plans are bound by these policies.

    The report is loaded once and both keywords are searched against that
    same copy, so the result's ``complete`` flag says whether all of it
    came from a fresh, valid report. Returns an empty list if no usable
    keywords are present or if the CMS API is unreachable AND we have no
    cached report. Transient outages where the in-process report cache is
    still warm continue to return citations from the cached data, marked
    incomplete.
    """
    keywords = _candidate_keywords(procedure, diagnosis)
    if not keywords:
        return CMSCoverageCitations()
    client = get_cms_coverage_client()
    records, report_valid = await client.load_report()
    citations: List[str] = []
    seen_ids: set = set()
    for keyword in keywords:
        if len(citations) >= max_results:
            break
        for ncd in _filter_by_keyword(records, keyword, max_results):
            if ncd.document_id in seen_ids:
                continue
            seen_ids.add(ncd.document_id)
            citations.append(ncd.as_citation(medicare_plan=medicare_plan))
            if len(citations) >= max_results:
                break
    return CMSCoverageCitations(citations, complete=report_valid)
