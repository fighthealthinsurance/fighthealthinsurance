"""Lookups against Medicaid.gov and a few other trusted coverage references.

Two things live here:

* A small registry of CURATED pages we point people at often -- the renewal
  hub and its per-state pages, the official income-eligibility table, and a
  couple of federal-poverty-level references. These are named so the model
  asks for ``renew_info`` rather than trying to remember a 130-character URL.
* A search over Medicaid.gov backed by its SITEMAP.

Why the sitemap and not the site's own search box: medicaid.gov runs Google
Vertex AI Search, which renders results client-side from a credentialed
datastore -- there is no public query endpoint -- and ``/search/`` is
``Disallow``ed in their robots.txt besides. The sitemap is the sanctioned
index of the same pages, so matching against it gets us "look up a page on
medicaid.gov" without scraping a search UI we are asked not to touch.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests
from django.core.cache import cache
from loguru import logger

MEDICAID_GOV = "https://www.medicaid.gov"
SITEMAP_INDEX_URL = f"{MEDICAID_GOV}/sitemap.xml"

# Hosts this module is allowed to hand back. Everything here is a public
# government or non-profit coverage reference; the chat tool refuses anything
# else so a model can't turn this into a general-purpose fetcher.
ALLOWED_HOSTS = frozenset(
    {
        "www.medicaid.gov",
        "medicaid.gov",
        "www.coveredca.com",
        "coveredca.com",
        "www.healthinsurance.org",
        "healthinsurance.org",
    }
)

# Paths medicaid.gov's robots.txt asks crawlers to stay out of. Checked
# against sitemap entries and explicit URLs alike -- the sitemap shouldn't
# contain these, but a model guessing a URL might.
_DISALLOWED_PREFIXES = (
    "/core/",
    "/profiles/",
    "/admin/",
    "/comment/reply/",
    "/filter/tips",
    "/node/",
    "/search/",
    "/taxonomy/",
    "/themes/",
    "/libraries/",
    "/modules/",
    "/devel/",
    "/user/",
    "/index.php/",
)

_SITEMAP_CACHE_KEY = "medicaid_gov_sitemap_urls_v1"
_SITEMAP_CACHE_SECONDS = 60 * 60 * 24  # the sitemap's lastmod moves daily
# Per-request timeout. Deliberately short: this runs while a person is
# waiting on a chat reply, so a slow upstream should cost us the lookup, not
# the turn.
_SITEMAP_TIMEOUT_SECONDS = 5
# Whole-walk budget. The per-request timeout alone bounds nothing useful --
# _MAX_SITEMAP_PAGES + 1 requests that each take just under the timeout still
# add up to a minute -- so the walk also stops once this much has elapsed and
# uses whatever it collected.
_SITEMAP_TOTAL_BUDGET_SECONDS = 15
# The index paginates at 1,500 URLs a page; cap the walk so a sitemap that
# grows unexpectedly can't turn one lookup into a crawl.
_MAX_SITEMAP_PAGES = 10
# Largest sitemap document we will parse. medicaid.gov's pages run well under
# a megabyte; anything far larger is a mistake or a malicious response, and
# handing it to the XML parser is how you turn a lookup into an OOM.
_MAX_SITEMAP_BYTES = 8 * 1024 * 1024

_SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass(frozen=True)
class CuratedSource:
    """A page we hand out by name instead of by URL."""

    key: str
    url: str
    description: str
    # When set, ``state`` picks a per-state variant of the page.
    state_url_template: Optional[str] = None


CURATED_SOURCES: Dict[str, CuratedSource] = {
    "renew_info": CuratedSource(
        key="renew_info",
        url=f"{MEDICAID_GOV}/renew-info",
        description=(
            "Official Medicaid/CHIP renewal hub -- how to keep coverage, what "
            "the state will mail you, and what to do if you're disenrolled."
        ),
        state_url_template=f"{MEDICAID_GOV}/renew-info/{{state}}/",
    ),
    "eligibility_levels": CuratedSource(
        key="eligibility_levels",
        url=(
            f"{MEDICAID_GOV}/medicaid/national-medicaid-chip-program-information"
            "/medicaid-childrens-health-insurance-program-basic-health-program"
            "-eligibility-levels"
        ),
        description=(
            "Official Medicaid / CHIP / Basic Health Program income "
            "eligibility levels by state and category."
        ),
    ),
    "fpl_chart": CuratedSource(
        key="fpl_chart",
        url="https://www.coveredca.com/pdfs/FPL-chart.pdf",
        description=(
            "Covered California's federal-poverty-level chart (PDF) -- FPL "
            "dollar amounts by household size and percentage band."
        ),
    ),
    "fpl_glossary": CuratedSource(
        key="fpl_glossary",
        url="https://www.healthinsurance.org/glossary/federal-poverty-level/",
        description=(
            "Plain-language explanation of what the federal poverty level is "
            "and how coverage programs use it."
        ),
    ),
}


# Phrasings that should land on a curated page even though they don't appear
# in any URL slug. The sitemap carries no page titles, so slug matching alone
# misses the way people actually ask ("my coverage is ending" is nowhere in
# "/renew-info"). Keyword -> curated key.
_CURATED_ALIASES: Dict[str, Tuple[str, ...]] = {
    "renew_info": (
        "renew",
        "renewal",
        "redetermination",
        "recertification",
        "keep my coverage",
        "coverage ending",
        "coverage is ending",
        "coverage ends",
        "lose my coverage",
        "losing coverage",
        "disenrolled",
        "terminated",
        "paperwork",
    ),
    "eligibility_levels": (
        "income limit",
        "income limits",
        "eligibility level",
        "eligibility levels",
        "how much can i make",
        "income threshold",
        "qualify income",
    ),
    "fpl_chart": (
        "fpl chart",
        "poverty level chart",
        "poverty guidelines",
        "fpl table",
        "percent of poverty",
    ),
    "fpl_glossary": (
        "federal poverty level",
        "what is fpl",
        "poverty level mean",
    ),
}


def suggest_curated_sources(query: str) -> List[CuratedSource]:
    """Curated pages whose subject matches ``query``, best first.

    Checked before the sitemap: these are the pages we already know answer
    the question, and slug matching would not find several of them.
    """
    if not query or not isinstance(query, str):
        return []
    lowered = query.lower()
    scored: List[Tuple[int, CuratedSource]] = []
    for key, aliases in _CURATED_ALIASES.items():
        source = CURATED_SOURCES.get(key)
        if source is None:
            continue
        hits = sum(1 for alias in aliases if alias in lowered)
        if hits:
            scored.append((hits, source))
    scored.sort(key=lambda pair: (-pair[0], pair[1].key))
    return [source for _, source in scored]


def is_allowed_url(url: str) -> bool:
    """Whether ``url`` is an https page on an allowed host and not robots-blocked."""
    if not url:
        return False
    parsed = urlparse(url)
    # https only. Every curated source is https, and these pages are the
    # authority we quote to people about their coverage -- fetching one over
    # cleartext would let anything on the path rewrite the answer.
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return False
    if host.endswith("medicaid.gov"):
        path = parsed.path or "/"
        if any(path.startswith(prefix) for prefix in _DISALLOWED_PREFIXES):
            return False
    return True


def resolve_curated_source(page: str, state: Optional[str] = None) -> Optional[str]:
    """URL for a curated page name, optionally its per-state variant.

    ``state`` accepts anything ``medicaid_api`` can normalize -- a full name,
    a postal code, or a state program name like "Medi-Cal" -- so the model
    doesn't have to convert "Iowa" to "ia" itself.
    """
    if not page or not isinstance(page, str):
        return None
    source = CURATED_SOURCES.get(page.strip().lower().replace("-", "_"))
    if source is None:
        return None

    if state and source.state_url_template:
        # Imported here: medicaid_api pulls in pandas, and this module is
        # imported by the chat tools on every chat.
        from fighthealthinsurance.medicaid_api import _normalize_state

        try:
            code = _normalize_state(state)
        except ValueError:
            code = None
        if code:
            return source.state_url_template.format(state=code)
        # An unreadable state falls back to the national page rather than
        # guessing a slug that would 404.
        logger.debug(f"medicaid_gov: could not resolve state {state!r}, using national")
    return source.url


def curated_source_menu() -> str:
    """One line per curated page, for the tool's own error/help text."""
    lines = []
    for source in CURATED_SOURCES.values():
        suffix = ' (accepts "state")' if source.state_url_template else ""
        lines.append(f'- "{source.key}"{suffix}: {source.description}')
    return "\n".join(lines)


def _parse_sitemap_xml(content: bytes) -> ET.Element:
    """Parse a sitemap document, refusing anything that isn't plain XML.

    Two guards, both because this is remote content we don't control:

    * A size cap, so an unexpectedly enormous (or hostile) response can't be
      handed to the parser to expand in memory.
    * No document type declaration. Sitemaps have no DTD, so a ``<!DOCTYPE``
      or ``<!ENTITY`` here is either a mistake or an entity-expansion attempt.
      Current expat refuses external entities and is no longer vulnerable to
      the classic expansion attacks, but rejecting the declaration outright
      costs nothing and doesn't depend on the parser version underneath us.
    """
    if len(content) > _MAX_SITEMAP_BYTES:
        raise ValueError(f"sitemap document too large ({len(content)} bytes)")
    head = content[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("sitemap document carries a doctype/entity declaration")
    return ET.fromstring(content)


def _fetch_sitemap_urls() -> List[str]:
    """Every content URL in medicaid.gov's sitemap, cached for a day.

    Blocking: does network IO with ``requests``. Callers on the event loop
    must bridge (see ``MedicaidGovLookupTool._resolve_target``).
    """
    cached = cache.get(_SITEMAP_CACHE_KEY)
    if cached is not None:
        return list(cached)

    deadline = time.monotonic() + _SITEMAP_TOTAL_BUDGET_SECONDS
    urls: List[str] = []
    try:
        index = requests.get(SITEMAP_INDEX_URL, timeout=_SITEMAP_TIMEOUT_SECONDS)
        index.raise_for_status()
        root = _parse_sitemap_xml(index.content)
        pages = [
            loc.text.strip()
            for loc in root.findall(".//s:sitemap/s:loc", _SITEMAP_NS)
            if loc.text
        ]
        if not pages:
            # A flat sitemap rather than an index.
            pages = [SITEMAP_INDEX_URL]

        for page_url in pages[:_MAX_SITEMAP_PAGES]:
            if time.monotonic() >= deadline:
                # Out of time. Keep what we have rather than dropping the
                # lookup: a partial index still answers most queries, and the
                # person is waiting on a chat reply.
                logger.warning(
                    f"medicaid_gov: sitemap walk hit its "
                    f"{_SITEMAP_TOTAL_BUDGET_SECONDS}s budget with "
                    f"{len(urls)} URLs collected"
                )
                break
            page = requests.get(page_url, timeout=_SITEMAP_TIMEOUT_SECONDS)
            page.raise_for_status()
            page_root = _parse_sitemap_xml(page.content)
            for loc in page_root.findall(".//s:url/s:loc", _SITEMAP_NS):
                if loc.text:
                    candidate = loc.text.strip()
                    if is_allowed_url(candidate):
                        urls.append(candidate)
    except Exception as e:
        logger.warning(f"medicaid_gov: sitemap fetch failed: {e}")
        # Cache the failure briefly so a site outage doesn't make every chat
        # turn pay the timeout.
        cache.set(_SITEMAP_CACHE_KEY, [], 300)
        return []

    urls = sorted(set(urls))
    cache.set(_SITEMAP_CACHE_KEY, urls, _SITEMAP_CACHE_SECONDS)
    logger.debug(f"medicaid_gov: cached {len(urls)} sitemap URLs")
    return urls


_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that appear in half the site's slugs and so tell us nothing about
# which page the user wants.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "chip",
        "do",
        "does",
        "for",
        "from",
        "get",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "medicaid",
        "my",
        "of",
        "on",
        "or",
        "programs",
        "state",
        "states",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
    }
)


def _slug_words(url: str) -> List[str]:
    return _WORD_RE.findall(urlparse(url).path.lower())


# Shortest prefix we'll treat as a word match. Long enough that "car" does
# not match "caregiver", short enough that "renew"/"renewal" and
# "apply"/"application" connect -- URL slugs and the way people phrase
# questions rarely agree on suffixes.
_MIN_PREFIX_MATCH = 4


def _words_match(query_word: str, slug_word: str) -> bool:
    if query_word == slug_word:
        return True
    shorter, longer = sorted((query_word, slug_word), key=len)
    return len(shorter) >= _MIN_PREFIX_MATCH and longer.startswith(shorter)


def _score(query_words: Sequence[str], url: str) -> float:
    """How well ``url``'s slug matches the query. 0 means no match."""
    slug = _slug_words(url)
    if not slug:
        return 0.0
    hits = [w for w in query_words if any(_words_match(w, s) for s in slug)]
    if not hits:
        return 0.0
    # Coverage of the query matters most; shallow pages break ties, since
    # /renew-info beats /renew-info/some/deep/child for a general question.
    coverage = len(hits) / len(query_words)
    depth_penalty = 1.0 + 0.08 * max(0, len(slug) - len(hits))
    return coverage / depth_penalty


def search_medicaid_gov(query: str, limit: int = 5) -> List[Tuple[str, float]]:
    """Rank medicaid.gov pages against ``query``, best first.

    Matches on URL slugs from the sitemap -- see the module docstring for why
    this stands in for the site's own (credentialed, robots-blocked) search.

    Blocking on a cold cache (it walks the sitemap); bridge off the event
    loop rather than awaiting it inline.
    Returns ``(url, score)`` pairs; an empty list means nothing matched or the
    sitemap was unreachable.
    """
    if not query or not isinstance(query, str):
        return []
    query_words = [
        w for w in _WORD_RE.findall(query.lower()) if w not in _STOPWORDS and len(w) > 2
    ]
    if not query_words:
        return []

    scored = []
    for url in _fetch_sitemap_urls():
        score = _score(query_words, url)
        if score > 0:
            scored.append((url, score))

    scored.sort(key=lambda pair: (-pair[1], len(pair[0]), pair[0]))
    return scored[: max(1, limit)]
