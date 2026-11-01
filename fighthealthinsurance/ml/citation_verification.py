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
anything about the person. Every check is time-boxed: a registry that is slow
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
from typing import Dict, Iterable, List, Optional, Tuple
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

# The whole check for one denial's citations, in seconds. What has not
# answered by then counts as unchecked.
CHECK_BUDGET_SECONDS = 6.0
# Each request to a registry, in seconds.
REQUEST_TIMEOUT_SECONDS = 3.0
# Identifiers looked up per denial; the rest count as unchecked.
MAX_LOOKUPS = 25
# Requests in flight at once.
MAX_CONCURRENT_LOOKUPS = 5

# Registry answers are cached by identifier. Existence is stable; absence is
# kept shorter in case a new DOI or PubMed record is still being indexed.
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
    text: str  # the matched text, for cutting it out
    malformed: bool = False


_TRAILING_PUNCT = ".,;:)]}>'\""

# A DOI: "10.", a registrant code, "/", and a suffix.
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")
# Something introduced as a DOI, valid or not.
_DOI_MENTION_RE = re.compile(r"\bdoi(?:\.org/|\s*:\s*|\s+)(\S+)", re.IGNORECASE)
_PMID_RE = re.compile(r"\bPMID\s*[:#]?\s*([^\s,;)\]]+)", re.IGNORECASE)
_PUBMED_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE)
_CFR_RE = re.compile(
    r"\b(\d{1,3})\s*C\.?\s*F\.?\s*R\.?\s*(?:Part|Parts|Sec(?:tion)?\.?|§+)?\s*"
    r"(\d{1,5})(?:\.(\d{1,5}(?:-\d{1,3})?))?",
    re.IGNORECASE,
)
_USC_RE = re.compile(
    r"\b(\d{1,3})\s*U\.?\s*S\.?\s*C\.?\s*(?:Sec(?:tion)?\.?|§+)?\s*(\d{1,6}[a-z]{0,3})",
    re.IGNORECASE,
)
_PMID_FORM = re.compile(r"^[1-9]\d{0,8}$")
_DOI_FORM = re.compile(r"^10\.\d{4,9}/\S+$")
# CFR has 50 titles; the U.S. Code 54.
_CFR_TITLES = 50
_USC_TITLES = 54


def _clean(value: str) -> str:
    return value.rstrip(_TRAILING_PUNCT)


def find_identifiers(citation: str) -> List[Identifier]:
    """The DOIs, PubMed IDs, CFR and U.S. Code references in one citation,
    each once, with those of a wrong form marked malformed."""
    found: List[Identifier] = []
    seen: set = set()

    def add(ident: Identifier) -> None:
        key = (ident.kind, ident.value, ident.malformed)
        if key not in seen:
            seen.add(key)
            found.append(ident)

    for m in _DOI_RE.finditer(citation):
        doi = _clean(m.group(0))
        add(Identifier("doi", doi.lower(), doi))
    for m in _DOI_MENTION_RE.finditer(citation):
        token = _clean(m.group(1))
        if token.lower().startswith("https://doi.org/"):
            token = token[len("https://doi.org/") :]
        if not _DOI_FORM.match(token):
            add(Identifier("doi", token.lower(), m.group(0), malformed=True))
    for m in _PMID_RE.finditer(citation):
        token = _clean(m.group(1))
        if _PMID_FORM.match(token):
            add(Identifier("pmid", token, m.group(0)))
        else:
            add(Identifier("pmid", token, m.group(0), malformed=True))
    for m in _PUBMED_URL_RE.finditer(citation):
        token = m.group(1)
        add(
            Identifier("pmid", token, m.group(0), malformed=not _PMID_FORM.match(token))
        )
    for m in _CFR_RE.finditer(citation):
        title, part, sub = m.group(1), m.group(2), m.group(3)
        bad = not (1 <= int(title) <= _CFR_TITLES)
        section = f"{part}.{sub}" if sub else ""
        add(
            Identifier(
                "cfr", f"{int(title)}/{int(part)}/{section}", m.group(0), malformed=bad
            )
        )
    for m in _USC_RE.finditer(citation):
        title, section = m.group(1), m.group(2)
        bad = not (1 <= int(title) <= _USC_TITLES)
        add(
            Identifier(
                "usc", f"{int(title)}/{section.lower()}", m.group(0), malformed=bad
            )
        )
    return found


async def _cache_get(ident: Identifier) -> Optional[str]:
    try:
        value = await cache.aget(f"{_CACHE_PREFIX}{ident.kind}:{ident.value}")
    except Exception:
        return None
    return value if isinstance(value, str) else None


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
    redirect, an HTML page (a bot check or challenge), or a body that does
    not parse. Redirects are never followed."""
    if resp.is_redirect or not (200 <= resp.status_code < 500):
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
    """FOUND when the handle service resolves this DOI (responseCode 1 for
    the same handle), MISSING when it says there is no such handle
    (responseCode 100, with a 404), UNCHECKED for any other answer."""
    try:
        resp = await client.get(_DOI_HANDLE_URL + quote(doi, safe="/"))
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


async def _check_pmids(client: httpx.AsyncClient, pmids: List[str]) -> Dict[str, str]:
    """One esummary call for all the PubMed IDs. A record with an "error"
    (NCBI's "cannot get document summary") is MISSING."""
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
        if record.get("error"):
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
      another shape, a server error): UNCHECKED.
    """
    title, part, section = value.split("/", 2)
    try:
        resp = await client.get(
            f"{_ECFR_STRUCTURE_URL}title-{title}.json", params={"part": part}
        )
    except Exception:
        return UNCHECKED
    data = _json_body(resp)
    if data is None:
        return UNCHECKED
    if resp.status_code == 404:
        return MISSING
    if resp.status_code != 200 or not isinstance(data, dict):
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
) -> Dict[Tuple[str, str], str]:
    """Status for each (kind, value): FOUND, MISSING, MALFORMED, UNCHECKED or
    FORM_ONLY. Never raises and never runs past ``budget_seconds``."""
    statuses: Dict[Tuple[str, str], str] = {}
    to_look_up: List[Identifier] = []
    for ident in identifiers:
        key = (ident.kind, ident.value)
        if key in statuses:
            continue
        if ident.malformed:
            statuses[key] = MALFORMED
        elif ident.kind == "usc":
            statuses[key] = FORM_ONLY
        else:
            cached = await _cache_get(ident)
            if cached in (FOUND, MISSING):
                statuses[key] = cached
            elif len(to_look_up) < MAX_LOOKUPS:
                statuses[key] = UNCHECKED
                to_look_up.append(ident)
            else:
                statuses[key] = UNCHECKED
    if not to_look_up:
        return statuses

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=2.0),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
    gate = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)

    async def one(ident: Identifier) -> None:
        async with gate:
            if ident.kind == "doi":
                status = await _check_doi(client, ident.value)
            else:
                status = await _check_cfr(client, ident.value)
        statuses[(ident.kind, ident.value)] = status
        await _cache_set(ident, status)

    async def pmids(group: List[Identifier]) -> None:
        async with gate:
            found = await _check_pmids(client, [i.value for i in group])
        for ident in group:
            status = found.get(ident.value, UNCHECKED)
            statuses[(ident.kind, ident.value)] = status
            await _cache_set(ident, status)

    tasks = [asyncio.create_task(one(i)) for i in to_look_up if i.kind != "pmid"]
    pmid_group = [i for i in to_look_up if i.kind == "pmid"]
    if pmid_group:
        tasks.append(asyncio.create_task(pmids(pmid_group)))
    try:
        _done, pending = await asyncio.wait(tasks, timeout=budget_seconds)
        for task in pending:
            task.cancel()
    except Exception as e:
        logger.opt(exception=True).debug(f"Citation checks stopped early: {e}")
    finally:
        if own_client:
            try:
                await client.aclose()
            except Exception:
                pass
    return statuses


def _apply(citation: str, idents: List[Identifier], statuses) -> Optional[str]:
    """The citation as it should reach the letter request, or None to drop
    it (see the module docstring)."""
    bad = [i for i in idents if statuses.get((i.kind, i.value)) in (MISSING, MALFORMED)]
    if bad:
        if len(citation) <= SHORT_CITATION_CHARS:
            return None
        for ident in bad:
            citation = citation.replace(ident.text, REMOVED_REFERENCE)
        return citation
    if any(statuses.get((i.kind, i.value)) == UNCHECKED for i in idents):
        if citation.startswith(UNVERIFIED_LABEL):
            return citation
        return UNVERIFIED_LABEL + citation
    return citation


async def verify_citations(
    citations: List[str],
    budget_seconds: float = CHECK_BUDGET_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> List[str]:
    """``citations`` with bad identifiers dropped or cut out and unchecked
    ones labelled, in the same order. Strings with no identifier pass through
    untouched. Never raises: on any unexpected error the citations come back
    as they were, which is what the app did before these checks."""
    if not citations or not checks_enabled():
        return citations
    try:
        found = [
            (c, find_identifiers(c)) if isinstance(c, str) else (c, [])
            for c in citations
        ]
        statuses = await check_identifiers(
            [i for _c, idents in found for i in idents],
            budget_seconds=budget_seconds,
            client=client,
        )
        out: List[str] = []
        dropped = 0
        for citation, idents in found:
            if not idents:
                out.append(citation)
                continue
            kept = _apply(citation, idents, statuses)
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
        logger.opt(exception=True).warning(f"Citation checks failed: {e}")
        return citations
