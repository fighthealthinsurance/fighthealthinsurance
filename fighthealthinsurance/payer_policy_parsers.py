"""
Parsers for payer medical-policy index pages.

Each parser takes the raw HTML of a payer's static-index URL and returns a
list of ``ParsedPolicyEntry`` records (title, absolute url, optional payer-
internal id). The orchestrator (``payer_policy_fetcher``) writes these as
``PayerPolicyEntry`` rows that ``payer_policy_helper`` later keyword-matches
against the patient's procedure/diagnosis at appeal time.

Adding support for a new payer means:
  1. Verify the payer's index URL is fetchable static HTML/PDF (no JS).
  2. Set ``medical_policy_url_is_static_index=True`` on the InsuranceCompany.
  3. Write a parser here returning ``ParsedPolicyEntry`` records.
  4. Register it in ``PARSERS_BY_HOST`` keyed on the URL hostname.
"""

import re
from dataclasses import dataclass
from typing import Callable, Dict, List
from urllib.parse import urljoin


@dataclass(frozen=True)
class ParsedPolicyEntry:
    title: str
    url: str
    payer_policy_id: str = ""


# Cigna A-Z titles look like "Acupuncture - (CPG024)" -- pull the id off the
# end so we can use it for cross-references. Cigna's source uses both ASCII
# hyphens and U+2013 EN DASH; match either. The EN DASH is written as a
# Unicode escape so Ruff RUF001 doesn't flag the ambiguous glyph.
_CIGNA_ID_SUFFIX_RE = re.compile(r"\s*[-–]\s*\(([A-Za-z0-9_]+)\)\s*$")
_CIGNA_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\'](?P<href>[^"\']+\.pdf)["\'][^>]*>' r"(?P<text>[^<]+)</a>",
    re.IGNORECASE,
)
_CIGNA_BASE = "https://static.cigna.com"


def parse_cigna_medical_a_z(html: str) -> List[ParsedPolicyEntry]:
    """
    Parse Cigna's static A-Z medical coverage policies page into entries.

    The page lists policies as ``<a href="/assets/.../foo.pdf">Title - (ID)</a>``.
    We resolve the relative URL against ``static.cigna.com`` and lift the
    parenthesized id at the end of the title into ``payer_policy_id``.
    """
    entries: List[ParsedPolicyEntry] = []
    seen: set[str] = set()
    for m in _CIGNA_LINK_RE.finditer(html):
        href = m.group("href").strip()
        text = m.group("text").strip()
        if not href or not text:
            continue
        url = urljoin(_CIGNA_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        policy_id = ""
        id_match = _CIGNA_ID_SUFFIX_RE.search(text)
        if id_match:
            policy_id = id_match.group(1)
            text = _CIGNA_ID_SUFFIX_RE.sub("", text).strip()
        entries.append(
            ParsedPolicyEntry(title=text, url=url, payer_policy_id=policy_id)
        )
    return entries


# Map from URL hostname -> parser. The fetcher dispatches on hostname so a
# given parser can apply to several specific URL paths on the same host.
PARSERS_BY_HOST: Dict[str, Callable[[str], List[ParsedPolicyEntry]]] = {
    "static.cigna.com": parse_cigna_medical_a_z,
}
