"""Check the identifiers in citations a model wrote before they reach a letter.

The citation backends are asked for real, verifiable references, but nothing
checked that the DOIs, PubMed IDs and CFR sections they return exist. This
module finds those identifiers in each citation string and asks the public
registry whether each one exists:

* DOIs: the DOI handle service (doi.org/api/handles).
* PubMed IDs: NCBI E-utilities esummary, the service the app already uses.
* CFR sections: eCFR's versioner API, whose structure of a title's part
  lists every section in it, and which answers a missing part with an
  empty JSON object.

U.S. Code sections are never looked up: there is no simple public lookup,
and a well-known statute cited from memory is usually right.

What is sent, and when:

* Only an identifier of the expected shape is sent: a DOI whose suffix uses
  the registry's recommended characters, at most 100 of them, with no path
  segments such as ".."; a PubMed ID of digits; a CFR title, part and
  section of numbers. The form check bounds the shape of what goes out; it
  cannot tell what a well-formed string means.
* A DOI is also held back when any four characters in a row of its suffix
  appear in the denial's text, health history, email, employer or claim
  number (``guard_text``), so a suffix that overlaps the person's own data
  is not sent.
* Nothing is sent, and no cached verdict is used, unless ``may_send`` says
  the person's consent allows it. It is asked before the cache is read, right
  before the first request and again before later ones.
* Requests go straight through an httpx transport, not through
  ``httpx.AsyncClient.send``, which the error tracker's httpx integration
  wraps: that would put the full URL (identifiers, NCBI key) on a tracing
  span and send tracing headers to the registry.

What happens to a citation:

* An identifier its registry says does not exist (a documented "not found"
  answer): a short citation is dropped; in a long one (a backend's whole
  answer) every occurrence of that identifier is cut out and the rest kept.
  This is the only thing that removes anything. A DOI read with a trailing
  "." or ";" taken off is labelled instead, since a registered DOI can end
  in one.
* An identifier that was not sent (odd shape, a range, overlap with the
  person's data, no consent, over the lookup limit) or could not be checked
  (timeout, refusal, error, an answer of an unexpected kind such as a
  redirect or a page that is not the registry's JSON): the citation is kept
  with ``UNVERIFIED_LABEL`` in front, so the letter writer knows not to lean
  on it.
* Every identifier found: kept as it is.
* No identifier at all (a guideline title, a URL, a plain-text reference):
  kept as it is. There is nothing to look up.

Every check is time-boxed, parsing included: a registry that is slow or down
never holds up a letter.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import (
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    cast,
)
from urllib.parse import quote

import httpx
from django.core.cache import cache

# httpx's own reading of the proxy environment, so this client picks the
# same proxy an httpx.AsyncClient would (scheme-less HTTPS_PROXY values,
# NO_PROXY entries with or without a scheme, "*", IPs).
from httpx._utils import URLPattern, get_environment_proxies
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable

UNVERIFIED_LABEL = "Unverified, use only if you can confirm it: "

# Put in place of an identifier cut out of a long citation.
REMOVED_REFERENCE = "(unverified reference removed)"

# A citation at most this long is one reference, and goes when one of its
# identifiers is missing. Longer strings are a backend's whole answer, which
# usually holds several references; only the missing identifier is cut out.
SHORT_CITATION_CHARS = 500
# Only this much of a citation is scanned for identifiers; a longer one is
# labelled unverified, since part of it went unread.
MAX_SCAN_CHARS = 20_000

# The whole check for one denial's citations, in seconds: parsing, cache
# reads, consent checks and requests. What has not answered by then counts
# as unchecked.
CHECK_BUDGET_SECONDS = 6.0
# Each request to a registry, in seconds.
REQUEST_TIMEOUT_SECONDS = 3.0
# Identifiers looked up (cache or registry) per run; the rest count as
# unchecked without either.
MAX_LOOKUPS = 25
# Requests in flight at once.
MAX_CONCURRENT_LOOKUPS = 5
# How long a "yes" from the consent check holds before it is asked again.
CONSENT_RECHECK_SECONDS = 1.0
# Closing the client after the deadline is bounded too.
_CLOSE_TIMEOUT_SECONDS = 1.0

# Registry answers are cached by identifier. Existence is stable; absence is
# kept shorter in case a new DOI or PubMed record is still being indexed.
# Only those two verdicts are cached, never an error or a refusal.
_CACHE_PREFIX = "citecheck:v3:"
_CACHE_FOUND_SECONDS = 30 * 24 * 3600
_CACHE_MISSING_SECONDS = 7 * 24 * 3600
# Regulation sections are amended, added and removed; DOIs and PubMed IDs are
# permanent. So a CFR answer is kept for less time.
_CFR_CACHE_FOUND_SECONDS = 7 * 24 * 3600
_CFR_CACHE_MISSING_SECONDS = 24 * 3600

_DOI_HANDLE_URL = "https://doi.org/api/handles/"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_ECFR_STRUCTURE_URL = "https://www.ecfr.gov/api/versioner/v1/structure/current/"

FOUND = "found"
MISSING = "missing"
UNCHECKED = "unchecked"
# Well formed, and not looked up by design (U.S. Code).
FORM_ONLY = "form_only"
# Kept for callers and tests that name it; an identifier of the wrong shape
# is never sent and counts as unchecked.
MALFORMED = UNCHECKED


def checks_enabled() -> bool:
    """CITATION_CHECKS_ENABLED, on unless set to 0/false/no/off."""
    raw = (get_env_variable("CITATION_CHECKS_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Identifier:
    """One identifier found in a citation string. ``malformed`` means it is
    not of a shape that may be sent (wrong form, a range, possibly cut
    short); it is never looked up."""

    kind: str  # "doi", "pmid", "cfr" or "usc"
    value: str  # normalized: DOI lowercased, PMID digits, "45/147/147.136"
    malformed: bool = False

    @property
    def key(self) -> Tuple[str, str, bool]:
        return (self.kind, self.value, self.malformed)


@dataclass(frozen=True)
class Occurrence:
    """Where an identifier appears in a citation, for cutting it out."""

    ident: Identifier
    start: int
    end: int


# A DOI suffix may only hold the characters Crossref recommends, and at most
# this many of them. Anything else makes the "DOI" one that is not sent.
_DOI_MAX_SUFFIX = 100
_DOI_VALID = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9\-._;()/:]{1,%d}$" % _DOI_MAX_SUFFIX)
# A DOI candidate: "10." with a registrant code and "/", up to whitespace,
# a quote (straight or curly), a bracket, a brace or "<". Not preceded by a
# word character, a dot, a hyphen or a slash (other than doi.org's own), so
# "110.1234/x" or a version number is not one.
_DOI_CANDIDATE = re.compile(
    "(?:(?<=doi\\.org/)|(?<![\\w.\\-/]))10\\.\\d{4,9}/"
    "[^\\s\"'‘’“”<>\\[\\]{}|\\\\^`]+"
)
# A URL query or fragment after a DOI: "?utm_source=..." or "#abstract".
_DOI_QUERY = re.compile(r"(?:\?[\w.\-]+=|#[\w\-]*$)")
# Prefixes that belong to a DOI occurrence when cutting it out.
_DOI_PREFIX = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi\.org/|doi[*_\s]{0,5}:[*_\s]{0,5})$",
    re.IGNORECASE,
)
# Something introduced as a DOI ("DOI:", "**DOI:**", "doi.org/").
_DOI_MENTION = re.compile(
    r"\bdoi(?:\.org/|[*_\s]{0,5}:[*_\s]{0,5})(\S{1,200})", re.IGNORECASE
)
_DOI_START = re.compile(r"10\.\d{4,9}/")
# What "DOI:" is sometimes followed by when there is no DOI.
_NO_DOI = re.compile(
    r"^(?:n/?a|none|null|unknown|unavailable|not\s+available|tbd|-+)$", re.I
)
_PMID_VALID = re.compile(r"^[1-9]\d{0,8}$")
_PMID_TEXT = re.compile(
    r"\bPMID[*_\s]{0,5}[:#]?[*_\s]{0,5}([^\s,;\])*_]{1,20})", re.IGNORECASE
)
_PUBMED_URL = re.compile(
    r"(?:https?://)?(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/([^\s/?#\])\"'<>]{1,20})/?",
    re.IGNORECASE,
)
_DASH = "-‐‑‒–—"
# Subsections such as "(b)(1)" may follow a section, and a subsection range
# "(b)(1)-(3)".
_SUBSECTIONS = (
    r"((?:\([0-9A-Za-z]{1,4}\)|\s{0,2}[" + _DASH + r"]\s{0,2}\([0-9A-Za-z]{1,4}\))*)"
)
# A range of sections, "438.400-438.424".
# A range of sections: "438.400-438.424", or "147.136 through 147.140".
_SECTION_RANGE = (
    r"(?P<range>\s{0,2}[" + _DASH + r"]\s{0,2}\d{1,5}\.\d{1,5}"
    r"|\s{1,3}(?:through|thru|to)\s{1,3}\d{1,5}(?:\.\d{1,5})?)?"
)
# Anything else stuck to the number ("147.136oops", "300gg-???") makes it an
# identifier that is not sent, rather than letting a valid-looking prefix
# through. Markdown emphasis and quotes end the number.
_STUCK_ON = "([^\\s,;:.)\\]*_\"'‘’“”]*)"
_CFR_TEXT = re.compile(
    r"(?<![\w.])(?P<title>\d{1,3})\s{0,3}C\.?\s{0,2}F\.?\s{0,2}R\.?\s{0,3}"
    r"(?:Parts?|Sec(?:tion)?s?\.?|(?P<signs>§{1,2}))?\s{0,3}"
    r"(?P<part>\d{1,5})(?:\.(?P<sub>\d{1,5})"
    r"(?:(?P<dash>["
    + _DASH
    + r"])(?P<tail>\d{1,5}[A-Z]?)(?!\.\d))?)?"
    + _SECTION_RANGE
    + _SUBSECTIONS
    + _STUCK_ON,
    re.IGNORECASE,
)
_USC_TEXT = re.compile(
    r"(?<![\w.])(\d{1,3})\s{0,3}U\.?\s{0,2}S\.?\s{0,2}C\.?\s{0,3}"
    r"(?:Sec(?:tion)?s?\.?|§{1,2})?\s{0,3}"
    r"(\d{1,6}[a-z]{0,3}(?:["
    + _DASH
    + r"]\d{1,5}[a-z]{0,2})?)"
    + _SUBSECTIONS
    + _STUCK_ON,
    re.IGNORECASE,
)
# A dot and a letter or digit right after the number ("147.136.1",
# "300gg.99") is stuck on too: no section is written that way.
_DOT_STUCK_ON = re.compile(r"\.[0-9A-Za-z]")
# A second section listed after the first ("147.136, 147.999",
# "147.136 and 147.140", "300gg-19, 300gg-22"): a list is not looked up, so
# no section in it goes unchecked or unlabelled.
# A bare number counts too ("Parts 147 and 999", "1395, 1396"), unless it
# starts another citation ("and 29 CFR ...").
_LISTED_SECTION = re.compile(
    r"\s{0,2}(?:,\s{0,3}(?:(?:and|or|&)\s{1,3})?|;\s{0,3}|\s(?:and|or|&)\s{1,3})"
    r"(?:§{1,2}\s{0,2})?\d{1,6}"
    r"(?:\.\d{1,5}|[a-z]{1,3}(?:[" + _DASH + r"]\d{1,5})?)?"
    r"(?![\w.])(?!\s{0,3}(?:C\.?\s{0,2}F\.?\s{0,2}R\b|U\.?\s{0,2}S\.?\s{0,2}C\b))",
    re.IGNORECASE,
)
# A plural keyword says more than one part or section is cited.
_PLURAL_KEYWORD = re.compile(r"\b(?:Parts|Sections|Secs)\b|§§", re.IGNORECASE)


def _stuck_or_listed(text: str, start: int, end: int) -> bool:
    return bool(
        _DOT_STUCK_ON.match(text, end)
        or _LISTED_SECTION.match(text, end)
        or _PLURAL_KEYWORD.search(text, start, end)
    )


_CFR_SECTION_VALID = re.compile(r"^\d{1,5}\.\d{1,5}(?:-\d{1,5}[A-Z]?)?$")
# CFR has 50 titles; the U.S. Code 54.
_CFR_TITLES = 50
_USC_TITLES = 54
# Cheap test for "this string cites something", used to label citations
# when the check itself fails.
_LOOKS_LIKE_REFERENCE = re.compile(
    r"10\.\d{4,9}/|\bPMID\b|pubmed\.ncbi|C\.?\s{0,2}F\.?\s{0,2}R\b|U\.?\s{0,2}S\.?\s{0,2}C\b",
    re.IGNORECASE,
)
_ALWAYS_TRAILING = ".,;:'\"*_‘’“”"
# What _trim_doi may take off that a registered DOI could also end in.
_TRIMMED_DOI_ENDINGS = frozenset(".;:_)")
# The record-level error esummary gives for an ID that is not a PubMed
# record. Any other error says nothing about the ID.
_NCBI_NOT_FOUND = "cannot get document summary"


def _dash(text: str) -> str:
    """Hyphens and dashes of every kind as "-"."""
    for d in _DASH[1:]:
        text = text.replace(d, "-")
    return text


def valid_doi(doi: str) -> bool:
    """Whether ``doi`` is of a shape that may be sent: the registry's
    recommended characters and length, no "..", no empty, "." or ".."
    path segment."""
    if not _DOI_VALID.match(doi) or ".." in doi:
        return False
    segments = doi.split("/")[1:]
    return all(seg not in ("", ".", "..") for seg in segments)


def _trim_doi(candidate: str, in_url: bool = False) -> str:
    """Drop what a URL or a sentence leaves on a DOI: in a doi.org link, a
    "?key=value" query or "#fragment"; always, trailing punctuation, markdown
    emphasis and quotes, and a ")" only while the DOI has more ")" than "(",
    since DOIs such as 10.1016/S0140-6736(20)30183-5 hold balanced
    parentheses. Outside a link a "?" or "#" stays, and makes the DOI one
    that is not sent."""
    query = _DOI_QUERY.search(candidate) if in_url else None
    if query:
        candidate = candidate[: query.start()]
    while candidate:
        if candidate[-1] in _ALWAYS_TRAILING:
            candidate = candidate[:-1]
        elif candidate[-1] == ")" and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        else:
            break
    return candidate


def find_occurrences(citation: str) -> List[Occurrence]:
    """Every DOI, PubMed ID, CFR and U.S. Code reference in a citation (in
    its first MAX_SCAN_CHARS), with its span. Those that may not be sent are
    marked malformed."""
    text = citation[:MAX_SCAN_CHARS]
    cut_short = len(citation) > MAX_SCAN_CHARS
    found: List[Occurrence] = []

    def add(
        kind: str,
        value: str,
        start: int,
        end: int,
        malformed: bool,
        raw_end: Optional[int] = None,
    ) -> None:
        # A match in the last word before the scan cap (nothing but
        # non-space characters between it and the cap) may be the front of a
        # longer identifier; querying it could call a real one missing.
        if cut_short and not any(
            c.isspace() for c in text[raw_end if raw_end is not None else end :]
        ):
            malformed = True
        found.append(Occurrence(Identifier(kind, value, malformed), start, end))

    for m in _DOI_CANDIDATE.finditer(text):
        in_url = text[max(0, m.start() - 8) : m.start()].lower().endswith("doi.org/")
        doi = _trim_doi(m.group(0), in_url)
        start, end = m.start(), m.start() + len(doi)
        prefix = _DOI_PREFIX.search(text, max(0, start - 30), start)
        if prefix and prefix.end() == start:
            start = prefix.start()
        # Stopping at "<" may mean the DOI was cut short; never send it.
        truncated = text[m.end() : m.end() + 1] == "<"
        if valid_doi(doi) and not truncated:
            add("doi", doi.lower(), start, end, False, m.end())
        else:
            add("doi", doi.lower()[:120], start, end, True, m.end())
    doi_spans = [(o.start, o.end) for o in found if o.ident.kind == "doi"]
    for m in _DOI_MENTION.finditer(text):
        token = m.group(1).lstrip("*_[(<\"'‘“")
        if not token or _NO_DOI.match(token.rstrip(_ALWAYS_TRAILING)):
            continue
        # Skipped only when a DOI read above covers what follows "DOI:"; a
        # mention nothing was read from ("DOI: 10.1056/", a doi.org link to
        # something that is not a DOI) is an identifier that is not sent.
        if any(s < m.end(1) and m.start(1) < e for s, e in doi_spans):
            continue
        add("doi", _trim_doi(token).lower()[:120], m.start(), m.end(), True)
    for m in _PMID_TEXT.finditer(text):
        token = m.group(1).rstrip(_ALWAYS_TRAILING)
        end = m.start(1) + len(token)
        add("pmid", token[:20], m.start(), end, not _PMID_VALID.match(token), m.end())
    for m in _PUBMED_URL.finditer(text):
        token = m.group(1).rstrip(_ALWAYS_TRAILING)
        add("pmid", token[:20], m.start(), m.end(), not _PMID_VALID.match(token))
    for m in _CFR_TEXT.finditer(text):
        title, part, sub = m.group("title"), m.group("part"), m.group("sub")
        dash, tail = m.group("dash"), m.group("tail")
        stuck = m.groups()[-1]
        section = f"{part}.{sub}" if sub else ""
        if tail:
            section += f"-{tail}"
        # A range of sections is never looked up: checking one end could
        # call a real range missing after a misparse. Ranges are "§§", a
        # second section after a dash or "through"/"to", or an abbreviated
        # one: a typographic dash, or a hyphen between two plain numbers
        # ("438.400-424", "438.2-10"). The one hyphenated form taken as a
        # section is the Title 29 style, three or more decimal digits and a
        # suffix of one or two ("2560.503-1"), or a suffix with a letter
        # ("2590.715-2719A"). When in doubt it is a range, so it is not
        # looked up.
        section_style = (
            tail is not None
            and sub is not None
            and (not tail.isdigit() or (len(sub) >= 3 and len(tail) <= 2))
        )
        abbreviated = bool(tail) and (dash != "-" or not section_style)
        is_range = bool(m.group("range")) or m.group("signs") == "§§" or abbreviated
        bad = (
            bool(stuck)
            or is_range
            or _stuck_or_listed(text, m.start(), m.end())
            or not (1 <= int(title) <= _CFR_TITLES)
        )
        value = f"{int(title)}/{int(part)}/{section}"
        add("cfr", value, m.start(), m.end(), bad)
    for m in _USC_TEXT.finditer(text):
        title, section, stuck = m.group(1), m.group(2), m.group(4)
        bad = (
            bool(stuck)
            or _stuck_or_listed(text, m.start(), m.end())
            or not (1 <= int(title) <= _USC_TITLES)
        )
        add("usc", f"{int(title)}/{_dash(section.lower())}", m.start(), m.end(), bad)
    return found


def find_identifiers(citation: str) -> List[Identifier]:
    """The distinct identifiers in a citation, in order of appearance."""
    out: List[Identifier] = []
    for occ in sorted(find_occurrences(citation), key=lambda o: o.start):
        if occ.ident not in out:
            out.append(occ.ident)
    return out


# The most guard text read. Past it, no DOI is sent at all, since the unread
# rest could overlap any of them.
MAX_GUARD_CHARS = 200_000


@dataclass(frozen=True)
class _Guard:
    """The four-character runs of the person's text, built once per check."""

    grams: FrozenSet[str]
    blocks_all: bool = False

    @classmethod
    def of(cls, text: str) -> "_Guard":
        if len(text) > MAX_GUARD_CHARS:
            return cls(frozenset(), blocks_all=True)
        lowered = text.lower()
        return cls(frozenset(lowered[i : i + 4] for i in range(len(lowered) - 3)))


_NO_GUARD = _Guard(frozenset())


def _overlaps(doi: str, guard: _Guard) -> bool:
    """Whether any four characters in a row of the DOI's suffix appear in
    the guard's text."""
    if guard.blocks_all:
        return True
    if not guard.grams:
        return False
    suffix = doi.split("/", 1)[1].lower() if "/" in doi else doi.lower()
    return any(suffix[i : i + 4] in guard.grams for i in range(len(suffix) - 3))


def _sendable(ident: Identifier, guard: _Guard = _NO_GUARD) -> bool:
    """A last check, right before a request, that only an identifier of the
    expected shape, and no DOI overlapping the person's data, goes out."""
    if ident.malformed:
        return False
    if ident.kind == "doi":
        return valid_doi(ident.value) and not _overlaps(ident.value, guard)
    if ident.kind == "pmid":
        return bool(_PMID_VALID.match(ident.value))
    if ident.kind == "cfr":
        parts = ident.value.split("/")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return False
        return not parts[2] or bool(_CFR_SECTION_VALID.match(parts[2]))
    return False


class UntracedClient:
    """GET requests sent straight through an httpx transport.

    Not through ``httpx.AsyncClient.send``: the error tracker's httpx
    integration wraps that method, recording the full URL (the identifier,
    and NCBI's key and IDs in the query) on a tracing span and adding
    tracing headers to the request. A transport sends exactly what is built
    here, follows no redirect, and logs nothing. Like an httpx client, it
    honours the HTTPS_PROXY / NO_PROXY environment, which a bare transport
    would not.
    """

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._fixed = transport
        self._transports: Dict[Optional[str], httpx.AsyncBaseTransport] = {}
        self._timeout = timeout

    def _transport_for(self, url: str) -> httpx.AsyncBaseTransport:
        if self._fixed is not None:
            return self._fixed
        target = httpx.URL(url)
        mounts = sorted(
            (URLPattern(pattern), proxy)
            for pattern, proxy in get_environment_proxies().items()
        )
        proxy = next(
            (proxy for pattern, proxy in mounts if pattern.matches(target)), None
        )
        if proxy not in self._transports:
            self._transports[proxy] = httpx.AsyncHTTPTransport(retries=0, proxy=proxy)
        return self._transports[proxy]

    async def get(
        self, url: str, params: Optional[Dict[str, str]] = None
    ) -> httpx.Response:
        request = httpx.Request(
            "GET",
            httpx.URL(url, params=params),
            headers={"Accept": "application/json"},
            extensions={
                "timeout": {
                    "connect": min(2.0, self._timeout),
                    "read": self._timeout,
                    "write": self._timeout,
                    "pool": self._timeout,
                }
            },
        )
        transport = self._transport_for(url)
        response = await transport.handle_async_request(request)
        try:
            await response.aread()
        finally:
            await response.aclose()
        return response

    async def aclose(self) -> None:
        # Concurrently, so one transport that hangs on close doesn't keep the
        # others open; the caller bounds the whole close.
        await asyncio.gather(
            *(
                transport.aclose()
                for transport in [self._fixed, *self._transports.values()]
                if transport is not None
            ),
            return_exceptions=True,
        )

    async def __aenter__(self) -> "UntracedClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _new_client() -> UntracedClient:
    return UntracedClient()


async def _cache_get(ident: Identifier) -> Optional[str]:
    try:
        value = await cache.aget(f"{_CACHE_PREFIX}{ident.kind}:{ident.value}")
    except Exception:
        return None
    if value == FOUND:
        return FOUND
    if value == MISSING:
        return MISSING
    return None


async def _cache_set(ident: Identifier, status: str) -> None:
    if status not in (FOUND, MISSING):
        return
    if ident.kind == "cfr":
        timeout = (
            _CFR_CACHE_FOUND_SECONDS if status == FOUND else _CFR_CACHE_MISSING_SECONDS
        )
    else:
        timeout = _CACHE_FOUND_SECONDS if status == FOUND else _CACHE_MISSING_SECONDS
    try:
        await cache.aset(
            f"{_CACHE_PREFIX}{ident.kind}:{ident.value}", status, timeout=timeout
        )
    except Exception:
        pass


def _json_body(resp: httpx.Response) -> Optional[object]:
    """The parsed body of a JSON answer, or None for anything else: a
    redirect, an HTML page (a bot check or challenge), a refusal or server
    error, or a body that does not parse."""
    if resp.is_redirect or resp.status_code not in (200, 404):
        return None
    content_type = resp.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return None
    try:
        body: object = resp.json()
    except Exception:
        return None
    return body


async def _check_doi(client: UntracedClient, doi: str) -> str:
    """FOUND when the handle service resolves this DOI (200, responseCode 1
    for the same handle), MISSING when it says there is no such handle (404,
    responseCode 100), UNCHECKED for any other answer. The DOI is sent as one
    fully encoded path segment."""
    try:
        resp = await client.get(_DOI_HANDLE_URL + quote(doi, safe=""))
    except Exception:
        return UNCHECKED
    data = _json_body(resp)
    if not isinstance(data, dict):
        return UNCHECKED
    code = data.get("responseCode")
    handle = str(data.get("handle", "")).lower()
    if code == 1 and resp.status_code == 200 and handle == doi.lower():
        return FOUND
    if code == 100 and resp.status_code == 404:
        return MISSING
    return UNCHECKED


async def _check_pmids(client: UntracedClient, pmids: List[str]) -> Dict[str, str]:
    """One esummary call for all the PubMed IDs. A record whose error is
    NCBI's "cannot get document summary" is MISSING; a record with a title
    or journal is FOUND; anything else is UNCHECKED."""
    from fighthealthinsurance.pubmed_tools import _EUTILS_PARAMS

    out = {p: UNCHECKED for p in pmids}
    try:
        resp = await client.get(
            _ESUMMARY_URL,
            params={
                **_EUTILS_PARAMS,
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "json",
            },
        )
    except Exception:
        return out
    data = _json_body(resp)
    if resp.status_code != 200 or not isinstance(data, dict):
        return out
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("uids"), list):
        return out
    for pmid in pmids:
        record = result.get(pmid)
        if not isinstance(record, dict) or record.get("uid") != pmid:
            continue
        error = record.get("error")
        if error:
            if isinstance(error, str) and error.strip().lower() == _NCBI_NOT_FOUND:
                out[pmid] = MISSING
        elif record.get("title") or record.get("source"):
            # A real summary carries the article's title and journal.
            out[pmid] = FOUND
    return out


def _structure_nodes(node: object):
    """Every node of an eCFR structure tree, depth first."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            children = current.get("children")
            if isinstance(children, list):
                stack.extend(children)


async def _check_cfr(client: UntracedClient, value: str) -> str:
    """Look the part up in eCFR's versioner API (the structure of
    ``title-N`` for ``part=P`` on the current date).

    * 200 with the title's structure, holding the part and its sections:
      FOUND for a part, and for a section the part lists; MISSING for a
      section it does not list (the part's structure is the full list of its
      sections). A part listing no sections is UNCHECKED.
    * 404 with eCFR's answer for a part that does not exist, an empty JSON
      object: MISSING.
    * Anything else (another 404 body, a redirect, a non-JSON page such as a
      bot check, JSON of another shape, a refusal or server error):
      UNCHECKED.
    """
    title, part, section = value.split("/", 2)
    try:
        resp = await client.get(
            f"{_ECFR_STRUCTURE_URL}title-{int(title)}.json",
            params={"part": str(int(part))},
        )
    except Exception:
        return UNCHECKED
    data = _json_body(resp)
    if data is None:
        return UNCHECKED
    if resp.status_code == 404:
        return MISSING if data == {} else UNCHECKED
    if not isinstance(data, dict):
        return UNCHECKED
    if data.get("type") != "title" or str(data.get("identifier")) != title:
        return UNCHECKED
    part_node = next(
        (
            n
            for n in _structure_nodes(data)
            if n.get("type") == "part" and str(n.get("identifier")) == part
        ),
        None,
    )
    if part_node is None:
        return UNCHECKED
    sections = [
        str(n.get("identifier"))
        for n in _structure_nodes(part_node)
        if n.get("type") == "section"
    ]
    if not sections:
        return UNCHECKED
    if not section:
        return FOUND
    return FOUND if section in sections else MISSING


def _settle(tasks: Iterable["asyncio.Task[None]"]) -> None:
    """Cancel what is still running and retrieve every task's outcome, so no
    exception is left unretrieved for the event loop to report."""

    def retrieve(task: "asyncio.Task[None]") -> None:
        if not task.cancelled():
            task.exception()

    for task in tasks:
        if task.done():
            retrieve(task)
        else:
            task.cancel()
            task.add_done_callback(retrieve)


async def check_identifiers(
    identifiers: Iterable[Identifier],
    budget_seconds: float = CHECK_BUDGET_SECONDS,
    client: Optional[UntracedClient] = None,
    may_send: Optional[Callable[[], Awaitable[bool]]] = None,
    guard_text: str = "",
    deadline: Optional[float] = None,
) -> Dict[Tuple[str, str, bool], str]:
    """Status for each identifier's key: FOUND, MISSING, UNCHECKED or
    FORM_ONLY. Never raises, and the whole of it (cache reads, consent checks
    and requests) runs inside one deadline: ``deadline`` (the event loop's
    clock) when given, else ``budget_seconds`` from now.

    ``may_send`` is awaited before any cached verdict is read, again right
    before the first request, and again before a later one once
    CONSENT_RECHECK_SECONDS have passed; anything but True (a failure or a
    timeout included) stops everything from then on, cached verdicts
    included. Without it nothing is read or sent at all.
    """
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + budget_seconds

    def remaining() -> float:
        return max(0.0, cast(float, deadline) - loop.time())

    statuses: Dict[Tuple[str, str, bool], str] = {}
    checkable: List[Identifier] = []
    built: Optional[_Guard] = None
    considered = 0
    for ident in identifiers:
        if ident.key in statuses:
            continue
        if ident.kind == "usc" and not ident.malformed:
            statuses[ident.key] = FORM_ONLY
            continue
        statuses[ident.key] = UNCHECKED
        # Every identifier weighed counts toward the limit, those the guard
        # holds back included, and none is weighed past the deadline.
        if ident.malformed or considered >= MAX_LOOKUPS or remaining() <= 0:
            continue
        considered += 1
        if ident.kind == "doi" and built is None:
            built = _Guard.of(guard_text)
        if _sendable(ident, built or _NO_GUARD):
            checkable.append(ident)
    guard = built or _NO_GUARD
    if not checkable or may_send is None:
        return statuses

    consent = {"ok": None, "at": 0.0}
    consent_lock = asyncio.Lock()

    async def consent_allows(fresh: bool = False) -> bool:
        async with consent_lock:
            if consent["ok"] is False:
                return False
            if (
                not fresh
                and consent["ok"] is True
                and loop.time() - consent["at"] < CONSENT_RECHECK_SECONDS
            ):
                return True
            try:
                allowed = await asyncio.wait_for(
                    cast(Callable[[], Awaitable[bool]], may_send)(),
                    timeout=remaining(),
                )
            except Exception as e:
                logger.debug(
                    f"Citation checks: consent check failed ({type(e).__name__})"
                )
                allowed = False
            consent["ok"] = allowed is True
            consent["at"] = loop.time()
            return allowed is True

    # Consent comes first: without it, no cached verdict is applied either,
    # so every identifier stays unchecked and its citation is labelled.
    if not await consent_allows():
        return statuses

    async def read_cache(ident: Identifier) -> None:
        cached = await _cache_get(ident)
        if cached is not None:
            statuses[ident.key] = cached

    reads = [asyncio.create_task(read_cache(i)) for i in checkable]
    if remaining() > 0:
        await asyncio.wait(reads, timeout=remaining())
    _settle(reads)
    to_look_up = [i for i in checkable if statuses[i.key] == UNCHECKED]
    if not to_look_up or remaining() <= 0:
        return statuses

    # A fresh read right before the first request: the early answer is for
    # the cache, and consent could have been withdrawn while it was read.
    if not await consent_allows(fresh=True):
        # Refused after the cache was read: no cached verdict counts either,
        # so every identifier is unchecked and its citation labelled.
        for ident in checkable:
            statuses[ident.key] = UNCHECKED
        return statuses

    own_client = client is None
    http = client if client is not None else _new_client()
    gate = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)

    async def one(ident: Identifier) -> None:
        async with gate:
            if not _sendable(ident, guard) or not await consent_allows():
                return
            if ident.kind == "doi":
                status = await _check_doi(http, ident.value)
            else:
                status = await _check_cfr(http, ident.value)
        statuses[ident.key] = status
        await _cache_set(ident, status)

    async def pmids(group: List[Identifier]) -> None:
        async with gate:
            if not await consent_allows():
                return
            found = await _check_pmids(http, [i.value for i in group])
        for ident in group:
            status = found.get(ident.value, UNCHECKED)
            statuses[ident.key] = status
            await _cache_set(ident, status)

    tasks = [asyncio.create_task(one(i)) for i in to_look_up if i.kind != "pmid"]
    pmid_group = [i for i in to_look_up if i.kind == "pmid"]
    if pmid_group:
        tasks.append(asyncio.create_task(pmids(pmid_group)))
    try:
        await asyncio.wait(tasks, timeout=remaining())
    except Exception as e:
        logger.debug(f"Citation checks stopped early ({type(e).__name__})")
    finally:
        _settle(tasks)
        if own_client:
            try:
                await asyncio.wait_for(http.aclose(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
    return statuses


def _cut(citation: str, spans: List[Tuple[int, int]]) -> str:
    """``citation`` with each span replaced by REMOVED_REFERENCE, working
    from the end so earlier spans keep their positions. Overlapping spans
    are merged first."""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        citation = citation[:start] + REMOVED_REFERENCE + citation[end:]
    return citation


def _label(citation: str) -> str:
    if citation.startswith(UNVERIFIED_LABEL):
        return citation
    return UNVERIFIED_LABEL + citation


def _apply(
    citation: str,
    occurrences: Sequence[Occurrence],
    statuses: Dict[Tuple[str, str, bool], str],
) -> Optional[str]:
    """The citation as it should reach the letter request, or None to drop
    it (see the module docstring). Only a registry's MISSING removes
    anything."""

    def trimmed(o: Occurrence) -> bool:
        # A character taken off the end of a DOI is usually sentence
        # punctuation or markup, but a registered DOI can end in any of
        # these. A trimmed DOI the registry calls missing is labelled rather
        # than dropped.
        return o.ident.kind == "doi" and citation[o.end : o.end + 1] in (
            _TRIMMED_DOI_ENDINGS
        )

    missing = [
        o
        for o in occurrences
        if statuses.get(o.ident.key) == MISSING and not trimmed(o)
    ]
    unchecked = len(citation) > MAX_SCAN_CHARS or any(
        statuses.get(o.ident.key, UNCHECKED) == UNCHECKED
        or (statuses.get(o.ident.key) == MISSING and trimmed(o))
        for o in occurrences
    )
    if missing:
        if len(citation) <= SHORT_CITATION_CHARS:
            return None
        citation = _cut(citation, [(o.start, o.end) for o in missing])
    return _label(citation) if unchecked else citation


def _label_if_reference(citation: object) -> object:
    if isinstance(citation, str) and _LOOKS_LIKE_REFERENCE.search(
        citation[:MAX_SCAN_CHARS]
    ):
        return _label(citation)
    return citation


async def verify_citations(
    citations: List[str],
    budget_seconds: float = CHECK_BUDGET_SECONDS,
    client: Optional[UntracedClient] = None,
    may_send: Optional[Callable[[], Awaitable[bool]]] = None,
    guard_text: str = "",
) -> List[str]:
    """``citations`` with missing identifiers dropped or cut out and the
    rest of the unverified ones labelled, in the same order. Strings with no
    identifier pass through untouched. Never raises: on any unexpected error
    every citation that looks like a reference comes back labelled
    unverified."""
    if not citations or not checks_enabled():
        return citations
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_seconds
        found: List[Tuple[object, List[Occurrence]]] = []
        for c in citations:
            # Parsing counts against the deadline: past it, the remaining
            # citations are only labelled.
            if isinstance(c, str) and loop.time() < deadline:
                found.append((c, find_occurrences(c)))
            else:
                found.append((c, []))
                if isinstance(c, str):
                    found[-1] = (_label_if_reference(c), [])
        statuses = await check_identifiers(
            [o.ident for _c, occs in found for o in occs],
            client=client,
            may_send=may_send,
            guard_text=guard_text,
            deadline=deadline,
        )
        out: List[str] = []
        dropped = 0
        for citation, occs in found:
            if not occs or not isinstance(citation, str):
                if isinstance(citation, str) and len(citation) > MAX_SCAN_CHARS:
                    # Part of it went unread, so nothing vouches for it.
                    citation = _label(citation)
                out.append(cast(str, citation))
                continue
            kept = _apply(citation, occs, statuses)
            if kept is None:
                dropped += 1
            else:
                out.append(kept)
        if dropped:
            logger.info(
                f"Citation checks dropped {dropped} of {len(citations)} citations "
                "whose identifier was not found"
            )
        return out
    except Exception as e:
        # Only the exception's type: its message or a traceback could carry
        # citation text.
        logger.warning(
            f"Citation checks failed ({type(e).__name__}); "
            "labelling the citations unverified"
        )
        try:
            return [cast(str, _label_if_reference(c)) for c in citations]
        except Exception:
            return [_label(c) if isinstance(c, str) else c for c in citations]
