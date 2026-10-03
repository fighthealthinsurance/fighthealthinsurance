"""Blanks left in a finished letter: ``[Your Name]``, ``{{SCSID}}``, ``XXX``.

The patterns live in ``letter_placeholders.json`` next to this file, which
``static/js/letter_placeholders.ts`` bundles for the print and fax buttons,
so the browser and the fax form find the same things. This side is what the
fax form checks before a letter is staged for an insurance company.
"""

import json
import pathlib
import re
from dataclasses import dataclass
from typing import Optional

PATTERNS_FILE = pathlib.Path(__file__).with_name("letter_placeholders.json")

# How many the fax form names before it says "and N more".
LISTED_IN_A_MESSAGE = 10


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    label: Optional[str]


def _compile(entry: dict) -> _Rule:
    # re.ASCII makes \b and the character classes read text the way
    # JavaScript's RegExp does without the u flag.
    flags = re.ASCII
    if "i" in entry.get("flags", ""):
        flags |= re.IGNORECASE
    return _Rule(re.compile(entry["pattern"], flags), entry.get("label"))


def _load() -> tuple[_Rule, _Rule, list[_Rule], list[_Rule]]:
    spec = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    return (
        _compile(spec["reference_links"]["definition"]),
        _compile(spec["reference_links"]["link"]),
        [_compile(entry) for entry in spec["ignore"]],
        [_compile(entry) for entry in spec["placeholders"]],
    )


_REFERENCE_DEFINITION, _REFERENCE_LINK, _IGNORE, _PLACEHOLDERS = _load()

_CAPITAL = re.compile("[A-Z]")
_SPACES = re.compile(r"[ \t]+")


def _reference_id(text: str) -> str:
    """A reference link's id as it is matched: capitals and runs of spaces
    don't count. Only A to Z fold, the same as in the browser."""
    lowered = _CAPITAL.sub(lambda capital: capital.group(0).lower(), text)
    return _SPACES.sub(" ", lowered).strip(" ")


def _blank_out_reference_links(text: str) -> str:
    """Blank out each reference link the letter defines, and each definition.

    ``[Coverage Policy][1]`` with ``[1]: https://example.com/policy`` on a
    line of its own is a link, not a blank. A reference link to an id the
    letter never defines is left as it is, for the placeholder patterns.
    """
    defined: set[str] = set()

    def definition(match: re.Match[str]) -> str:
        whole = match.group(0)
        # The match can start with the line break before the definition,
        # which stays, so the lines stay apart.
        start = whole.index("[")
        defined.add(_reference_id(whole[start + 1 : whole.index("]", start)]))
        return whole[:start] + " " * (len(whole) - start)

    rest = _REFERENCE_DEFINITION.regex.sub(definition, text)

    def link(match: re.Match[str]) -> str:
        whole = match.group(0)
        shown, _, given_id = whole[1:-1].partition("][")
        found_id = _reference_id(given_id or shown)
        if found_id and found_id in defined:
            return " " * len(whole)
        return whole

    return _REFERENCE_LINK.regex.sub(link, rest)


def find_unfilled_placeholders(text: str) -> list[str]:
    """Each blank in ``text``, once, in the order it first appears."""
    rest = _blank_out_reference_links(text or "")

    def blank_out(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    for rule in _IGNORE:
        rest = rule.regex.sub(blank_out, rest)

    hits: list[tuple[int, str]] = []
    for rule in _PLACEHOLDERS:

        def claim(match: re.Match[str], rule: _Rule = rule) -> str:
            hits.append((match.start(), rule.label or match.group(0)))
            return blank_out(match)

        rest = rule.regex.sub(claim, rest)

    hits.sort(key=lambda hit: hit[0])
    found: list[str] = []
    for _, shown in hits:
        if shown not in found:
            found.append(shown)
    return found


def describe_placeholders(found: list[str]) -> str:
    """The blanks as one line for a message: ``[Your Name], {{SCSID}}``."""
    listed = ", ".join(found[:LISTED_IN_A_MESSAGE])
    more = len(found) - LISTED_IN_A_MESSAGE
    if more > 0:
        listed += f" and {more} more"
    return listed
