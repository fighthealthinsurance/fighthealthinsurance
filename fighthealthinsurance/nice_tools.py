"""Integration with NICE (National Institute for Health and Care Excellence) syndication API.

NICE publishes evidence-based clinical recommendations for the UK NHS. The syndication API
exposes that guidance for re-use. NICE is not a U.S. coverage authority, so its content is
phrased as "international clinical guidance" when surfaced in appeals, complementing U.S.
sources where they are sparse or vague.

API docs: https://www.nice.org.uk/about/what-we-do/our-programmes/nice-guidance/nice-syndication
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import aiohttp
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    NICEGuidance,
    NICEQueryData,
)

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


# Default base URL for the NICE syndication API. Overridable via env var for testing or
# for pointing at a future endpoint without a code change.
NICE_API_BASE_URL = os.getenv("NICE_API_BASE_URL", "https://api.nice.org.uk")

# How many guidance items we keep per query.
PER_QUERY_LIMIT = 3

# Cap how many guidance items end up formatted into the appeal context.
MAX_GUIDANCE_FOR_CONTEXT = 5

# How long cached query results stay fresh before we re-query.
QUERY_CACHE_DAYS = 30

# Standard caveat appended to all NICE-derived context to avoid mis-citing it as a
# U.S. coverage authority.
INTERNATIONAL_GUIDANCE_CAVEAT = (
    "Note: NICE (UK) is referenced here as international clinical guidance, not as "
    "U.S. coverage authority. It is offered as supporting evidence-based clinical "
    "rationale where U.S. guidelines are sparse or payer policies are vague."
)


_FETCH_HEADERS = {
    "User-Agent": "FightHealthInsurance/1.0 (mailto:support@fighthealthinsurance.com)",
    "Accept": "application/json,application/xml;q=0.9,*/*;q=0.8",
}


class NICETools:
    """Async client for the NICE syndication API.

    Mirrors the shape of PubMedTools so callers can fan out queries the same way.
    Cached results live in NICEQueryData; full guidance items live in NICEGuidance.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Treat None as "fall back to env"; treat "" as "explicitly no key" so tests
        # can disable the API path without the env leaking in.
        self.api_key = api_key if api_key is not None else os.getenv("NICE_API_KEY", "")
        self.base_url = NICE_API_BASE_URL.rstrip("/")

    def _auth_headers(self) -> Dict[str, str]:
        headers = dict(_FETCH_HEADERS)
        if self.api_key:
            # The syndication API accepts the key under either header. We send the
            # Azure-style header since NICE's gateway uses APIM in front of it.
            headers["Api-Key"] = self.api_key
            headers["Ocp-Apim-Subscription-Key"] = self.api_key
        return headers

    @staticmethod
    def _build_query(procedure: str, diagnosis: str) -> str:
        """Combine procedure and diagnosis into a single search query."""
        parts = [p.strip() for p in (procedure, diagnosis) if p and p.strip()]
        return " ".join(parts).strip()

    async def _fetch_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        timeout_secs: float,
        label: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Fetch JSON from url with timeout. Returns None on any failure."""
        try:
            async with async_timeout(timeout_secs):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug(
                            f"[NICE:{label}] non-200 status {resp.status} for {url}"
                        )
                        return None
                    # NICE may return XML for some endpoints, but search returns JSON.
                    content_type = resp.headers.get("Content-Type", "")
                    if "json" not in content_type:
                        logger.debug(
                            f"[NICE:{label}] unexpected content-type {content_type}"
                        )
                        return None
                    data: Dict[str, Any] = await resp.json()
                    return data
        except Exception as e:
            logger.debug(f"[NICE:{label}] JSON fetch failed: {e}")
        return None

    @staticmethod
    def _extract_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Pull guidance items from a NICE search response, tolerating shape changes.

        The syndication search response has historically used either a top-level
        `documents` list or a nested `results.items` shape. We accept either.
        """
        if not isinstance(data, dict):
            return []
        for key in ("documents", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                inner = value.get("items") or value.get("documents")
                if isinstance(inner, list):
                    return [item for item in inner if isinstance(item, dict)]
        return []

    @staticmethod
    def _normalize_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Reduce a NICE search item to the small set of fields we persist."""
        # Field names vary slightly between API revisions; check the common variants.
        guidance_id = (
            item.get("id")
            or item.get("guidanceId")
            or item.get("reference")
            or item.get("Reference")
        )
        title = item.get("title") or item.get("Title")
        url = item.get("url") or item.get("Url") or item.get("link")
        guidance_type = (
            item.get("type") or item.get("Type") or item.get("guidanceType") or ""
        )
        summary = (
            item.get("summary")
            or item.get("Summary")
            or item.get("description")
            or item.get("Description")
            or ""
        )
        if not guidance_id or not title:
            return None
        return {
            "guidance_id": str(guidance_id).strip(),
            "title": str(title).strip(),
            "url": str(url).strip() if url else "",
            "guidance_type": str(guidance_type).strip(),
            "summary": str(summary).strip(),
        }

    async def search_guidance(
        self,
        query: str,
        timeout: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """Search NICE syndication for guidance matching `query`.

        Caches results in NICEQueryData for QUERY_CACHE_DAYS to avoid hammering the API.
        Returns a list of normalized guidance dicts (possibly empty).
        """
        if not query or not query.strip():
            return []
        query = query.strip()

        # Serve from cache when possible.
        try:
            recent_cutoff = datetime.now() - timedelta(days=QUERY_CACHE_DAYS)
            cached = NICEQueryData.objects.filter(
                query=query,
                created__gte=recent_cutoff,
                denial_id__isnull=True,
            ).order_by("-created")
            if await cached.aexists():
                first = await cached.afirst()
                if first and first.results:
                    try:
                        items = json.loads(first.results.replace("\x00", ""))
                        if isinstance(items, list):
                            return items
                    except json.JSONDecodeError:
                        logger.error(
                            f"Error parsing cached NICE results JSON for {query}"
                        )
        except Exception as e:
            logger.opt(exception=True).debug(f"NICE cache lookup failed: {e}")

        if not self.api_key:
            # Without an API key we can't talk to syndication. Return empty rather
            # than failing the whole appeal generation.
            logger.debug("NICE_API_KEY not set; skipping NICE search")
            return []

        url = f"{self.base_url}/services/search?q={quote(query)}&pageSize={PER_QUERY_LIMIT}"
        items: List[Dict[str, Any]] = []
        try:
            async with async_timeout(timeout):
                async with aiohttp.ClientSession(
                    headers=self._auth_headers()
                ) as session:
                    data = await self._fetch_json(session, url, timeout, label=query)
                    if data:
                        for raw in self._extract_items(data)[:PER_QUERY_LIMIT]:
                            normalized = self._normalize_item(raw)
                            if normalized:
                                items.append(normalized)
        except asyncio.TimeoutError:
            logger.debug(f"Timeout searching NICE for {query}")
        except asyncio.exceptions.CancelledError:
            logger.debug(f"Cancelled NICE search for {query}")
        except Exception as e:
            logger.opt(exception=True).debug(f"NICE search error for {query}: {e}")

        if items:
            try:
                await NICEQueryData.objects.acreate(
                    query=query,
                    results=json.dumps(items).replace("\x00", ""),
                )
            except Exception as e:
                logger.opt(exception=True).debug(f"Failed to cache NICE results: {e}")
        return items

    async def find_guidance_for_denial(
        self,
        denial: Denial,
        timeout: float = 30.0,
    ) -> List[NICEGuidance]:
        """Look up NICE guidance for a denial, persisting results as NICEGuidance rows."""
        # Re-fetch to pick up fields written elsewhere in the appeal pipeline.
        denial = await Denial.objects.aget(pk=denial.denial_id)

        procedure_opt = denial.procedure or ""
        diagnosis_opt = denial.diagnosis or ""
        query = self._build_query(procedure_opt, diagnosis_opt)
        if not query:
            return []

        guidance_models: List[NICEGuidance] = []
        seen_ids: Set[str] = set()
        try:
            async with async_timeout(timeout):
                items = await self.search_guidance(query, timeout=timeout)
                for item in items:
                    gid = item["guidance_id"]
                    if gid in seen_ids:
                        continue
                    seen_ids.add(gid)
                    existing = await NICEGuidance.objects.filter(
                        guidance_id=gid
                    ).afirst()
                    if existing:
                        guidance_models.append(existing)
                        continue
                    try:
                        created = await NICEGuidance.objects.acreate(
                            guidance_id=gid,
                            title=item["title"],
                            url=item["url"],
                            guidance_type=item["guidance_type"],
                            summary=item["summary"],
                        )
                        guidance_models.append(created)
                    except Exception as e:
                        logger.opt(exception=True).debug(
                            f"Failed to persist NICE guidance {gid}: {e}"
                        )
        except asyncio.TimeoutError:
            logger.debug(f"Timeout finding NICE guidance for denial {denial.denial_id}")
        except asyncio.exceptions.CancelledError:
            logger.debug(
                f"Cancelled finding NICE guidance for denial {denial.denial_id}"
            )

        if guidance_models:
            try:
                await NICEQueryData.objects.acreate(
                    denial_id=denial,
                    query=query,
                    results=json.dumps(
                        [g.guidance_id for g in guidance_models]
                    ).replace("\x00", ""),
                )
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Failed to record NICE query for denial: {e}"
                )
        return guidance_models

    async def find_context_for_denial(
        self,
        denial: Denial,
        timeout: float = 30.0,
    ) -> str:
        """Return formatted NICE context for a denial, caching the result on the denial."""
        result = await self._find_context_for_denial(denial, timeout)
        if result:
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                nice_context=result
            )
        return result

    async def _find_context_for_denial(
        self,
        denial: Denial,
        timeout: float = 30.0,
    ) -> str:
        """Worker that builds the NICE context string without persisting it."""
        if denial.nice_context and len(denial.nice_context) > 1:
            return denial.nice_context

        # No API key configured: skip everything, including the DB lookup. This
        # keeps unit tests that don't mock the NICE backend from hitting the ORM
        # and matches production behavior in environments without a NICE key.
        if not self.api_key:
            return ""

        try:
            guidance_models = await self.find_guidance_for_denial(
                denial, timeout=timeout
            )
        except Exception as e:
            logger.opt(exception=True).debug(
                f"NICE context lookup failed for denial {denial.denial_id}: {e}"
            )
            return ""

        if not guidance_models:
            return ""

        formatted = [
            self.format_guidance_short(g)
            for g in guidance_models[:MAX_GUIDANCE_FOR_CONTEXT]
        ]
        body = "\n".join(formatted)
        return f"{INTERNATIONAL_GUIDANCE_CAVEAT}\n{body}"

    @staticmethod
    def format_guidance_short(guidance: NICEGuidance) -> str:
        """One-line citation suitable for inclusion in an appeal prompt."""
        parts: List[str] = []
        if guidance.guidance_id:
            parts.append(f"NICE {guidance.guidance_id}")
        if guidance.guidance_type:
            parts.append(f"Type: {guidance.guidance_type}")
        if guidance.title:
            parts.append(f"Title: {guidance.title}")
        if guidance.url:
            parts.append(f"URL: {guidance.url}")
        if guidance.summary:
            summary = guidance.summary
            if len(summary) > 400:
                summary = summary[:400].rstrip() + "..."
            parts.append(f"Summary: {summary}")
        return "; ".join(parts)
