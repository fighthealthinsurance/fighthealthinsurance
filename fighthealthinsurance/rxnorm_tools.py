"""
RxNorm / RxNav API integration for medication name normalization.

RxNorm is a standardized vocabulary published by the U.S. National Library
of Medicine that links drug names across vocabularies (brand names, generic
ingredient names, dose forms, clinical drug forms, etc.). RxNav exposes a
free public REST API at https://rxnav.nlm.nih.gov/REST/.

This module wraps a small subset of the RxNav API and caches results in the
``RxNormConcept`` model. The goal is "data plumbing" rather than appeal
evidence: before searching PubMed, DailyMed, or insurer policy documents,
normalize whatever the user typed (e.g., "Glucophage", "metformen",
"METFORMIN HCL 500mg") into a canonical drug name plus a pool of related
synonyms (brand names, ingredients, etc.).

Public entry points:

* :py:meth:`RxNormTools.normalize` -- normalize one input string to a
  :py:class:`NormalizedDrug` (canonical name + RxCUI + score). Cached.
* :py:meth:`RxNormTools.expand_query_terms` -- given a free-text drug name,
  return an ordered, deduplicated list of search-friendly terms (canonical
  name first, then brand names and ingredients) suitable for feeding into
  PubMed / policy search queries.
* :py:meth:`RxNormTools.get_brands_and_generics` -- structured view of a
  drug: its canonical name, generic ingredients, and known brand names.
"""

import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from asgiref.sync import async_to_sync
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import RxNormConcept

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


# Base URL for the public RxNav REST API.
RXNAV_BASE_URL = "https://rxnav.nlm.nih.gov/REST"

# How long a cache entry stays fresh. RxNorm is updated weekly; a 30-day
# cache is plenty for our normalization use case and keeps API traffic low.
CACHE_TTL = timedelta(days=30)

# Per-request timeout (seconds). The RxNav API is usually fast (<500ms) so
# a short ceiling keeps us from blocking appeal generation when it's down.
DEFAULT_TIMEOUT = 5.0

# Term types we care about when expanding a drug name. See
# https://www.nlm.nih.gov/research/umls/rxnorm/docs/appendix5.html
TTY_INGREDIENT = "IN"
TTY_PRECISE_INGREDIENT = "PIN"
TTY_BRAND_NAME = "BN"
TTY_SEMANTIC_CLINICAL_DRUG = "SCD"
TTY_SEMANTIC_BRANDED_DRUG = "SBD"

EXPAND_TTYS = [
    TTY_INGREDIENT,
    TTY_PRECISE_INGREDIENT,
    TTY_BRAND_NAME,
    TTY_SEMANTIC_CLINICAL_DRUG,
    TTY_SEMANTIC_BRANDED_DRUG,
]

# HTTP headers for RxNav. Including a contact email is a courtesy expected
# by NIH services and helps them reach us if our traffic looks abusive.
_FETCH_HEADERS = {
    "User-Agent": "FightHealthInsurance/1.0 (mailto:support@fighthealthinsurance.com)",
    "Accept": "application/json",
}


@dataclass
class NormalizedDrug:
    """Canonical drug record returned from a normalization call.

    ``rxcui`` is empty when no RxNorm match was found; callers should fall
    back to the original input in that case.
    """

    query: str
    rxcui: str
    canonical_name: str
    tty: str = ""
    score: Optional[int] = None
    related: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return bool(self.rxcui) and bool(self.canonical_name)


def _clean_query(name: str) -> str:
    """Lowercase, trim, collapse whitespace.

    We deliberately keep punctuation (e.g., dashes in "co-trimoxazole") so
    we don't mangle real drug names.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


class RxNormTools:
    """Async client for RxNav with database-backed caching.

    Instances are cheap to create and stateless aside from an optional
    shared :py:class:`aiohttp.ClientSession`. In long-running consumers
    (chat workers, prefetch actors) reuse a single instance per request to
    benefit from connection pooling.
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: str = RXNAV_BASE_URL,
    ) -> None:
        self._external_session = session
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._external_session is not None:
            return self._external_session
        # Lazy create a per-call session if the caller didn't provide one.
        return aiohttp.ClientSession(headers=_FETCH_HEADERS)

    async def _fetch_json(self, url: str) -> Optional[Dict[str, Any]]:
        """GET ``url`` and parse the response as JSON, returning ``None`` on
        any error or non-200 response. Honors :py:attr:`_timeout`."""
        session = await self._get_session()
        owns_session = self._external_session is None
        try:
            async with async_timeout(self._timeout):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug(f"RxNav GET {url} -> HTTP {resp.status}")
                        return None
                    data: Dict[str, Any] = await resp.json()
                    return data
        except asyncio.TimeoutError:
            logger.debug(f"RxNav GET {url} timed out")
            return None
        except Exception as e:
            logger.opt(exception=True).debug(f"RxNav GET {url} failed: {e}")
            return None
        finally:
            if owns_session:
                await session.close()

    # ---- cache helpers ---------------------------------------------------

    async def _cache_get(self, query: str) -> Optional[RxNormConcept]:
        cutoff = timezone.now() - CACHE_TTL
        return await RxNormConcept.objects.filter(
            query=query, created__gte=cutoff
        ).afirst()

    async def _cache_put(
        self,
        query: str,
        rxcui: str,
        canonical_name: str,
        tty: str,
        score: Optional[int],
        related: Dict[str, List[Dict[str, str]]],
    ) -> RxNormConcept:
        # Replace any stale entry for this query so we never accumulate
        # multiple rows per input. This keeps the cache table small.
        await RxNormConcept.objects.filter(query=query).adelete()
        return await RxNormConcept.objects.acreate(
            query=query,
            rxcui=rxcui,
            canonical_name=canonical_name,
            tty=tty,
            score=score,
            related_json=related,
        )

    # ---- RxNav HTTP wrappers --------------------------------------------

    async def _exact_rxcui(self, name: str) -> Optional[Dict[str, str]]:
        """Try an exact name match. Returns ``{"rxcui": ..., "name": ...}``
        if RxNorm has a record with this exact name, else ``None``."""
        url = f"{self._base_url}/rxcui.json?name={quote(name, safe='')}&search=2"
        data = await self._fetch_json(url)
        if not data:
            return None
        ids = (data.get("idGroup") or {}).get("rxnormId") or []
        if not ids:
            return None
        rxcui = str(ids[0])
        # RxNav's rxcui.json doesn't return the canonical name; fetch it.
        props = await self._properties(rxcui)
        if not props:
            return {"rxcui": rxcui, "name": name, "tty": ""}
        return {
            "rxcui": rxcui,
            "name": props.get("name", name),
            "tty": props.get("tty", ""),
        }

    async def _approximate(
        self, name: str, max_entries: int = 4
    ) -> Optional[Dict[str, Any]]:
        """Approximate match — handles misspellings and partial names."""
        url = (
            f"{self._base_url}/approximateTerm.json"
            f"?term={quote(name, safe='')}&maxEntries={max_entries}"
        )
        data = await self._fetch_json(url)
        if not data:
            return None
        candidates = (data.get("approximateGroup") or {}).get("candidate") or []
        if not candidates:
            return None
        best = candidates[0]
        rxcui = str(best.get("rxcui") or "")
        if not rxcui:
            return None
        try:
            score: Optional[int] = int(best.get("score"))
        except (TypeError, ValueError):
            score = None
        props = await self._properties(rxcui)
        return {
            "rxcui": rxcui,
            "name": (props or {}).get("name") or best.get("name") or name,
            "tty": (props or {}).get("tty") or "",
            "score": score,
        }

    async def _properties(self, rxcui: str) -> Optional[Dict[str, str]]:
        """Fetch canonical name + tty for a known RxCUI."""
        url = f"{self._base_url}/rxcui/{quote(rxcui, safe='')}/properties.json"
        data = await self._fetch_json(url)
        if not data:
            return None
        props = data.get("properties") or {}
        if not props:
            return None
        return {
            "name": str(props.get("name") or ""),
            "tty": str(props.get("tty") or ""),
            "synonym": str(props.get("synonym") or ""),
        }

    async def _related(
        self, rxcui: str, ttys: List[str]
    ) -> Dict[str, List[Dict[str, str]]]:
        """Fetch related concepts (brands, ingredients, etc.) for an RxCUI.

        Returns a dict keyed by tty (e.g., "IN", "BN") whose values are
        lists of ``{"rxcui": str, "name": str}`` dicts. Empty dict on
        failure.
        """
        if not rxcui or not ttys:
            return {}
        url = (
            f"{self._base_url}/rxcui/{quote(rxcui, safe='')}/related.json"
            f"?tty={'+'.join(ttys)}"
        )
        data = await self._fetch_json(url)
        if not data:
            return {}
        groups = (data.get("relatedGroup") or {}).get("conceptGroup") or []
        out: Dict[str, List[Dict[str, str]]] = {}
        for group in groups:
            tty = str(group.get("tty") or "")
            props = group.get("conceptProperties") or []
            if not tty or not props:
                continue
            out[tty] = [
                {"rxcui": str(p.get("rxcui") or ""), "name": str(p.get("name") or "")}
                for p in props
                if p.get("name")
            ]
        return out

    # ---- public API ------------------------------------------------------

    async def normalize(self, name: str) -> NormalizedDrug:
        """Normalize a free-text drug name to a canonical RxNorm concept.

        Tries an exact name lookup first, then an approximate (fuzzy) match.
        Always returns a :py:class:`NormalizedDrug`; callers should check
        :py:attr:`NormalizedDrug.matched` before trusting the result.

        Results — including misses — are cached in :py:class:`RxNormConcept`
        for :py:data:`CACHE_TTL` so repeated lookups don't hit the API.
        """
        query = _clean_query(name)
        if not query:
            return NormalizedDrug(query="", rxcui="", canonical_name="")

        cached = await self._cache_get(query)
        if cached is not None:
            # Touching last_used helps an LRU-style eviction job later.
            await RxNormConcept.objects.filter(pk=cached.pk).aupdate(
                last_used=timezone.now()
            )
            return NormalizedDrug(
                query=query,
                rxcui=cached.rxcui,
                canonical_name=cached.canonical_name,
                tty=cached.tty,
                score=cached.score,
                related=cached.related_json or {},
            )

        # Not cached — query RxNav. Try exact match first, then approximate.
        match = await self._exact_rxcui(query)
        score: Optional[int] = 100 if match else None
        if not match:
            approx = await self._approximate(query)
            if approx:
                match = {
                    "rxcui": approx["rxcui"],
                    "name": approx["name"],
                    "tty": approx.get("tty", ""),
                }
                score = approx.get("score")

        if not match:
            # Cache the miss so we don't keep retrying unknown junk strings.
            await self._cache_put(query, "", "", "", None, {})
            return NormalizedDrug(query=query, rxcui="", canonical_name="")

        rxcui = match["rxcui"]
        canonical_name = match["name"]
        tty = match.get("tty", "")
        related = await self._related(rxcui, EXPAND_TTYS)

        await self._cache_put(query, rxcui, canonical_name, tty, score, related)

        return NormalizedDrug(
            query=query,
            rxcui=rxcui,
            canonical_name=canonical_name,
            tty=tty,
            score=score,
            related=related,
        )

    async def expand_query_terms(
        self, name: str, include_brands: bool = True, max_terms: int = 8
    ) -> List[str]:
        """Return search-friendly terms for ``name`` ordered by relevance.

        Use this to broaden a PubMed / policy search beyond what the user
        typed. The canonical name comes first, then ingredient names, then
        brand names. The original input is appended last when no RxNorm
        match exists, so callers can always feed the result back into a
        search engine without losing the user's intent.
        """
        normalized = await self.normalize(name)
        terms: List[str] = []
        seen: set[str] = set()

        def add(term: str) -> None:
            cleaned = (term or "").strip()
            if not cleaned:
                return
            key = cleaned.lower()
            if key in seen:
                return
            seen.add(key)
            terms.append(cleaned)

        if normalized.matched:
            add(normalized.canonical_name)
            for tty in (TTY_INGREDIENT, TTY_PRECISE_INGREDIENT):
                for concept in normalized.related.get(tty, []):
                    add(concept.get("name", ""))
            if include_brands:
                for concept in normalized.related.get(TTY_BRAND_NAME, []):
                    add(concept.get("name", ""))

        # Always include the original cleaned query so callers don't lose
        # the user's input (and so a miss still returns something useful).
        add(_clean_query(name))

        return terms[:max_terms]

    async def get_brands_and_generics(self, name: str) -> Dict[str, Any]:
        """Structured view of a drug for display / chat answers.

        Returns a dict with::

            {
                "matched": bool,
                "rxcui": str,
                "canonical_name": str,
                "tty": str,             # "IN", "BN", ...
                "ingredients": [name, ...],
                "brand_names": [name, ...],
            }
        """
        normalized = await self.normalize(name)
        ingredients = [
            c["name"]
            for c in normalized.related.get(TTY_INGREDIENT, [])
            if c.get("name")
        ]
        # Fall back to PIN if no plain IN entries were returned.
        if not ingredients:
            ingredients = [
                c["name"]
                for c in normalized.related.get(TTY_PRECISE_INGREDIENT, [])
                if c.get("name")
            ]
        brand_names = [
            c["name"]
            for c in normalized.related.get(TTY_BRAND_NAME, [])
            if c.get("name")
        ]
        return {
            "matched": normalized.matched,
            "rxcui": normalized.rxcui,
            "canonical_name": normalized.canonical_name,
            "tty": normalized.tty,
            "ingredients": ingredients,
            "brand_names": brand_names,
        }


_default_tools: Optional[RxNormTools] = None


def get_default_rxnorm_tools() -> RxNormTools:
    """Process-wide singleton for callers that don't manage their own."""
    global _default_tools
    if _default_tools is None:
        _default_tools = RxNormTools()
    return _default_tools


async def normalize_drug_name(name: str) -> NormalizedDrug:
    """Convenience wrapper around the default :py:class:`RxNormTools`."""
    return await get_default_rxnorm_tools().normalize(name)


async def expand_drug_query_terms(name: str, max_terms: int = 8) -> List[str]:
    """Convenience wrapper around :py:meth:`RxNormTools.expand_query_terms`."""
    return await get_default_rxnorm_tools().expand_query_terms(
        name, max_terms=max_terms
    )


def normalize_drug_name_sync(name: str) -> NormalizedDrug:
    """Sync wrapper for callers that aren't async-aware (e.g., admin views).

    Internally runs :py:func:`normalize_drug_name` on a private event loop
    via ``asgiref``. Avoid in tight loops.
    """
    result: NormalizedDrug = async_to_sync(normalize_drug_name)(name)
    return result
