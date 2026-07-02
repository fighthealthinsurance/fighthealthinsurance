"""Tests for business_hours: area-code -> timezone, the sending windows, and
the next-window computation used to defer email to likely business hours."""

import datetime
from zoneinfo import ZoneInfo

from fighthealthinsurance.business_hours import (
    DEFAULT_TIMEZONE,
    describe_send_window,
    is_within_business_hours,
    next_business_hours_start,
    normalize_area_code,
    resolve_send_timezone,
    timezone_for_phone,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
EASTERN = ZoneInfo("America/New_York")
PHOENIX = ZoneInfo("America/Phoenix")

# Fixed reference dates (verified weekdays):
#   2026-06-17 = Wednesday, 2026-06-19 = Friday,
#   2026-06-20 = Saturday,  2026-06-21 = Sunday.
WEDNESDAY = (2026, 6, 17)
FRIDAY = (2026, 6, 19)
SATURDAY = (2026, 6, 20)


def _at(date, hour, tz, minute=0):
    return datetime.datetime(*date, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------------------
# Area-code parsing
# ---------------------------------------------------------------------------


def test_normalize_area_code_various_formats():
    assert normalize_area_code("(212) 555-1234") == "212"
    assert normalize_area_code("212-555-1234") == "212"
    assert normalize_area_code("+1 212 555 1234") == "212"
    assert normalize_area_code("12125551234") == "212"
    assert normalize_area_code("212.555.1234 x99") == "212"


def test_normalize_area_code_strips_country_code_with_extension():
    # The country code must be dropped even when an extension pushes the digit
    # count past 11, otherwise the area code wrongly becomes "180" (regression).
    assert normalize_area_code("+1 808 555 1234 x99") == "808"
    assert normalize_area_code("1-808-555-1234 ext. 5") == "808"
    assert normalize_area_code("+1 (212) 555-1234 x1000") == "212"


def test_normalize_area_code_rejects_international():
    # Non-NANP numbers must not be coerced into a US area code; callers then
    # fall back to the conservative default window instead.
    assert normalize_area_code("+44 20 7946 0958") is None
    assert normalize_area_code("+33 1 42 68 53 00") is None


def test_normalize_area_code_insufficient_digits():
    assert normalize_area_code("555-1234") is None
    assert normalize_area_code("") is None
    assert normalize_area_code(None) is None


def test_timezone_for_phone_known_codes():
    assert timezone_for_phone("212-555-1234") == "America/New_York"
    assert timezone_for_phone("415-555-1234") == "America/Los_Angeles"
    assert timezone_for_phone("312-555-1234") == "America/Chicago"
    assert timezone_for_phone("303-555-1234") == "America/Denver"
    assert timezone_for_phone("602-555-1234") == "America/Phoenix"
    assert timezone_for_phone("808-555-1234") == "Pacific/Honolulu"
    assert timezone_for_phone("907-555-1234") == "America/Anchorage"


def test_timezone_for_phone_unknown_or_service_code():
    # 999 is not an assigned area code; 555 is not in the map either.
    assert timezone_for_phone("999-555-1234") is None
    assert timezone_for_phone(None) is None
    # 812 and its overlay 930 (southern Indiana, split Eastern/Central) are
    # deliberately ambiguous -> conservative fallback, never a guessed zone.
    assert timezone_for_phone("812-555-1234") is None
    assert timezone_for_phone("930-555-1234") is None


def test_resolve_send_timezone():
    # Known area code -> specific timezone.
    assert resolve_send_timezone("212-555-1234") == ("America/New_York", True)
    # No / unknown phone -> conservative Pacific overlap, not specific.
    assert resolve_send_timezone(None) == (DEFAULT_TIMEZONE, False)
    assert resolve_send_timezone("999-555-1234") == (DEFAULT_TIMEZONE, False)


# ---------------------------------------------------------------------------
# Window membership
# ---------------------------------------------------------------------------


def test_default_window_is_nine_to_two_pacific():
    # is_specific=False -> 9am-2pm Pacific (the cross-US overlap).
    assert is_within_business_hours(
        _at(WEDNESDAY, 10, PACIFIC), DEFAULT_TIMEZONE, False
    )
    assert is_within_business_hours(_at(WEDNESDAY, 9, PACIFIC), DEFAULT_TIMEZONE, False)
    assert is_within_business_hours(
        _at(WEDNESDAY, 13, PACIFIC, 59), DEFAULT_TIMEZONE, False
    )
    # Boundaries: before 9am and at/after 2pm are out.
    assert not is_within_business_hours(
        _at(WEDNESDAY, 8, PACIFIC, 59), DEFAULT_TIMEZONE, False
    )
    assert not is_within_business_hours(
        _at(WEDNESDAY, 14, PACIFIC), DEFAULT_TIMEZONE, False
    )


def test_specific_window_is_nine_to_five_local():
    # is_specific=True -> normal 9am-5pm in the recipient's own timezone.
    assert is_within_business_hours(
        _at(WEDNESDAY, 16, EASTERN), "America/New_York", True
    )
    assert not is_within_business_hours(
        _at(WEDNESDAY, 17, EASTERN), "America/New_York", True
    )
    # 4pm Pacific would be outside the conservative default window though.
    assert not is_within_business_hours(
        _at(WEDNESDAY, 16, PACIFIC), DEFAULT_TIMEZONE, False
    )


def test_weekends_are_never_within_business_hours():
    assert not is_within_business_hours(
        _at(SATURDAY, 11, PACIFIC), DEFAULT_TIMEZONE, False
    )


# ---------------------------------------------------------------------------
# Next-window computation
# ---------------------------------------------------------------------------


def _pacific(dt):
    return dt.astimezone(PACIFIC)


def test_next_window_within_window_returns_now():
    now = _at(WEDNESDAY, 10, PACIFIC)
    assert next_business_hours_start(DEFAULT_TIMEZONE, False, now=now) == now


def test_next_window_before_window_is_today():
    now = _at(WEDNESDAY, 7, PACIFIC)
    result = _pacific(next_business_hours_start(DEFAULT_TIMEZONE, False, now=now))
    assert result.hour == 9
    assert result.date() == now.date()
    assert is_within_business_hours(result, DEFAULT_TIMEZONE, False)


def test_next_window_after_window_is_next_weekday():
    now = _at(WEDNESDAY, 15, PACIFIC)  # past 2pm
    result = _pacific(next_business_hours_start(DEFAULT_TIMEZONE, False, now=now))
    assert result.hour == 9
    assert result.date() == now.date() + datetime.timedelta(days=1)  # Thursday
    assert result.weekday() < 5


def test_next_window_skips_weekend():
    now = _at(FRIDAY, 15, PACIFIC)  # Friday after the window
    result = _pacific(next_business_hours_start(DEFAULT_TIMEZONE, False, now=now))
    assert result.weekday() == 0  # Monday
    assert result.hour == 9


def test_next_window_from_saturday_is_monday():
    now = _at(SATURDAY, 11, PACIFIC)
    result = _pacific(next_business_hours_start(DEFAULT_TIMEZONE, False, now=now))
    assert result.weekday() == 0  # Monday
    assert result.hour == 9


def test_next_window_specific_zone_phoenix_before_window():
    # Specific (is_specific=True) non-DST zone, before 9am on a weekday ->
    # same-day 9am Arizona time.
    now = _at(WEDNESDAY, 7, PHOENIX)
    result = next_business_hours_start("America/Phoenix", True, now=now).astimezone(
        PHOENIX
    )
    assert result.hour == 9
    assert result.date() == now.date()
    assert is_within_business_hours(result, "America/Phoenix", True)


def test_next_window_specific_zone_crosses_spring_forward():
    # Friday evening before the US spring-forward weekend -> Monday 9am Eastern,
    # computed correctly across the DST boundary.
    now = datetime.datetime(2026, 3, 6, 18, 0, tzinfo=EASTERN)  # Fri, after window
    result = next_business_hours_start("America/New_York", True, now=now).astimezone(
        EASTERN
    )
    assert result.weekday() == 0  # Monday 2026-03-09
    assert result.hour == 9 and result.minute == 0
    assert is_within_business_hours(result, "America/New_York", True)


def test_next_window_specific_zone_crosses_fall_back():
    # Friday evening before the US fall-back weekend -> Monday 9am Eastern.
    now = datetime.datetime(2026, 10, 30, 18, 0, tzinfo=EASTERN)  # Fri, after window
    result = next_business_hours_start("America/New_York", True, now=now).astimezone(
        EASTERN
    )
    assert result.weekday() == 0  # Monday 2026-11-02
    assert result.hour == 9 and result.minute == 0
    assert is_within_business_hours(result, "America/New_York", True)


# ---------------------------------------------------------------------------
# Human-readable hint
# ---------------------------------------------------------------------------


def test_describe_send_window_specific_vs_default():
    specific = describe_send_window("212-555-1234")
    assert "Eastern" in specific
    assert "area code" in specific

    default = describe_send_window(None)
    assert "Pacific" in default
    assert "9am" in default and "2pm" in default
