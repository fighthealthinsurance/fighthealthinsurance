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

# North American area code -> IANA timezone. Codes are packed per zone (split
# on whitespace below); see git history for a per-state annotated version.
# Split states/area codes are assigned to the timezone of their most populous
# region; genuinely ambiguous codes (e.g. FL 850/448, IN 812/930) are
# intentionally omitted so they fall back to the safe Pacific overlap.
_ZONE_AREA_CODES: dict[str, str] = {
    "America/New_York": """
        201 202 203 207 212 215 216 220 223 229 231 234 239
        240 248 252 260 267 269 272 276 301 302 304 305 313
        315 317 321 326 330 332 336 339 347 351 352 380 386
        401 404 407 410 412 413 419 423 434 440 443 445 463
        470 475 484 502 508 513 516 517 518 540 551 561 567
        570 571 574 585 586 603 606 607 609 610 614 616 617
        631 640 646 678 680 681 689 703 704 706 716 717 718
        724 727 732 734 740 743 754 757 762 765 770 772 774
        781 786 802 803 804 810 813 814 828 838 839 843 845
        848 854 856 857 859 860 862 863 864 865 878 904 906
        908 910 912 914 917 919 929 934 937 941 947 954 959
        973 978 980 984 989
    """,
    "America/Chicago": """
        205 210 214 217 218 219 224 225 228 251 254 256 262
        270 274 281 308 309 312 314 316 318 319 320 325 331
        334 337 346 361 364 402 405 409 414 417 432 447 464
        469 479 501 504 507 512 515 531 534 539 557 563 572
        573 580 601 605 608 612 615 618 620 629 630 636 641
        651 659 660 662 682 701 708 712 713 715 726 731 737
        763 769 773 779 785 806 815 816 817 830 832 847 870
        872 901 913 918 920 931 936 938 940 945 952 956 972
        979 985
    """,
    "America/Denver": """
        208 303 307 385 406 505 575 719 720 801 915 970 983
        986
    """,
    "America/Phoenix": """
        480 520 602 623 928
    """,
    "America/Los_Angeles": """
        206 209 213 253 279 310 323 360 408 415 424 425 442
        458 503 509 510 530 541 559 562 564 619 626 628 650
        657 661 669 702 707 714 725 747 760 775 805 818 820
        831 840 858 909 916 925 949 951 971
    """,
    "America/Anchorage": """
        907
    """,
    "Pacific/Honolulu": """
        808
    """,
}

# Flattened lookup: area code -> IANA timezone name.
AREA_CODE_TIMEZONES: dict[str, str] = {
    area_code: tz
    for tz, area_codes in _ZONE_AREA_CODES.items()
    for area_code in area_codes.split()
}
# A code listed under two zones would silently last-write-win in the dict
# comprehension above; fail loudly at import instead.
assert len(AREA_CODE_TIMEZONES) == sum(
    len(codes.split()) for codes in _ZONE_AREA_CODES.values()
), "duplicate area code across zones in _ZONE_AREA_CODES"

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
