"""Today's date on the letters the site prepares.

substitute_appeal_fields runs every letter through date_the_letter before the
person sees it, on the appeals page and in the assistant's chat alike. The
letter's own date line, the one at the top above the salutation and the
subject line, is set to today's date where the case is (letter_zone), written
the way the site writes dates ("October 8, 2026"). Nothing below the
salutation or the subject line is read, so a denial date, a date of service or
an appeal deadline in the body keeps whatever the letter says.

A line counts as the date line only when it holds a date and nothing else,
save a markdown heading mark, a label ("Date:", "Appeal Date:", "Date
Sent:"), markdown emphasis, a closing period or comma and invisible
characters around it: a date written out ("October 25, 2026", "Thu, Oct. 25,
2026", "25 October 2026", "25-Oct-2026", "October 25"), in numbers
("10/25/2026", "2026-10-25"), a blank for one ("[Date]", "[Insert Date]",
"[Today's Date]", "<Insert date>", "{{date}}"), or a stretch of time ("Later
this month", "Mid-October"). A lone date under a line that names it as some
other date ("Date of Birth:", "Dates of Service", "... denied this on") is
that date and is left alone, unless it carries the letter's own label
("Date:"). The first date line is replaced and no other, so running it twice
changes nothing, and a letter with no date line comes back as it went in.
"""

import datetime
import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

# How far down the letter the date line may sit, counted in non-blank lines:
# a sender block with a name, two address lines, a phone number, an email
# and a member id still leaves room for the date under it.
HEADER_LINES = 12

# The zone a letter is dated in, by the two-letter code of the state on the
# case. A state across two zones gets its western one, so no one in it gets
# tomorrow's date; its eastern part gets the day before in the first hour
# after midnight.
_ZONE_STATES: dict[str, str] = {
    "America/New_York": "CT DC DE GA MA MD ME NC NH NJ NY OH PA RI SC VA VT WV",
    "America/Chicago": "AL AR FL IA IL IN KY LA MI MN MO MS OK TN WI",
    "America/Denver": "CO KS MT ND NE NM SD TX UT WY",
    "America/Phoenix": "AZ",
    "America/Los_Angeles": "CA ID NV OR WA",
    "America/Adak": "AK",
    "Pacific/Honolulu": "HI",
    "America/Puerto_Rico": "PR VI",
    "Pacific/Guam": "GU MP",
    "Pacific/Pago_Pago": "AS",
}
STATE_ZONES: dict[str, str] = {
    state: zone for zone, states in _ZONE_STATES.items() for state in states.split()
}
# A case with no state: Pacific time is the westernmost zone of the 48
# contiguous states, so its date is never ahead of the date anywhere in them.
DEFAULT_LETTER_ZONE = "America/Los_Angeles"

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?"
    r"|dec(?:ember)?)\.?"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_YEAR = r"\d{4}"
_WEEKDAY = (
    r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\.?,?\s+)?"
)
_PERIOD = r"(?:week|month|year)"
# A day or month in numbers (1 to 31, or 1 to 12), and a year in numbers.
_NUMERIC_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_NUMERIC_MONTH = r"(?:0?[1-9]|1[0-2])"
_NUMERIC_YEAR = r"(?:(?:19|20)\d{2}|\d{2})(?!\d)"

_DATE = "|".join(
    (
        # October 25, 2026 / Oct. 25 2026 / Monday, October 25th, 2026 /
        # Thu, Oct 8, 2026 / October 25
        rf"{_WEEKDAY}{_MONTH}\s+{_DAY}(?:,?\s+{_YEAR})?",
        # 25 October 2026 / the 25th of October, 2026
        rf"{_WEEKDAY}(?:the\s+)?{_DAY}\s+(?:of\s+)?{_MONTH},?(?:\s+{_YEAR})?",
        # 25-Oct-2026
        rf"{_NUMERIC_DAY}-{_MONTH}-{_NUMERIC_YEAR}",
        # October 2026
        rf"{_MONTH},?\s+{_YEAR}",
        # 10/25/2026, 10-25-26, 25.10.2026
        rf"{_NUMERIC_DAY}(?P<sep>[/.-]){_NUMERIC_DAY}(?P=sep){_NUMERIC_YEAR}",
        # 2026-10-25
        rf"(?:19|20)\d\d(?P<iso_sep>[/.-]){_NUMERIC_MONTH}(?P=iso_sep)"
        rf"{_NUMERIC_DAY}(?!\d)",
        # [Date], [Insert Date], [Today's Date], [Current Date], [Date of
        # Letter], <Insert date>, {date}
        r"[\[<{]\s*(?:insert\s+|enter\s+)?(?:the\s+)?"
        r"(?:(?:current|today['’]?s|letter|appeal)\s+)?date"
        r"(?:\s+(?:here|of\s+(?:this\s+|the\s+)?(?:letter|appeal)))?"
        r"(?:\s+placeholder)?\s*[\]>}]",
        r"\{\{\s*(?:date|today)\s*\}\}",
        # [Month Day, Year], [MM/DD/YYYY]
        r"\[\s*(?:month\s+(?:day|dd),?\s+(?:year|yyyy)|mm/dd/yy(?:yy)?)\s*\]",
        # INSERT DATE HERE, Today's date, Today
        r"insert\s+(?:(?:current|today['’]?s)\s+)?date(?:\s+here)?",
        r"today(?:['’]?s\s+date)?",
        # Later this month, next week, mid-October, end of October 2026
        rf"(?:(?:early|mid|late|later|earlier)[\s-]+)?(?:this|next)\s+{_PERIOD}",
        rf"(?:early|mid|late)[\s-]+{_MONTH}(?:,?\s+{_YEAR})?",
        rf"(?:the\s+)?(?:end|beginning|start|middle)\s+of\s+"
        rf"(?:{_MONTH}|(?:this|next)\s+{_PERIOD})(?:,?\s+{_YEAR})?",
    )
)

# A label that names the date after it as the letter's own.
_LETTER_DATE_LABEL = (
    r"(?:(?:letter|today['’]?s|appeal)\s+date"
    r"|date(?:d|\s+of\s+(?:this\s+|the\s+)?(?:letter|appeal)"
    r"|\s+(?:submitted|sent|written))?)\s*:"
)
_EMPHASIS = r"[*_]{0,2}"
# Zero-width space, direction marks, word joiner and byte-order mark.
_INVISIBLE = r"[​‎‏⁠﻿]*"
_HEADING = r"(?:#{1,6}[ \t]+)?"

_DATE_LINE = re.compile(
    rf"(?P<lead>{_INVISIBLE}[ \t]*{_HEADING}{_EMPHASIS}"
    rf"(?P<label>{_LETTER_DATE_LABEL}{_EMPHASIS}[ \t]*)?)"
    rf"(?P<date>{_DATE})"
    rf"(?P<trail>{_EMPHASIS}[.,]?[ \t]*{_INVISIBLE}[ \t\r]*)",
    re.IGNORECASE,
)
_LABEL_ONLY = re.compile(
    rf"{_INVISIBLE}[ \t]*{_HEADING}{_EMPHASIS}{_LETTER_DATE_LABEL}{_EMPHASIS}"
    rf"[ \t\r]*",
    re.IGNORECASE,
)
# Where the letter turns to its reader or names its subject: the date line
# sits above these, and nothing at or below them is read.
_BODY_STARTS = re.compile(
    r"[^\w]*(?:dear\b|to\s+whom\b|re\s*:|subject\s*:|regarding\s*:)",
    re.IGNORECASE,
)
# A line naming the value under it: a label ("Date of Birth:", "Dates of
# Service", "DOB") ...
_LABEL = re.compile(r":[*_ \t\r]*$|\b(?:dates?|dos|dob)\b", re.IGNORECASE)
# ... unless it holds its own value, after its colon ("DOB: 01/02/1980",
# "Attn: Member Services") or as a date ("DOB 01/02/1980") ...
_VALUE_AFTER_COLON = re.compile(r":[*_ \t]*[^*_\s]")
_HOLDS_A_DATE = re.compile(rf"(?<!\w)(?:{_DATE})", re.IGNORECASE)
# ... or a sentence broken off before its date ("Your plan denied this on",
# "Your letter dated").
_BROKEN_OFF = re.compile(
    r"\b(?:on|of|dated|since|from|until|by|before|after)[*_ \t\r]*$",
    re.IGNORECASE,
)


def format_letter_date(day: datetime.date) -> str:
    """A date as the site writes one in a letter: "October 8, 2026"."""
    return f"{day:%B} {day.day}, {day.year}"


def letter_zone(state: Optional[str] = None) -> Optional[ZoneInfo]:
    """The zone a letter for a case in ``state`` (a two-letter code) is dated
    in: the state's own, its western one where it has two, and Pacific time
    when the state is missing or not one listed. None when the zone cannot be
    loaded, and the configured timezone is used instead."""
    name = STATE_ZONES.get((state or "").strip().upper(), DEFAULT_LETTER_ZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def todays_letter_date(state: Optional[str] = None) -> str:
    """Today's date where a case in ``state`` is (letter_zone), as a letter
    writes it."""
    return format_letter_date(timezone.localdate(timezone=letter_zone(state)))


def _names_the_line_under_it(line: str) -> bool:
    if _LABEL_ONLY.fullmatch(line):
        # "Date:" alone names the letter's own date.
        return False
    if _BROKEN_OFF.search(line):
        return True
    return bool(
        _LABEL.search(line)
        and not _VALUE_AFTER_COLON.search(line)
        and not _HOLDS_A_DATE.search(line)
    )


def date_the_letter(content: str, today: Optional[str] = None) -> str:
    """The letter with its date line set to ``today`` (today's date in
    Pacific time when not given). Only the first date line above the
    salutation and subject line is replaced; a letter without one is
    returned unchanged."""
    if not content:
        return content
    lines = content.split("\n")
    seen = 0
    # Whether the next date line is the value a line above names, and
    # whether that line has had its value yet: a blank line ends a list of
    # dates under a label, but not the wait for the first one.
    named = False
    answered = False
    for index, line in enumerate(lines):
        if not line.strip():
            if answered:
                named = False
            continue
        seen += 1
        if seen > HEADER_LINES or _BODY_STARTS.match(line):
            break
        match = _DATE_LINE.fullmatch(line)
        if match is None:
            named = _names_the_line_under_it(line)
            answered = False
            continue
        if named and not match.group("label"):
            # A date of birth or of service under its label, or the next one
            # in a list of them.
            answered = True
            continue
        lines[index] = (
            match.group("lead")
            + (today if today is not None else todays_letter_date())
            + match.group("trail")
        )
        return "\n".join(lines)
    return content
