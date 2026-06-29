"""Helpers for sending email during the recipient's likely business hours.

Why a special window? When we don't know where a recipient is, we still want
mail to land while they're at work. The contiguous US spans Eastern to Pacific
(three hours). The slice of the day that is inside normal business hours in
*every* one of those zones at once is **9am-2pm Pacific**: 9am Pacific is noon
Eastern, and 2pm Pacific is 5pm Eastern. Sending in that window means the mail
arrives during business hours whether the recipient is on the east or west
coast, which is the conservative default used when we have no better signal.

When we *do* have a phone number with a recognizable North American area code we
map it to that area code's timezone and use a normal local business-hours window
(9am-5pm) instead of the cross-country overlap.

Anything we can't confidently place (no phone, unparseable number, or an area
code we don't recognize) falls back to the conservative Pacific overlap, which
is always inside business hours across the lower 48 -- so an omission here is
safe, never sending at a bad local time.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

# Conservative default: the recipient's timezone is unknown.
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Local-hour windows (24h, [start, end)).
# Unknown timezone -> the all-US business-hours overlap (9am-2pm Pacific).
DEFAULT_WINDOW: tuple[int, int] = (9, 14)
# Known recipient timezone -> normal local business hours (9am-5pm).
SPECIFIC_WINDOW: tuple[int, int] = (9, 17)

# North American area code -> IANA timezone. Grouped by zone for maintainability
# and flattened below. Split states/area codes are assigned to the timezone of
# their most populous region; genuinely ambiguous codes (e.g. FL 850, IN 812)
# are intentionally omitted so they fall back to the safe Pacific overlap.
_ZONE_AREA_CODES: dict[str, tuple[str, ...]] = {
    "America/New_York": (
        # CT, DC, DE
        "203",
        "475",
        "860",
        "959",
        "202",
        "302",
        # FL (peninsula -- Eastern)
        "305",
        "786",
        "321",
        "407",
        "689",
        "561",
        "772",
        "904",
        "386",
        "352",
        "754",
        "954",
        "727",
        "813",
        "941",
        "239",
        "863",
        # GA
        "404",
        "470",
        "678",
        "770",
        "762",
        "706",
        "912",
        "229",
        # IN (Eastern majority)
        "317",
        "463",
        "260",
        "574",
        "765",
        "930",
        # KY (Louisville/Lexington -- Eastern)
        "502",
        "859",
        "606",
        # MA
        "617",
        "857",
        "781",
        "339",
        "508",
        "774",
        "978",
        "351",
        "413",
        # MD
        "410",
        "443",
        "240",
        "301",
        # ME
        "207",
        # MI (Eastern majority, incl. most of the UP)
        "313",
        "248",
        "947",
        "586",
        "734",
        "810",
        "989",
        "517",
        "616",
        "231",
        "269",
        "906",
        # NC
        "704",
        "980",
        "828",
        "336",
        "743",
        "919",
        "984",
        "252",
        "910",
        # NH
        "603",
        # NJ
        "201",
        "551",
        "609",
        "640",
        "732",
        "848",
        "856",
        "862",
        "908",
        "973",
        # NY
        "212",
        "646",
        "332",
        "718",
        "347",
        "929",
        "917",
        "516",
        "631",
        "934",
        "845",
        "914",
        "315",
        "680",
        "585",
        "607",
        "716",
        "518",
        "838",
        # OH
        "216",
        "220",
        "234",
        "330",
        "380",
        "419",
        "440",
        "513",
        "567",
        "614",
        "740",
        "937",
        "326",
        # PA
        "215",
        "267",
        "445",
        "484",
        "610",
        "412",
        "878",
        "717",
        "223",
        "570",
        "272",
        "814",
        "724",
        # RI
        "401",
        # SC
        "803",
        "839",
        "843",
        "854",
        "864",
        # TN (eastern -- Knoxville/Chattanooga)
        "423",
        "865",
        # VA
        "703",
        "571",
        "804",
        "757",
        "540",
        "434",
        "276",
        # VT
        "802",
        # WV
        "304",
        "681",
    ),
    "America/Chicago": (
        # AL
        "205",
        "251",
        "256",
        "334",
        "938",
        "659",
        # AR
        "479",
        "501",
        "870",
        # IA
        "319",
        "515",
        "563",
        "641",
        "712",
        # IL
        "312",
        "872",
        "773",
        "224",
        "847",
        "630",
        "331",
        "708",
        "815",
        "779",
        "217",
        "447",
        "309",
        "618",
        "464",
        # IN (NW -- Gary, Central)
        "219",
        # KS (Central majority)
        "316",
        "620",
        "785",
        "913",
        # KY (western -- Central)
        "270",
        "364",
        # LA
        "225",
        "318",
        "337",
        "504",
        "985",
        # MN
        "218",
        "320",
        "507",
        "612",
        "651",
        "763",
        "952",
        # MO
        "314",
        "557",
        "573",
        "636",
        "660",
        "816",
        "417",
        # MS
        "228",
        "601",
        "662",
        "769",
        # ND (Central majority)
        "701",
        # NE (Central majority)
        "402",
        "531",
        "308",
        # OK
        "405",
        "572",
        "539",
        "918",
        "580",
        # SD (Central majority -- Sioux Falls)
        "605",
        # TN (central/western -- Nashville/Memphis)
        "615",
        "629",
        "731",
        "901",
        "931",
        # TX (Central majority)
        "214",
        "469",
        "972",
        "945",
        "817",
        "682",
        "713",
        "281",
        "832",
        "346",
        "409",
        "936",
        "979",
        "210",
        "726",
        "512",
        "737",
        "361",
        "956",
        "830",
        "254",
        "940",
        "325",
        "432",
        "806",
        # WI
        "262",
        "274",
        "414",
        "534",
        "608",
        "715",
        "920",
    ),
    "America/Denver": (
        # CO
        "303",
        "720",
        "983",
        "970",
        "719",
        # ID (Mountain majority -- Boise)
        "208",
        "986",
        # MT
        "406",
        # NM
        "505",
        "575",
        # TX (El Paso)
        "915",
        # UT
        "385",
        "801",
        # WY
        "307",
    ),
    "America/Phoenix": (
        # AZ (no DST)
        "480",
        "602",
        "623",
        "520",
        "928",
    ),
    "America/Los_Angeles": (
        # CA
        "209",
        "213",
        "279",
        "310",
        "323",
        "408",
        "415",
        "424",
        "442",
        "510",
        "530",
        "559",
        "562",
        "619",
        "626",
        "628",
        "650",
        "657",
        "661",
        "669",
        "707",
        "714",
        "747",
        "760",
        "805",
        "818",
        "820",
        "831",
        "840",
        "858",
        "909",
        "916",
        "925",
        "949",
        "951",
        # NV
        "702",
        "725",
        "775",
        # OR (Pacific majority)
        "503",
        "971",
        "541",
        "458",
        # WA
        "206",
        "253",
        "360",
        "425",
        "509",
        "564",
    ),
    "America/Anchorage": ("907",),
    "Pacific/Honolulu": ("808",),
}

# Flattened lookup: area code -> IANA timezone name.
AREA_CODE_TIMEZONES: dict[str, str] = {
    area_code: tz
    for tz, area_codes in _ZONE_AREA_CODES.items()
    for area_code in area_codes
}

# IANA timezone -> short, human-friendly label for the staff UI hint.
_FRIENDLY_TZ_LABELS: dict[str, str] = {
    "America/New_York": "Eastern",
    "America/Chicago": "Central",
    "America/Denver": "Mountain",
    "America/Phoenix": "Arizona (MST)",
    "America/Los_Angeles": "Pacific",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
}


def _zone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA name to a ZoneInfo, falling back to the default zone."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def normalize_area_code(phone: Optional[str]) -> Optional[str]:
    """Extract the 3-digit NANP area code from a free-form phone string.

    Handles common formats ("(212) 555-1234", "212-555-1234", "+1 212 555
    1234", "+1 808 555 1234 x99"). Returns ``None`` when the value isn't a
    canonical 10-digit NANP number after dropping an optional ``+1`` and any
    extension -- including international numbers like "+44 20 7946 0958" -- so
    callers fall back to the conservative default window instead of inventing a
    US area code from non-NANP digits. Does not further validate that the result
    is an assigned area code -- callers look it up in
    :data:`AREA_CODE_TIMEZONES`, where unknown codes simply miss.
    """
    if not phone:
        return None
    # Strip a trailing extension ("x99", "ext. 5") first so its digits don't
    # inflate the length check or get mistaken for part of the number.
    core = re.split(r"(?:ext\.?|x)\s*\d+\s*$", phone, flags=re.IGNORECASE)[0]
    digits = re.sub(r"\D", "", core)
    # Drop a leading US/Canada country code so "+1 212..." parses correctly.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Require a canonical 10-digit NANP number; anything else (too short, or an
    # international number) has no area code we can trust.
    if len(digits) != 10:
        return None
    return digits[:3]


def timezone_for_phone(phone: Optional[str]) -> Optional[str]:
    """IANA timezone for a phone number's area code, or ``None`` if unknown."""
    area_code = normalize_area_code(phone)
    if area_code is None:
        return None
    return AREA_CODE_TIMEZONES.get(area_code)


def resolve_send_timezone(phone: Optional[str]) -> tuple[str, bool]:
    """Return ``(timezone_name, is_specific)`` for a (possibly missing) phone.

    ``is_specific`` is ``True`` when the timezone was confidently derived from
    the phone's area code (so the normal local window applies), and ``False``
    when we fell back to the conservative cross-US Pacific overlap.
    """
    tz_name = timezone_for_phone(phone)
    if tz_name is not None:
        return tz_name, True
    return DEFAULT_TIMEZONE, False


def _window_for(is_specific: bool) -> tuple[int, int]:
    """Local-hour window to use, depending on timezone confidence."""
    return SPECIFIC_WINDOW if is_specific else DEFAULT_WINDOW


def is_within_business_hours(
    now: datetime.datetime, tz_name: str, is_specific: bool
) -> bool:
    """Whether ``now`` falls inside the business-hours window for ``tz_name``.

    Weekends are always outside the window. ``now`` must be timezone-aware.
    """
    local = now.astimezone(_zone(tz_name))
    if local.weekday() >= 5:  # Saturday / Sunday
        return False
    start, end = _window_for(is_specific)
    return start <= local.hour < end


def next_business_hours_start(
    tz_name: str,
    is_specific: bool,
    *,
    now: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """Earliest UTC time the email may send, gated on the business-hours window.

    Returns ``now`` when we're already inside a weekday window (eligible
    immediately); otherwise the next weekday window opening (today if it's a
    weekday and we're still before the window, else the next weekday).
    """
    if now is None:
        now = timezone.now()
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    start, end = _window_for(is_specific)

    # Already inside a weekday window -> send as soon as the actor picks it up.
    if local.weekday() < 5 and start <= local.hour < end:
        return now

    # Today still works only if it's a weekday and we're before the window opens.
    day = local
    if not (local.weekday() < 5 and local.hour < start):
        day = local + datetime.timedelta(days=1)
    while day.weekday() >= 5:  # skip Saturday / Sunday
        day = day + datetime.timedelta(days=1)

    window_start = day.replace(hour=start, minute=0, second=0, microsecond=0)
    return window_start.astimezone(datetime.timezone.utc)


def _format_hour(hour: int) -> str:
    """Render a 24h hour as a friendly 12h label ("9am", "2pm", "12pm")."""
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12
    if display == 0:
        display = 12
    return f"{display}{suffix}"


def describe_send_window(phone: Optional[str]) -> str:
    """One-line, staff-facing description of when a queued email would send."""
    tz_name, is_specific = resolve_send_timezone(phone)
    start, end = _window_for(is_specific)
    label = _FRIENDLY_TZ_LABELS.get(tz_name, tz_name)
    window = f"{_format_hour(start)}–{_format_hour(end)} {label}"
    if is_specific:
        return (
            f"Will send during business hours ({window}), based on the phone area code."
        )
    return (
        f"Will send during business hours ({window}) — no usable phone "
        f"timezone, so using the all-US business-hours overlap."
    )
