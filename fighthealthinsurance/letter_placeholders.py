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


@dataclass(frozen=True)
class _Spot:
    # Where the blank starts in the letter.
    at: int
    # The blank exactly as the letter has it.
    written: str
    # What a message calls it: the blank itself, or its label (a line to
    # write on is ___ however long it is).
    shown: str


def _find_spots(text: str) -> list[_Spot]:
    """Every blank in ``text``, in the order it appears."""
    rest = _blank_out_reference_links(text or "")

    def blank_out(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    for rule in _IGNORE:
        rest = rule.regex.sub(blank_out, rest)

    # Each pattern claims what it matches, so a later one never reports
    # part of a blank an earlier one already found.
    spots: list[_Spot] = []
    for rule in _PLACEHOLDERS:

        def claim(match: re.Match[str], rule: _Rule = rule) -> str:
            written = match.group(0)
            spots.append(_Spot(match.start(), written, rule.label or written))
            return blank_out(match)

        rest = rule.regex.sub(claim, rest)

    spots.sort(key=lambda spot: spot.at)
    return spots


def _once_each(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found


def find_unfilled_placeholders(text: str) -> list[str]:
    """Each blank in ``text``, once, in the order it first appears."""
    return _once_each([spot.shown for spot in _find_spots(text)])


def is_ignored(text: str) -> bool:
    """Whether the whole of ``text`` is something the check leaves alone,
    such as a quotation's bracketed word ([This]) or note ([Cleaned up])."""
    return any(rule.regex.fullmatch(text) for rule in _IGNORE)


def find_placeholder_spans(text: str) -> list[tuple[int, int]]:
    """Where each blank in ``text`` is, as ``(start, end)``, in the order it
    appears: what the fax form stops for, for a caller that lists the blanks
    as the letter has them (assistant_drafts.placeholders_in)."""
    return [(spot.at, spot.at + len(spot.written)) for spot in _find_spots(text)]


def find_placeholders_as_written(text: str) -> list[str]:
    """Each blank in ``text`` exactly as the letter has it, once, in the
    order it first appears.

    This is what a person says yes to when they send a letter with blanks
    as it is, and what the browser posts for them. It is the same list as
    ``find_unfilled_placeholders`` except for a line to write on, which is
    named ``___`` there whatever its length and is the line itself here, so
    a line of another length is another blank.
    """
    return _once_each([spot.written for spot in _find_spots(text)])


@dataclass(frozen=True)
class BlanksToName:
    # Each blank, once, as a message names it: first the ones it has room
    # for, in the order the letter has them, then the rest, which
    # describe_placeholders counts rather than names.
    shown: list[str]
    # What "Send it as it is" says yes to, each blank exactly as the letter
    # has it: the ones the message names, and the ones the person has
    # already said yes to. Never a blank they have not been shown.
    send_as_it_is: list[str]


def blanks_to_name(text: str, approved: set[str]) -> BlanksToName:
    """The blanks a message about ``text`` names, and what its box says yes to.

    A message names at most ``LISTED_IN_A_MESSAGE`` and says how many more
    there are. Blanks the person has not said yes to (not in ``approved``)
    are named before ones they have, so once they say yes to the first ten,
    the next message names the rest. A line to write on is named ``___``
    once, and stands for every line in the letter.
    """
    spots = _find_spots(text)
    shown = _once_each([spot.shown for spot in spots])
    waiting = {spot.shown for spot in spots if spot.written not in approved}
    # sorted() keeps the letter's order among the waiting, and among the rest.
    room = set(
        sorted(shown, key=lambda label: label not in waiting)[:LISTED_IN_A_MESSAGE]
    )
    return BlanksToName(
        shown=[label for label in shown if label in room]
        + [label for label in shown if label not in room],
        send_as_it_is=_once_each(
            [
                spot.written
                for spot in spots
                if spot.shown in room or spot.written in approved
            ]
        ),
    )


def describe_placeholders(found: list[str]) -> str:
    """The blanks as one line for a message: ``[Your Name], {{SCSID}}``."""
    listed = ", ".join(found[:LISTED_IN_A_MESSAGE])
    more = len(found) - LISTED_IN_A_MESSAGE
    if more > 0:
        listed += f" and {more} more"
    return listed
