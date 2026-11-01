"""Check the identifiers in citations a model wrote before they reach a letter.

The citation backends are asked for real, verifiable references, but nothing
checked that the DOIs, PubMed IDs and CFR sections they return exist. This
module finds those identifiers in each citation string, checks the form of
each one, and asks the public registry whether it exists:

* DOIs: the DOI handle service (doi.org/api/handles).
* PubMed IDs: NCBI E-utilities esummary, the service the app already uses.
* CFR sections: eCFR's versioner API, whose structure of a title's part
  lists every section in it, and which answers 404 for a part or title that
  does not exist.

U.S. Code sections are checked for form only: there is no simple public
lookup, and a well-known statute cited from memory is usually right.

Only the identifiers leave the app, never the citation text, the denial or
anything about the person, and only identifiers that pass a strict form check
are sent at all: a DOI's suffix is limited to the registry's recommended
characters and length (so a "DOI" a model filled with words, or a path like
``../``, is treated as malformed and never sent), a PubMed ID to digits, a CFR
reference to numbers. The caller decides, right before any request, whether
the person's consent allows the lookups (``may_send``); when it does not, no
request is made. Every check is time-boxed: a registry that is slow
or down never holds up a letter, and a citation it could not check is kept,
labelled as unverified, rather than dropped. The same goes for an answer of
an unexpected kind: a redirect (to a bot check, a login page, anywhere), a
page that is not the JSON the registry documents, or JSON of another shape.
Only an answer in the registry's documented form counts as found or missing.

What happens to a citation:

* An identifier that is malformed, or that its registry says does not exist:
  a short citation is dropped; in a long one (a backend's whole answer) the
  identifier is cut out and the rest kept.
* An identifier that could not be checked (timeout, error): the citation is
  kept with ``UNVERIFIED_LABEL`` in front, so the letter writer knows not to
  lean on it.
* Every identifier found: kept as it is.
* No identifier at all (a guideline title, a URL, a plain-text reference):
  kept as it is. There is nothing to look up.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    cast,
)
from urllib.parse import quote

import httpx
from django.core.cache import cache
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable

UNVERIFIED_LABEL = "Unverified, use only if you can confirm it: "

# Put in place of an identifier cut out of a long citation.
REMOVED_REFERENCE = "(unverified reference removed)"

# A citation at most this long is one reference, and goes when one of its
# identifiers is bad. Longer strings are a backend's whole answer, which
# usually holds several references; only the bad identifier is cut out.
SHORT_CITATION_CHARS = 500

# The whole check for one denial's citations, in seconds, cache reads and
# consent check included. What has not answered by then counts as unchecked.
CHECK_BUDGET_SECONDS = 6.0
# Each request to a registry, in seconds.
REQUEST_TIMEOUT_SECONDS = 3.0
# Identifiers looked up (cache or registry) per denial; the rest count as
# unchecked without either.
MAX_LOOKUPS = 25
# Requests in flight at once.
MAX_CONCURRENT_LOOKUPS = 5

# Registry answers are cached by identifier. Existence is stable; absence is
# kept shorter in case a new DOI or PubMed record is still being indexed.
# Only those two verdicts are cached, never an error or a refusal.
_CACHE_PREFIX = "citecheck:v2:"
_CACHE_FOUND_SECONDS = 30 * 24 * 3600
_CACHE_MISSING_SECONDS = 7 * 24 * 3600

_DOI_HANDLE_URL = "https://doi.org/api/handles/"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_ECFR_STRUCTURE_URL = "https://www.ecfr.gov/api/versioner/v1/structure/current/"

FOUND = "found"
MISSING = "missing"
MALFORMED = "malformed"
UNCHECKED = "unchecked"
# Well formed, and not looked up by design (U.S. Code).
FORM_ONLY = "form_only"


def checks_enabled() -> bool:
    """CITATION_CHECKS_ENABLED, on unless set to 0/false/no/off."""
    raw = (get_env_variable("CITATION_CHECKS_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Identifier:
    """One identifier found in a citation string."""

    kind: str  # "doi", "pmid", "cfr" or "usc"
    value: str  # normalized: DOI lowercased, PMID digits, "45/147/147.136"
    malformed: bool = False


@dataclass(frozen=True)
class Occurrence:
    """Where an identifier appears in a citation, for cutting it out."""

    ident: Identifier
    start: int
    end: int


# A DOI suffix may only hold the characters Crossref recommends, and at most
# this many of them. Anything else (words, "=", "&", "?", "%", "@", spaces,
# "..") makes the "DOI" malformed, so it is never sent.
_DOI_MAX_SUFFIX = 100
_DOI_VALID = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9\-._;()/:]{1,%d}$" % _DOI_MAX_SUFFIX)
# A DOI candidate: "10." with a registrant code and "/", up to whitespace,
# a quote, a bracket or a brace. Not preceded by a word character, a dot, a
# hyphen or a slash (other than doi.org's own), so "110.1234/x" or a
# version number is not one.
_DOI_CANDIDATE = re.compile(
    r"(?:(?<=doi\.org/)|(?<![\w.\-/]))10\.\d{4,9}/[^\s\"'<>\[\]{}|\\^`]+"
)
# Prefixes that belong to a DOI occurrence when cutting it out.
_DOI_PREFIX = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi\.org/|doi\s*:\s*)$", re.IGNORECASE
)
# Something introduced as a DOI that does not start like one.
_DOI_MENTION = re.compile(
    r"\b(?:https?://(?:dx\.)?)?doi(?:\.org/|\s*:\s*)(\S+)", re.IGNORECASE
)
_DOI_START = re.compile(r"10\.\d{4,9}/")
_PMID_VALID = re.compile(r"^[1-9]\d{0,8}$")
_PMID_TEXT = re.compile(r"\bPMID\s*[:#]?\s*([^\s,;\])]+)", re.IGNORECASE)
_PUBMED_URL = re.compile(
    r"(?:https?://)?(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/([^\s/?#\])\"'<>]+)/?",
    re.IGNORECASE,
)
# Subsections such as "(b)(1)(ii)" may follow a section. Anything else
# stuck to the number ("147.136oops", "300gg-???") makes it malformed rather
# than letting a valid-looking prefix through.
_SUBSECTIONS = r"((?:\([0-9A-Za-z]{1,4}\))*)"
_STUCK_ON = r"([^\s,;:.)\]]*)"
_CFR_TEXT = re.compile(
    r"(?<![\w.])(\d{1,3})\s*C\.?\s*F\.?\s*R\.?\s*(?:Parts?|Sec(?:tion)?s?\.?|§{1,2})?"
    r"\s*(\d{1,5})(?:\.(\d{1,5}(?:-\d{1,5}[A-Z]?)?))?" + _SUBSECTIONS + _STUCK_ON,
    re.IGNORECASE,
)
_USC_TEXT = re.compile(
    r"(?<![\w.])(\d{1,3})\s*U\.?\s*S\.?\s*C\.?\s*(?:Sec(?:tion)?s?\.?|§{1,2})?\s*"
    r"(\d{1,6}[a-z]{0,3}(?:-\d{1,5}[a-z]{0,2})?)" + _SUBSECTIONS + _STUCK_ON,
    re.IGNORECASE,
)
_CFR_SECTION_VALID = re.compile(r"^\d{1,5}\.\d{1,5}(?:-\d{1,5}[A-Z]?)?$")
# CFR has 50 titles; the U.S. Code 54.
_CFR_TITLES = 50
_USC_TITLES = 54
# Cheap test for "this string cites something", used to label citations
# when the check itself fails.
_LOOKS_LIKE_REFERENCE = re.compile(
    r"10\.\d{4,9}/|\bPMID\b|pubmed\.ncbi|C\.?\s*F\.?\s*R\b|U\.?\s*S\.?\s*C\b",
    re.IGNORECASE,
)
_ALWAYS_TRAILING = ".,;:'\""


def valid_doi(doi: str) -> bool:
    """Whether ``doi`` is safe and plausible to send: the registry's
    recommended characters and length, no "..", no empty, "." or ".."
    path segment."""
    if not _DOI_VALID.match(doi) or ".." in doi:
        return False
    segments = doi.split("/")[1:]
    return all(seg not in ("", ".", "..") for seg in segments)


def _trim_doi(candidate: str) -> str:
    """Drop the punctuation a sentence leaves on a DOI: always ". , ; : '",
    and a ")" only while the DOI has more ")" than "(", since DOIs such as
    10.1016/S0140-6736(20)30183-5 hold balanced parentheses."""
    while candidate:
        if candidate[-1] in _ALWAYS_TRAILING:
            candidate = candidate[:-1]
        elif candidate[-1] == ")" and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        else:
            break
    return candidate


def find_occurrences(citation: str) -> List[Occurrence]:
    """Every DOI, PubMed ID, CFR and U.S. Code reference in a citation, with
    its span. Those of a wrong form are marked malformed and are never
    sent anywhere."""
    found: List[Occurrence] = []

    def add(kind: str, value: str, start: int, end: int, malformed: bool) -> None:
        found.append(Occurrence(Identifier(kind, value, malformed), start, end))

    for m in _DOI_CANDIDATE.finditer(citation):
        doi = _trim_doi(m.group(0))
        start, end = m.start(), m.start() + len(doi)
        prefix = _DOI_PREFIX.search(citation, 0, start)
        if prefix and prefix.end() == start:
            start = prefix.start()
        if valid_doi(doi):
            add("doi", doi.lower(), start, end, False)
        else:
            add("doi", doi.lower()[:120], start, end, True)
    for m in _DOI_MENTION.finditer(citation):
        if _DOI_START.match(m.group(1)):
            continue  # a candidate above, valid or not
        add("doi", _trim_doi(m.group(1)).lower()[:120], m.start(), m.end(), True)
    for m in _PMID_TEXT.finditer(citation):
        token = m.group(1).rstrip(_ALWAYS_TRAILING)
        end = m.start(1) + len(token)
        add("pmid", token[:20], m.start(), end, not _PMID_VALID.match(token))
    for m in _PUBMED_URL.finditer(citation):
        token = m.group(1)
        add("pmid", token[:20], m.start(), m.end(), not _PMID_VALID.match(token))
    for m in _CFR_TEXT.finditer(citation):
        title, part, sub, stuck = m.group(1), m.group(2), m.group(3), m.group(5)
        section = f"{part}.{sub}" if sub else ""
        bad = bool(stuck) or not (1 <= int(title) <= _CFR_TITLES)
        value = f"{int(title)}/{int(part)}/{section}"
        add("cfr", value, m.start(), m.end(), bad)
    for m in _USC_TEXT.finditer(citation):
        title, section, stuck = m.group(1), m.group(2), m.group(4)
        bad = bool(stuck) or not (1 <= int(title) <= _USC_TITLES)
        add("usc", f"{int(title)}/{section.lower()}", m.start(), m.end(), bad)
    return found


def find_identifiers(citation: str) -> List[Identifier]:
    """The distinct identifiers in a citation, in order of appearance."""
    out: List[Identifier] = []
    for occ in sorted(find_occurrences(citation), key=lambda o: o.start):
        if occ.ident not in out:
            out.append(occ.ident)
    return out


def _sendable(ident: Identifier) -> bool:
    """A last check, right before a request, that only a well-formed
    identifier goes out."""
    if ident.malformed:
        return False
    if ident.kind == "doi":
        return valid_doi(ident.value)
    if ident.kind == "pmid":
        return bool(_PMID_VALID.match(ident.value))
    if ident.kind == "cfr":
        parts = ident.value.split("/")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return False
        return not parts[2] or bool(_CFR_SECTION_VALID.match(parts[2]))
    return False


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=2.0),
        follow_redirects=False,
        headers={"Accept": "application/json"},
    )


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
    error, or a body that does not parse. Redirects are never followed."""
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


async def _check_doi(client: httpx.AsyncClient, doi: str) -> str:
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


# The record-level errors esummary gives for an ID that is not a PubMed
# record. Any other error says nothing about the ID.
_NCBI_NOT_FOUND = re.compile(
    r"cannot get document summary|invalid uid|uid .*not found|not found",
    re.IGNORECASE,
)


async def _check_pmids(client: httpx.AsyncClient, pmids: List[str]) -> Dict[str, str]:
    """One esummary call for all the PubMed IDs. A record whose error says
    the ID is not found is MISSING; a record with a title or journal is
    FOUND; anything else is UNCHECKED."""
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
            if isinstance(error, str) and _NCBI_NOT_FOUND.search(error):
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


async def _check_cfr(client: httpx.AsyncClient, value: str) -> str:
    """Look the part up in eCFR's versioner API (the structure of
    ``title-N`` for ``part=P`` on the current date).

    * 200 with the title's structure, holding the part: FOUND for a part,
      and for a section that the part lists; MISSING for a section the part
      does not list (the part's structure is the full list of its sections).
    * 404 with a JSON body: the title or part does not exist, MISSING.
    * Anything else (a redirect, a non-JSON page such as a bot check, JSON of
      another shape, a refusal or server error): UNCHECKED.
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
        return MISSING
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
    if not section:
        return FOUND
    for n in _structure_nodes(part_node):
        if n.get("type") == "section" and str(n.get("identifier")) == section:
            return FOUND
    return MISSING


async def check_identifiers(
    identifiers: Iterable[Identifier],
    budget_seconds: float = CHECK_BUDGET_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
    may_send: Optional[Callable[[], Awaitable[bool]]] = None,
) -> Dict[Tuple[str, str], str]:
    """Status for each (kind, value): FOUND, MISSING, MALFORMED, UNCHECKED or
    FORM_ONLY. Never raises, and the whole of it (cache reads, the consent
    check and the requests) runs inside one ``budget_seconds`` deadline.

    ``may_send`` is awaited right before the first request; if it answers
    anything but True (or fails, or runs out of time), no request is made
    and what was not cached stays UNCHECKED. Without it, requests are
    allowed: the production caller always passes one.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds

    def remaining() -> float:
        return max(0.0, deadline - loop.time())

    statuses: Dict[Tuple[str, str], str] = {}
    checkable: List[Identifier] = []
    for ident in identifiers:
        key = (ident.kind, ident.value)
        if key in statuses:
            continue
        if ident.malformed:
            statuses[key] = MALFORMED
        elif ident.kind == "usc":
            statuses[key] = FORM_ONLY
        else:
            statuses[key] = UNCHECKED
            if len(checkable) < MAX_LOOKUPS and _sendable(ident):
                checkable.append(ident)
    if not checkable:
        return statuses

    async def read_cache(ident: Identifier) -> None:
        cached = await _cache_get(ident)
        if cached is not None:
            statuses[(ident.kind, ident.value)] = cached

    reads = [asyncio.create_task(read_cache(i)) for i in checkable]
    _done, pending = await asyncio.wait(reads, timeout=remaining())
    for task in pending:
        task.cancel()
    to_look_up = [i for i in checkable if statuses[(i.kind, i.value)] == UNCHECKED]
    if not to_look_up or remaining() <= 0:
        return statuses

    if may_send is not None:
        try:
            allowed = await asyncio.wait_for(may_send(), timeout=remaining())
        except Exception as e:
            logger.debug(f"Citation checks: consent check failed ({type(e).__name__})")
            allowed = False
        if allowed is not True:
            return statuses

    own_client = client is None
    http = client if client is not None else _new_client()
    gate = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)

    async def one(ident: Identifier) -> None:
        async with gate:
            if not _sendable(ident):
                return
            if ident.kind == "doi":
                status = await _check_doi(http, ident.value)
            else:
                status = await _check_cfr(http, ident.value)
        statuses[(ident.kind, ident.value)] = status
        await _cache_set(ident, status)

    async def pmids(group: List[Identifier]) -> None:
        async with gate:
            found = await _check_pmids(http, [i.value for i in group])
        for ident in group:
            status = found.get(ident.value, UNCHECKED)
            statuses[(ident.kind, ident.value)] = status
            await _cache_set(ident, status)

    tasks = [asyncio.create_task(one(i)) for i in to_look_up if i.kind != "pmid"]
    pmid_group = [i for i in to_look_up if i.kind == "pmid"]
    if pmid_group:
        tasks.append(asyncio.create_task(pmids(pmid_group)))
    try:
        _done, pending = await asyncio.wait(tasks, timeout=remaining())
        for task in pending:
            task.cancel()
    except Exception as e:
        logger.debug(f"Citation checks stopped early ({type(e).__name__})")
    finally:
        if own_client:
            try:
                await http.aclose()
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
    occurrences: List[Occurrence],
    statuses: Dict[Tuple[str, str], str],
) -> Optional[str]:
    """The citation as it should reach the letter request, or None to drop
    it (see the module docstring)."""

    def status(occ: Occurrence) -> Optional[str]:
        return statuses.get((occ.ident.kind, occ.ident.value))

    bad = [o for o in occurrences if status(o) in (MISSING, MALFORMED)]
    unchecked = any(status(o) == UNCHECKED for o in occurrences)
    if bad:
        if len(citation) <= SHORT_CITATION_CHARS:
            return None
        citation = _cut(citation, [(o.start, o.end) for o in bad])
    return _label(citation) if unchecked else citation


def _label_if_reference(citation: object) -> object:
    if isinstance(citation, str) and _LOOKS_LIKE_REFERENCE.search(citation):
        return _label(citation)
    return citation


async def verify_citations(
    citations: List[str],
    budget_seconds: float = CHECK_BUDGET_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
    may_send: Optional[Callable[[], Awaitable[bool]]] = None,
) -> List[str]:
    """``citations`` with bad identifiers dropped or cut out and unchecked
    ones labelled, in the same order. Strings with no identifier pass through
    untouched. Never raises: on any unexpected error every citation that
    looks like a reference comes back labelled unverified."""
    if not citations or not checks_enabled():
        return citations
    try:
        found = [
            (c, find_occurrences(c)) if isinstance(c, str) else (c, [])
            for c in citations
        ]
        statuses = await check_identifiers(
            [o.ident for _c, occs in found for o in occs],
            budget_seconds=budget_seconds,
            client=client,
            may_send=may_send,
        )
        out: List[str] = []
        dropped = 0
        for citation, occs in found:
            if not occs:
                out.append(citation)
                continue
            kept = _apply(citation, occs, statuses)
            if kept is None:
                dropped += 1
            else:
                out.append(kept)
        if dropped:
            logger.info(
                f"Citation checks dropped {dropped} of {len(citations)} citations "
                "with a malformed or missing identifier"
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
