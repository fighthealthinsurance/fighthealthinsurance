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


def _load() -> tuple[list[_Rule], list[_Rule]]:
    spec = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    return (
        [_compile(entry) for entry in spec["ignore"]],
        [_compile(entry) for entry in spec["placeholders"]],
    )


_IGNORE, _PLACEHOLDERS = _load()


def find_unfilled_placeholders(text: str) -> list[str]:
    """Each blank in ``text``, once, in the order it first appears."""
    rest = text or ""

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
