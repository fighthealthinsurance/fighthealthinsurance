import difflib
import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import pandas as pd
from loguru import logger

# Look for data/ next to the repo root
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_FILE = "medicaid_resources.csv"


def _explode_scraped_faq(df: pd.DataFrame) -> pd.DataFrame:
    if "scraped_faq" not in df.columns:
        return pd.DataFrame(
            columns=["faq_question", "faq_answer", "faq_source_url", "faq_fetched"]
        )
    rows: List[dict] = []
    for _, r in df.iterrows():
        blob = r.get("scraped_faq")
        if pd.isna(blob) or not str(blob).strip():
            continue
        for line in str(blob).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
                rows.append(
                    {
                        "state": r.get("state"),
                        "faq_question": j.get("question"),
                        "faq_answer": j.get("answer"),
                        "faq_source_url": j.get("source_url"),
                        "faq_fetched": j.get("fetched"),
                    }
                )
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)


# Canonical full-name -> 2-letter abbreviation (lowercase)
_STATE_MAP = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "district of columbia": "dc",
    "washington dc": "dc",
    "dc": "dc",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
    # US territories run Medicaid under capped block grants with their own
    # rules. They are recognized here so a resident's answer parses instead of
    # re-asking "what state do you live in?" forever; is_eligible then routes
    # them to their local agency rather than guessing with state rules.
    "puerto rico": "pr",
    "guam": "gu",
    "us virgin islands": "vi",
    "u.s. virgin islands": "vi",
    "virgin islands": "vi",
    "american samoa": "as",
    "northern mariana islands": "mp",
}

# Territory codes, kept out of the 50-state policy sets above.
_TERRITORY_CODES = frozenset({"pr", "gu", "vi", "as", "mp"})

# Helpful aliases/variants -> canonical full name
_ALIASES = {
    # DC variants
    "d.c.": "district of columbia",
    "wash dc": "washington dc",
    "wash. dc": "washington dc",
    "washington, dc": "washington dc",
    "w dc": "washington dc",
    # Abbrev-name shorthands & common variants
    "mass": "massachusetts",
    "penna": "pennsylvania",
    "penna.": "pennsylvania",
    "penn": "pennsylvania",
    "wash": "washington",
    "wash.": "washington",
    "calif": "california",
    "calif.": "california",
    "cal": "california",
    "ore": "oregon",
    "ore.": "oregon",
    "no dakota": "north dakota",
    "n dakota": "north dakota",
    "so dakota": "south dakota",
    "s dakota": "south dakota",
    "no carolina": "north carolina",
    "n carolina": "north carolina",
    "so carolina": "south carolina",
    "s carolina": "south carolina",
    # Directional punctuated forms
    "n. carolina": "north carolina",
    "s. carolina": "south carolina",
    "n. dakota": "north dakota",
    "s. dakota": "south dakota",
    # “state of …” forms
    "state of california": "california",
    "state of new york": "new york",
    "state of washington": "washington",
}

# State Medicaid program names. The prompt asks the model to infer the state
# from these; this is the backstop for when it passes the program name
# straight through as the "state".
#
# Kept separate from _ALIASES (and merged in below) so the fuzzy-exclusion
# set can be DERIVED from it rather than hand-copied. A second hand-kept
# list would drift, silently readmitting a program name to the fuzzy pool.
#
# Deliberately absent: a bare "medical". Pennsylvania, Minnesota and
# Maryland all call their programs "Medical Assistance", so users there say
# "Medical" and it must not resolve to California. Likewise "healthy
# connections", which names both South Carolina's and Idaho's programs --
# and they sit on opposite sides of the expansion line.
_PROGRAM_NAME_TO_STATE = {
    "medi-cal": "california",
    "denalicare": "alaska",
    "health first colorado": "colorado",
    "husky health": "connecticut",
    "diamond state health plan": "delaware",
    "med-quest": "hawaii",
    "medquest": "hawaii",
    "healthchoice illinois": "illinois",
    "hoosier healthwise": "indiana",
    "kansas medical assistance program": "kansas",
    "mainecare": "maine",
    "masshealth": "massachusetts",
    "mo healthnet": "missouri",
    "nj familycare": "new jersey",
    "turquoise care": "new mexico",
    "soonercare": "oklahoma",
    "tenncare": "tennessee",
    "star+plus": "texas",
    "star plus": "texas",
    "green mountain care": "vermont",
    "cardinal care": "virginia",
    "apple health": "washington",
    "forward health": "wisconsin",
    "forwardhealth": "wisconsin",
    "equality care": "wyoming",
}
_ALIASES.update(_PROGRAM_NAME_TO_STATE)

# Add 2-letter codes themselves as valid keys (so "CA"→"ca")
for full, abbr in list(_STATE_MAP.items()):
    _ALIASES[abbr] = full

# Program names are matched EXACTLY, never fuzzily: they are short and
# collide with ordinary words and with each other's typos, so fuzzy matching
# them silently scores one state's applicant under another's rules. A
# program name is either recognized exactly or it is not a state.
_PROGRAM_ALIASES = frozenset(_PROGRAM_NAME_TO_STATE)

# Precompute candidate keys (full names + aliases) for fuzzy matching
_CANDIDATE_KEYS = (set(_STATE_MAP.keys()) | set(_ALIASES.keys())) - _PROGRAM_ALIASES

# Reverse map: 2-letter code -> display name, derived from _STATE_MAP so the
# two never drift apart. Later duplicate full-name keys ("washington dc",
# "dc") overwrite earlier ones, so pin the DC display name explicitly.
_ABBR_TO_NAME = {abbr: full.title() for full, abbr in _STATE_MAP.items()}
_ABBR_TO_NAME["dc"] = "District of Columbia"

# ACA expansion states (40 states + DC per KFF; the ten that have not adopted
# expansion are AL, FL, GA, KS, MS, SC, TN, TX, WI, and WY). Policy data that
# changes: audit against KFF when updating.
_EXPANSION_STATES = frozenset(
    {
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sd",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "dc",
    }
)

# Non-expansion states whose waiver still covers childless adults up to 100%
# FPL (no coverage gap): currently just Wisconsin.
_WAIVER_100FPL_STATES = frozenset({"wi"})

# States with a medically-needy (spend-down) pathway.
_MEDICALLY_NEEDY_STATES = frozenset(
    {
        "ar",
        "ca",
        "ct",
        "dc",
        "fl",
        "ga",
        "hi",
        "il",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "mo",
        "mt",
        "ne",
        "nh",
        "nj",
        "ny",
        "nc",
        "nd",
        "pa",
        "ri",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
    }
)

# applying_reason values that mean a long-term-care application.
_LTC_REASONS = ("ltc_nursing_home", "ltc_home_care")

# Substrings that mean "this is a long-term-care application", however the
# model phrases it. Every other field here has tolerant parsing because the
# model does not reliably emit canonical tokens -- applying_reason had none,
# and an unrecognized value does not re-ask or go indeterminate. It silently
# routes to the MAGI/ABD branch, skipping the home-equity test entirely, and
# reports a nursing-home applicant as PROBABLY ELIGIBLE. A false positive is
# the worst answer this engine can give, so the matching is deliberately
# generous: over-matching costs an extra question, under-matching costs a
# wrong "yes". The branch treats the two LTC reasons identically, so a
# generic "long term care" can land on either.
_LTC_REASON_MARKERS = (
    "nursing home",
    "nursing facility",
    "nursing_home",
    "skilled nursing",
    "snf",
    "long term care",
    "long-term care",
    "longterm care",
    "ltc",
    "custodial care",
    "home care",
    "home-care",
    "home_care",
    "in-home care",
    "in home care",
    "home health",
    "personal care",
    "hcbs",
    "home and community based",
    "assisted living",
    "assisted_living",
)


def _normalize_applying_reason(raw: Any, living_situation: Any = None) -> str:
    """Map free-text application reasons onto the canonical LTC tokens.

    ``living_situation`` is consulted only as a fallback: if the model put
    the nursing-home fact there instead of in applying_reason, the LTC tests
    still need to run. Routing into the LTC branch is the conservative
    direction -- it applies MORE tests and returns indeterminate rather than
    eligible when information is missing.
    """
    for value in (raw, living_situation):
        if not isinstance(value, str):
            continue
        text = value.strip().lower()
        if not text:
            continue
        if text in _LTC_REASONS:
            return text
        if any(marker in text for marker in _LTC_REASON_MARKERS):
            return (
                "ltc_home_care"
                if ("home care" in text or "home-care" in text or "hcbs" in text)
                else "ltc_nursing_home"
            )
    return "standard"


# Stepwise questions raised from more than one branch. Shared constants keep
# every site byte-identical so the result dedup can collapse repeats
# (divergent phrasings previously got the same question asked twice).
_Q_ASSETS = "About how much are your countable financial assets (not including your primary home)?"
_Q_HOME_OWNER = "Do you own a home?"
_Q_HOME_EQUITY = "If you own a home, about how much equity do you have in it?"
_Q_LIVING_SITUATION = (
    "Where are you living now (home, assisted living, rehab, nursing home)?"
)
_Q_SSDI_MONTHS = "How many months have you been receiving SSDI?"
_Q_ESRD = "Are you in end stage renal failure?"
_Q_ALS = "Do you have ALS?"

# String truthiness for LLM-supplied "booleans": tool payloads are raw LLM
# JSON, which sometimes carries "false"/"no" instead of JSON booleans -- and
# bool("false") is True. The vocabulary covers how people actually answer in
# chat ("nope", "single") because an unrecognized string reads as unanswered,
# and an unanswered required field re-asks the same question forever. An
# empty string is explicitly NOT false: it is the most likely "I don't have
# this value" placeholder, so it must re-ask rather than record a "no".
_TRUE_STRINGS = frozenset(
    {
        "true",
        "yes",
        "y",
        "1",
        "on",
        "t",
        "yeah",
        "yep",
        "yup",
        "correct",
        "married",
        "i am",
        "i do",
    }
)
_FALSE_STRINGS = frozenset(
    {
        "false",
        "no",
        "n",
        "0",
        "off",
        "f",
        "nope",
        "nah",
        "single",
        "unmarried",
        "not married",
        "none",
        "never",
        "i am not",
        "i'm not",
        # Marital-status answers to "Are you married or single?". Without
        # these the question is re-asked every turn, since the answer parses
        # to None (unanswered) rather than to "not currently married".
        "widowed",
        "divorced",
        "separated",
    }
)

# How the model tells us a question was asked and cannot be answered ("I
# don't know my countable assets", "prefer not to say"). Without this channel
# the model has nothing valid to send, so the same question comes back every
# turn and the conversation never terminates. See _DECLINE_DEFAULTS.
_UNKNOWN_STRINGS = frozenset(
    {
        "unknown",
        "unsure",
        "not sure",
        "dont know",
        "don't know",
        "do not know",
        "no idea",
        "idk",
        "n/a",
        "na",
        "declined",
        "prefer not to say",
        "no answer",
        "unanswered",
        "maybe",
    }
)

# Fields we can safely assume when the user can't answer, so one unknown
# doesn't sink the whole estimate. Each assumption is the conservative one
# (single filer's lower asset limit, no exemptions) and is disclosed in the
# alternatives so the user can correct it.
_DECLINE_DEFAULTS: Dict[str, Any] = {
    "married": False,
    "pregnant": False,
    "children_in_household": 0,
    "receiving_ssdi": False,
    "disabled": False,
    "on_medicare": False,
    "esrd": False,
    "als": False,
    # NOTE: home_owner is deliberately NOT here. Assuming "no home" skips the
    # LTC home-equity test entirely, so someone with equity over the cap
    # would get a false "probably eligible" -- the opposite of conservative.
    # Left indeterminate instead, so it is asked or reported as undetermined.
    "veteran_or_spouse_of_veteran": False,
    "work_req_exempt_2026": False,
    "work_req_exempt": False,
}

# ---------------------------------------------------------------------------
# Which years we score
# ---------------------------------------------------------------------------
# Every estimate covers two years: the base year (the published FPL table we
# actually have, scored under today's rules) and a target year the caller can
# choose. The target defaults to the first year the federal work requirement
# can bite, but a user asking "what about 2028?" gets that year instead, with
# the work overlay applied.
#
# Income thresholds are NOT projected forward. We used to roll the FPL table
# up ~3%/year for future targets, which was wrong twice over: it moved the
# threshold while leaving the person's income fixed, so it biased everyone
# toward "more eligible later" on a number we were guessing anyway (real
# incomes rise too, and largely cancel it). And because the projection only
# ran for a year the user NAMED, the same person and the same calendar year
# came out differently depending on how they asked -- $1,810/mo in CA was
# "may not be eligible" on the default 2026 path and "could be eligible" when
# they typed "what about 2026?". The work requirement is the difference
# between the two years that we can actually speak to.
#
# This is the year of the PUBLISHED FPL TABLE, not "now" -- once the calendar
# moves past it we keep scoring against the same table (it's the newest one
# published) but we stop calling it the current year. Use
# ``current_eligibility_year`` for anything a user reads as "today".
#
# Moves with the dollar figures in ``fpl_annual_base``. Leaving it behind
# while HHS publishes a newer table doesn't just mislabel the answer, it
# scores people against limits that have been superseded -- and the limits
# only ever go up, so a stale table denies people who now qualify.
BASE_ELIGIBILITY_YEAR = 2026

# States must implement the federal work/community-engagement requirement no
# later than January 1, 2027; a few started earlier, which is why the overlay
# is modelled from 2026 on rather than 2027.
WORK_REQUIREMENT_FIRST_YEAR = 2026

# ...but "a few started earlier" is not "everyone has started", and we don't
# track which states went early. Between these two years the requirement is
# real for some people and not yet in force for most, so a shortfall in that
# window is reported as CONDITIONAL rather than as a denial: we say it may
# already apply where they live and will apply everywhere from this year.
#
# Getting this wrong is asymmetric. Telling someone "you may not be eligible"
# for a rule their state hasn't adopted can stop them applying at all, which
# is the failure this whole product exists to undo. Telling someone they may
# be eligible when the rule has already reached them costs them an
# application the state will decide on anyway.
WORK_REQUIREMENT_UNIVERSAL_YEAR = 2027

DEFAULT_TARGET_YEAR = WORK_REQUIREMENT_FIRST_YEAR

# Past this we would be projecting policy further out than anything we know
# can support, so a target beyond it is clamped (and the clamp is reported
# back through summarize_eligibility_inputs).
MAX_TARGET_YEAR = BASE_ELIGIBILITY_YEAR + 10


def current_eligibility_year() -> int:
    """The year to present as "today" in a verdict.

    Kept separate from ``BASE_ELIGIBILITY_YEAR``, which names the published
    FPL table we score against. The two were the same thing until the
    calendar moved past the table, at which point labelling every answer
    "current (2025)" -- and printing a verdict row for a year that had
    already ended -- was telling people about rules that were no longer the
    ones they live under.

    Floored at the table year (we can't score anything earlier) and capped at
    ``MAX_TARGET_YEAR`` so a wildly wrong system clock can't push the whole
    timeline past what we model.
    """
    return min(max(BASE_ELIGIBILITY_YEAR, date.today().year), MAX_TARGET_YEAR)


# Below this a number isn't a year at all, it's whatever _parse_numeric found
# in the sentence ("in 3 years", "for the next 5 years").
_MIN_PLAUSIBLE_YEAR = 1900

# Percent-of-FPL ceilings by category. Module scope because the stepwise
# question block consults them to skip questions that cannot change the
# answer, and it runs before the category chain.
THRESH_ADULT_MAGI = 138.0
THRESH_CHILD = 200.0
THRESH_PREG = 200.0

# Without these there is no estimate to give, so a decline here stops the
# flow with an explanation instead of a silent "not eligible".
_REQUIRED_FOR_ESTIMATE = ("state", "age", "household_size", "monthly_income")

# Questions that feed the Medicare determination. Medicare is federal and
# operates in the territories, so these survive the territory short-circuit
# while the Medicaid-only questions are dropped.
_MEDICARE_QUESTION_FIELDS = frozenset(
    {
        "age",
        "on_medicare",
        "years_worked",
        "receiving_ssdi",
        "ssdi_length",
        "esrd",
        "als",
    }
)

# Every kwarg is_eligible reads. Anything else the model sends is silently
# ignored today, which looks to the model like the answer was accepted -- so
# summarize_eligibility_inputs reports the strays back to it.
_KNOWN_ELIGIBILITY_FIELDS = frozenset(
    {
        "state",
        "married",
        "age",
        "pregnant",
        "receiving_ssdi",
        "disabled",
        "ssdi_length",
        "on_medicare",
        "veteran_or_spouse_of_veteran",
        "living_situation",
        "applying_reason",
        "household_size",
        "monthly_income",
        "assets_total",
        "home_owner",
        "home_equity",
        "children_in_household",
        "als",
        "esrd",
        "years_worked",
        "on_medicaid_past",
        "work_req_exempt_2026",
        # Same answer under a year-neutral name, since the target year is now
        # the caller's choice; both spellings are accepted.
        "work_req_exempt",
        "target_year",
        "qualifying_hours_weekly_last_12",
        "avg_monthly_qualifying_hours_last_3mo",
        "avg_weekly_qualifying_hours_last_3mo",
        "total_qualifying_hours_last_3mo",
    }
)


def _parse_bool(value: Any) -> Optional[bool]:
    """Tri-state bool out of an LLM-supplied value (None = unanswered)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower().rstrip(".!").strip()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        return None
    return bool(value)


def _is_unknown_marker(value: Any) -> bool:
    """Whether a value means "asked, but the user couldn't answer"."""
    if not isinstance(value, str):
        return False
    return value.strip().lower().rstrip(".!?").strip() in _UNKNOWN_STRINGS


# Word forms for the small counts people spell out ("three people").
_WORD_NUMBERS = {
    "zero": 0,
    "none": 0,
    # Natural answers to "About how much is your household's monthly income?"
    # from someone with no income. monthly_income is in _REQUIRED_FOR_ESTIMATE
    # so it cannot be defaulted away -- leaving these unparsed re-asked the
    # same question every turn. "unemployed" is deliberately absent: it is a
    # job status, not an amount, and unemployment benefits are income.
    "no income": 0,
    "zero income": 0,
    "nothing": 0,
    "none at all": 0,
    "no money": 0,
    "nil": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _parse_numeric(value: Any) -> Optional[float]:
    """Best-effort number out of an LLM-supplied value.

    Chat answers arrive as "$1,200", "1200/month", "about 1200", or "three",
    and a bare float() on those raises -- which the callers turn into None,
    i.e. "unanswered", so the same question is asked again every turn. Strip
    the currency/units decoration instead. Returns None only when there is
    genuinely no number to find.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[text])
    # Keep the first number-like run, dropping $ , and any trailing units.
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?\s*([km])?\b", text)
    if not match:
        return None
    try:
        number = float(match.group(0).rstrip("km ").replace(",", ""))
    except ValueError:
        return None
    suffix = match.group(1)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return number


def _parse_target_year(value: Any) -> Optional[int]:
    """Parse a caller-supplied target year, or None when there isn't one.

    Accepts what an LLM actually sends for "what about 2027?": an int, a
    string, or a phrase with the year in it. Two-digit years ("26") are read
    as this century, and anything outside the range we can model is clamped
    rather than rejected -- a clamped year still produces a usable estimate,
    where a rejection would drop the question the user asked.
    """
    number = _parse_numeric(value)
    if number is None:
        return None
    year = int(number)
    if 0 <= year < 100:
        # A two-digit year, but only if it can BE one: "26" is 2026, while
        # the 3 that _parse_numeric pulls out of "in 3 years" is not 2003.
        # Reading it as one used to clamp to the base year, which quietly
        # switched the work-requirement overlay off for a user who was
        # asking about a year the overlay applies to.
        year += 2000
        if year < BASE_ELIGIBILITY_YEAR:
            return None
    elif year < _MIN_PLAUSIBLE_YEAR:
        return None
    if year < BASE_ELIGIBILITY_YEAR:
        return BASE_ELIGIBILITY_YEAR
    if year > MAX_TARGET_YEAR:
        return MAX_TARGET_YEAR
    return year


def resolve_target_year(value: Any) -> int:
    """The year the second half of an estimate is scored for.

    Shared by is_eligible and the chat tool so the verdict and the label the
    user sees can't disagree about which year was checked.
    """
    return _parse_target_year(value) or DEFAULT_TARGET_YEAR


def timeline_years(value: Any = None) -> List[int]:
    """The years an estimate should show a verdict for, ascending.

    Always at least two, and always including the year the answer can CHANGE.
    Showing only "today" and "the year you asked about" hid the transition:
    someone asking about 2029 saw 2025 and 2029 and never learned that the
    switch happens in 2026, when the work requirement starts to bite.

    Income limits are identical across years (see BASE_ELIGIBILITY_YEAR), so
    the work requirement is the only thing that can move a verdict -- which
    makes ``WORK_REQUIREMENT_FIRST_YEAR`` the one year that earns a row
    alongside today's rules and whatever the user asked about, for as long as
    it is still ahead of us. Once it isn't, it's just today's rules and there
    is nothing to flag.

    Anchored on ``current_eligibility_year``, not the FPL table's year: a row
    for a year that has already ended is noise at best, and labelled
    "current" it is wrong.

    Never a single year. Once the work-requirement year is behind us the set
    above can collapse to just "now", and "will this still be true next year?"
    is the question people are actually asking -- so next year gets a row and
    answers it out loud instead of leaving it implied.
    """
    current = current_eligibility_year()
    years = {current, resolve_target_year(value)}
    for milestone in (WORK_REQUIREMENT_FIRST_YEAR, WORK_REQUIREMENT_UNIVERSAL_YEAR):
        if milestone > current:
            years.add(milestone)
    if len(years) == 1:
        years.add(min(current + 1, MAX_TARGET_YEAR))
    return sorted(years)


class YearVerdict(NamedTuple):
    """One row of an eligibility timeline.

    A NamedTuple so rows still unpack and compare like the plain tuples they
    replaced, while the two things that are NOT verdicts stay named -- both
    were silently lost when this was a ``(year, bool)`` pair, and both turned
    into denials nobody computed.
    """

    year: int
    probably_eligible: bool
    # Non-empty means this year is UNSCORED, not negative.
    still_needed: List[str]
    # True means the positive above rests on a work requirement that isn't in
    # force everywhere yet -- report it as conditional, not as a clean pass.
    work_requirement_conditional: bool = False


def eligibility_timeline(**kwargs: Any) -> List[YearVerdict]:
    """A ``YearVerdict`` for each year in ``timeline_years``.

    Runs the checker once per year so a year-over-year change is visible
    rather than implied. ``is_eligible`` is pure and does no IO, so the
    repeat calls are cheap.

    The second element of ``is_eligible``'s result is the right per-year
    verdict for EVERY year, including the base year: the work overlay is
    skipped below WORK_REQUIREMENT_FIRST_YEAR, so it collapses to the
    base-year verdict there.

    The third element is what that year still needs, and dropping it was a
    bug worth naming: ``is_eligible`` uses ``False`` for BOTH "we scored this
    and they fall short" and "we can't score this until you answer" -- an
    unanswered work-hours question sets the target-year flag False on
    purpose, because the questions ride alongside it. A caller who keeps only
    the boolean turns "we don't know yet" into "you may not be eligible",
    which is a denial nobody computed. Years that still need answers carry
    them here so the renderer can ask instead of concluding.
    """
    timeline: List[YearVerdict] = []
    for year in timeline_years(kwargs.get("target_year")):
        (
            _,
            probably_eligible,
            _,
            _,
            missing,
            determination_made,
            work_req_conditional,
        ) = _score_eligibility(**{**kwargs, "target_year": year})
        still_needed = list(missing) if not determination_made or missing else []
        timeline.append(
            YearVerdict(
                year=year,
                probably_eligible=bool(probably_eligible),
                still_needed=still_needed,
                work_requirement_conditional=bool(work_req_conditional),
            )
        )
    return timeline


def _clean_token(s: str) -> str:
    """Lower, trim, collapse spaces, remove most punctuation except spaces."""
    s = s.strip().lower()
    # replace common separators with spaces
    s = re.sub(r"[,_/]+", " ", s)
    # remove periods
    s = s.replace(".", "")
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    # normalize things like 'st of' -> 'state of'
    if s.startswith("state of "):
        return s
    return s


def _canonicalize(s: str) -> str:
    """Map aliases to canonical full names where possible."""
    if s in _ALIASES:
        return _ALIASES[s]
    return s


def _normalize_state(
    state: Optional[str], *, fuzzy: bool = True, cutoff: float = 0.84
) -> Optional[str]:
    """Normalize a US state (full name, alias, or 2-letter code) to lowercase 2-letter code.

    Args:
        state: State input (any case). Handles punctuation, spaces, and common variants.
        fuzzy: If True, use difflib to match close misspellings/variants.
        cutoff: Similarity threshold (0..1). Higher is stricter.

    Returns:
        Two-letter lowercase postal abbreviation.

    Raises:
        ValueError: If the input cannot be confidently mapped.
    """
    if state is None:
        return None
    if not isinstance(state, str):
        raise ValueError(f"State must be a string, got {type(state).__name__}")

    # Special case since our LLM sometimes uses the example too literally.
    if state == "StateName" or state == "statename" or state == "unknown":
        return None

    raw = state
    s = _clean_token(state)

    # Exact short-circuit: if already a 2-letter valid code (any case/punct)
    if len(s) == 2 and s in {v for v in _STATE_MAP.values()}:
        return s

    # Try alias/canonical exact match
    s = _canonicalize(s)
    if s in _STATE_MAP:
        return _STATE_MAP[s]

    # Try to expand common “state of X”
    if s.startswith("state of "):
        candidate = s[len("state of ") :]
        candidate = _canonicalize(candidate)
        if candidate in _STATE_MAP:
            return _STATE_MAP[candidate]

    if not fuzzy:
        raise ValueError(f"Unknown state: {raw}")

    # Fuzzy search across candidate keys
    # We compare against cleaned candidate keys; if alias matched, map to canonical.
    matches = difflib.get_close_matches(s, _CANDIDATE_KEYS, n=3, cutoff=cutoff)
    for m in matches:
        # Map alias->canonical full name, then to abbr
        canon = _canonicalize(m)
        if canon in _STATE_MAP:
            return _STATE_MAP[canon]

    # Extra: attempt fuzzy on full names only (avoids weird alias bias)
    full_name_matches = difflib.get_close_matches(
        s, list(_STATE_MAP.keys()), n=1, cutoff=cutoff
    )
    if full_name_matches:
        return _STATE_MAP[full_name_matches[0]]

    raise ValueError(f"Unknown state: {raw}")


def is_eligible(**kwargs) -> Tuple[bool, bool, bool, List[str], List[str], bool]:
    """Approximate Medicaid / Medicare eligibility. See ``_score_eligibility``.

    Thin wrapper so the widely-used six-tuple stays the public shape. The
    seventh value the scorer computes -- whether the target year's verdict
    hangs on a work requirement that isn't in force everywhere yet -- is only
    meaningful to ``eligibility_timeline``, which calls the scorer directly.
    """
    return _score_eligibility(**kwargs)[:6]


def _score_eligibility(
    **kwargs,
) -> Tuple[bool, bool, bool, List[str], List[str], bool, bool]:
    """
    Perform an approximate eligibility check for Medicaid / Medicare based on the provided parameters.
    Returns a tuple of (base-year eligibility, target-year eligibility,
    medicare, alternatives, missing_info, determination_made,
    work_requirement_conditional). Most callers want ``is_eligible``, which
    drops the last value.

    The base year is ``BASE_ELIGIBILITY_YEAR`` (the published FPL table we
    score today's rules against). The target year is whatever the caller
    passes as ``target_year`` -- a user asking "will I still qualify in
    2028?" gets 2028 -- and defaults to ``DEFAULT_TARGET_YEAR``. Callers must
    resolve the same year with ``resolve_target_year`` when labelling the
    result, so the verdict and the label can't disagree.

    IMPORTANT: This uses simplified heuristics. Medicaid rules vary by state and change often.
    Treat results as a best guess only and confirm with state resources.

    Federal work/community engagement requirement:
      - Per the CMS interim final rule (CMS-2454-IFC, June 2026), states must
        implement the requirement no later than JANUARY 1, 2027; a few have
        gone earlier, and states showing good-faith effort can get extensions.
        The target-year flag therefore models "eligibility once the work
        requirement applies to them" for any target year from
        ``WORK_REQUIREMENT_FIRST_YEAR`` on, not calendar-year 2026
        specifically. A target year before that is scored without the
        overlay.
      - The requirement is 80 qualifying hours PER MONTH, checked at
        application and renewal against a lookback of one to three months
        (states choose), which is why late-2026 activity can already matter.
      - Qualifying hours may include work, school, volunteering, or caregiving.
      - Some people may be exempt (pregnant, disabled/SSDI/medically frail, etc.). If unsure, we ask.

    Expected kwargs (all optional; function will ask for missing, step-by-step):
      - state: str
      - married: bool
      - age: int
      - pregnant: bool
      - receiving_ssdi: bool  (or disabled: bool)
      - on_medicare: bool
      - veteran_or_spouse_of_veteran: bool
      - living_situation: str     # "home", "friends", "assisted_living", "rehab_temp", "nursing_home_perm"
      - applying_reason: str       # "standard" (MAGI), "ltc_nursing_home", "ltc_home_care"
      - household_size: int
      - monthly_income: float      # MAGI-ish for MAGI; gross for ABD/LTC high-level
      - assets_total: float        # exclude primary home equity if possible
      - home_owner: bool
      - home_equity: float
      - children_in_household: int
      - als: bool
      - esrd: bool
      - ssdi_length: int # how many months have they been receiving ssdi
      - years_worked: int # years you (or spouse/ex-spouse) worked paying medicare taxes
      - on_medicaid_past: bool  # accepted for backwards compatibility; unused

      - target_year: int           # year to score the second verdict for (default DEFAULT_TARGET_YEAR)

      # federal work requirement (ALWAYS ASSUMED TRUE):
      - work_req_exempt_2026: Optional[bool]  # if caller knows the person is exempt from work rules
      - work_req_exempt: Optional[bool]       # year-neutral spelling of the same answer
      - qualifying_hours_weekly_last_12: Optional[Sequence[float]]  # 12 numbers, one per week
      - avg_monthly_qualifying_hours_last_3mo: Optional[float]      # matches how the re-ask question is phrased
      - avg_weekly_qualifying_hours_last_3mo: Optional[float]       # fallback if weekly list not available
      - total_qualifying_hours_last_3mo: Optional[float]            # fallback if neither weekly nor avg provided

    Heuristics (approx):
      - MAGI Adults (19–64) in expansion states: <= 138% FPL.
      - Children: <= 200% FPL (often higher) — otherwise suggest CHIP.
      - Pregnant: <= 200% FPL (often higher).
      - ABD & LTC: asset limits approx $2k single / $3k married; LTC income cap ~ $3,000/mo;
        home equity must be below a default cap (use $750k if unknown). Medically-needy may help.
      - Work overlay (target years from WORK_REQUIREMENT_FIRST_YEAR on):
        requires >=80 qualifying hours per month across a
        3-month lookback. Detailed weekly records are partitioned per month so
        each month is checked on its own; a single monthly average or a 3-month
        total has no per-month detail to check, so it is spread evenly and only
        the average can be tested.
      - Questions we would never ask (e.g. "are you on Medicare?" for a healthy
        30-year-old, or "are you pregnant?" for a toddler) are defaulted instead of
        stalling the determination waiting on an answer that will never come.

    Returns:
      (eligible_base_year, eligible_target_year, eligible_medicare,
       alternatives, missing_info, determination_made)

    ``determination_made`` is False when we could not score the person at all
    (US territory, or a required answer the user declined) — distinct from a
    scored "not eligible". Callers must not present the former as a verdict.
    """

    # ---- helpers ----
    def get_bool(name: str) -> Optional[bool]:
        # LLM payloads sometimes carry "false"/"no" string booleans, and
        # bool("false") is True -- see _parse_bool / _TRUE_STRINGS.
        return _parse_bool(kwargs.get(name, None))

    def get_int(name: str) -> Optional[int]:
        parsed = _parse_numeric(kwargs.get(name, None))
        return int(parsed) if parsed is not None else None

    def get_float(name: str) -> Optional[float]:
        return _parse_numeric(kwargs.get(name, None))

    def get_seq_of_floats(name: str) -> Optional[List[float]]:
        v = kwargs.get(name, None)
        if v is None:
            return None
        try:
            return [float(x) for x in v]
        except Exception:
            return None

    # The year the caller wants the second verdict for. Anything we can't
    # read falls back to the default rather than failing the whole check;
    # summarize_eligibility_inputs reports what we actually used.
    target_year = resolve_target_year(kwargs.get("target_year"))

    # FPL table (annual, 48 contiguous states + DC published guidelines:
    # $15,960 for one person plus $5,680 per additional person -- the 2026 HHS
    # poverty guidelines, which is what BASE_ELIGIBILITY_YEAR names). Alaska
    # and Hawaii run higher; we keep the contiguous values as the
    # approximation. We'll work monthly: divide by 12.
    #
    # The SAME published table scores every year -- see the note on
    # BASE_ELIGIBILITY_YEAR for why future years are not projected forward.
    # When HHS publishes the next year's guidelines, both numbers here and
    # BASE_ELIGIBILITY_YEAR move together; they are one fact in two places.
    def fpl_annual_base(hh: int) -> float:
        if hh <= 0:
            hh = 1
        base = 15960.0
        add = 5680.0
        return base + add * (hh - 1)

    def pct_fpl(monthly_income: float, hh: int) -> float:
        annual = monthly_income * 12.0
        fpl = fpl_annual_base(hh)
        return (annual / fpl) * 100.0 if fpl > 0 else 9999.0

    # Policy knobs / defaults
    LTC_INCOME_CAP = 3000.0  # rough nursing home / HCBS cap (varies by state/waiver)
    ABD_ASSET_LIMIT_SINGLE = 2000.0
    ABD_ASSET_LIMIT_MARRIED = 3000.0
    HOME_EQUITY_CAP_DEFAULT = 750000.0

    # Federal work requirement knobs: 80 hours per month over 3-month lookback
    WORK_REQ_MONTHLY_HOURS = 80.0  # required qualifying hours per month
    # Note: this is a little fuzzy at renewal some states _could_ look at one month back
    # but they could also do three. Yaaay.
    REQUIRED_MONTHS = 3  # lookback period in months
    MIN_MONTHS_MEETING = 3  # require meeting monthly hours in at least this many months

    # ---- honour "the user couldn't answer" markers ----
    # The model parses free text and hands us values; when the user simply
    # doesn't know, it has nothing valid to send, so without this the same
    # question returns every turn forever. Fold the safe assumptions in and
    # remember the rest so their questions are suppressed below.
    declined_fields = sorted(
        name for name, raw in kwargs.items() if _is_unknown_marker(raw)
    )
    assumed_fields: List[str] = []
    # Field name behind each queued question, parallel to ``missing`` (every
    # append goes through ask()). Lets the territory branch keep the
    # questions that can still produce an answer and drop the rest.
    asked_fields: List[str] = []
    # Categories we could not score because the answer they need was
    # declined. Unlike _REQUIRED_FOR_ESTIMATE (which stops the flow), these
    # let the rest of the estimate stand while keeping determination_made
    # False, so the chat tool reports "not scored" instead of "not eligible".
    indeterminate: List[str] = []
    if declined_fields:
        kwargs = dict(kwargs)
        for name in declined_fields:
            if name in _DECLINE_DEFAULTS:
                kwargs[name] = _DECLINE_DEFAULTS[name]
                assumed_fields.append(name)
            else:
                # No safe assumption: drop it so extraction sees "unanswered",
                # and ask() below keeps us from re-asking it.
                kwargs.pop(name, None)
    declined_set = set(declined_fields)

    # ---- extract inputs ----
    # A state we can't recognize (typo, garbled LLM value) becomes a re-ask
    # instead of an exception that kills the whole eligibility check.
    try:
        state = _normalize_state(kwargs.get("state"))
    except ValueError:
        state = None
    married = get_bool("married")
    age = get_int("age")
    pregnant = get_bool("pregnant")
    # receiving_ssdi and disabled are alternative spellings of the same
    # answer: True from either wins (a non-SSDI disabled person stays on the
    # disability pathway even when they also answered "no" to the SSDI
    # question); an explicit False is preserved so a "no" isn't re-asked
    # forever (the old `or` chain coerced False back to None); None only when
    # neither was provided.
    ssdi_answer = get_bool("receiving_ssdi")
    disabled_answer = get_bool("disabled")
    receiving_ssdi: Optional[bool]
    if ssdi_answer is None and disabled_answer is None:
        receiving_ssdi = None
    else:
        receiving_ssdi = bool(ssdi_answer) or bool(disabled_answer)
    ssdi_length = get_int("ssdi_length")
    on_medicare = get_bool("on_medicare")
    veteran = get_bool("veteran_or_spouse_of_veteran")
    living_situation = kwargs.get("living_situation")
    applying_reason = _normalize_applying_reason(
        kwargs.get("applying_reason"), living_situation
    )
    household_size = get_int("household_size")
    monthly_income = get_float("monthly_income")
    assets_total = get_float("assets_total")
    home_owner = get_bool("home_owner")
    home_equity = get_float("home_equity")
    kids = get_int("children_in_household")
    medically_needy = state in _MEDICALLY_NEEDY_STATES
    als = get_bool("als")
    esrd = get_bool("esrd")
    years_worked = get_int("years_worked")
    # on_medicaid_past is still accepted in kwargs for backwards compatibility
    # but no longer changes the result: past Medicaid enrollment does not by
    # itself confer Medicare eligibility.

    # Work requirement inputs. Either spelling of the exemption answer is
    # accepted; an explicit True from either one wins.
    work_req_exempt = get_bool("work_req_exempt_2026")
    if work_req_exempt is not True:
        year_neutral_exempt = get_bool("work_req_exempt")
        if year_neutral_exempt is not None:
            work_req_exempt = bool(work_req_exempt) or year_neutral_exempt
    weekly_hours = get_seq_of_floats("qualifying_hours_weekly_last_12")
    avg_weekly_hours = get_float("avg_weekly_qualifying_hours_last_3mo")
    avg_monthly_hours = get_float("avg_monthly_qualifying_hours_last_3mo")
    total_hours_3mo = get_float("total_qualifying_hours_last_3mo")
    if total_hours_3mo is None and avg_monthly_hours is not None:
        # The re-ask question is phrased per month, so accept the answer in
        # the same units.
        total_hours_3mo = avg_monthly_hours * 3.0
    if total_hours_3mo is None and avg_weekly_hours is not None:
        # 13 weeks per quarter, not 12: a 4-weeks-per-month conversion
        # undercounts real calendar months by ~8%, enough to flip verdicts
        # right at the 80-hour line.
        total_hours_3mo = avg_weekly_hours * 13.0

    # ---- track outputs ----
    missing: List[str] = []
    alts: List[str] = []
    eligible_base = False
    eligible_target = False
    eligible_medicare = False

    def ask(field: str, question: str) -> bool:
        """Queue a question unless the user already declined that field.

        Returns True when the question was actually queued. Callers that
        would otherwise fall straight through to a verdict need that
        distinction: a declined field means "we could not score this", not
        "the answer is no".
        """
        if field in declined_set:
            return False
        missing.append(question)
        asked_fields.append(field)
        return True

    # Set when the target year's positive verdict rests on a work requirement
    # that is in force in some states and not others (see
    # WORK_REQUIREMENT_UNIVERSAL_YEAR). Not a denial and not a clean pass.
    # Initialised here rather than beside the overlay: the early exits below
    # (territory, declined required inputs) reach _result long before the
    # overlay runs.
    work_req_conditional = False

    def _result(
        determination_made: bool = False,
    ) -> Tuple[bool, bool, bool, List[str], List[str], bool, bool]:
        """Single exit point: casts + dedupes so every return stays consistent.

        ``determination_made`` separates "we scored this person and the answer
        is no" from "we couldn't score them at all" (a US territory, or a
        required answer the user declined). Both used to come back as
        False/False with no questions left, which the chat tool rendered as a
        confident "may not be eligible".
        """
        return (
            bool(eligible_base),
            bool(eligible_target),
            bool(eligible_medicare),
            list(dict.fromkeys(alts)),
            list(dict.fromkeys(missing)),
            determination_made,
            bool(work_req_conditional),
        )

    def magi_expansion_covers(*, require_inputs: bool = False) -> bool:
        """Whether ACA expansion covers this adult on income alone.

        Expansion (and the 100%-FPL waiver states) apply NO asset test, so
        wherever this is true the asset question cannot change the verdict.
        Shared by the stepwise block, which uses it to skip asking, and by
        the ABD branch, which uses it to score -- keeping the question we
        suppress and the answer we give from disagreeing.

        ``require_inputs`` guards the stepwise call, which runs before the
        completeness gate and so may not have income or household size yet.
        """
        if age is None or not (19 <= age <= 64):
            return False
        if monthly_income is None or household_size is None:
            if require_inputs:
                return False
            raise AssertionError("scoring call needs income and household size")
        pct = pct_fpl(monthly_income, household_size)
        if state in _EXPANSION_STATES and pct <= THRESH_ADULT_MAGI:
            return True
        return state in _WAIVER_100FPL_STATES and pct <= 100.0

    # ---- prioritize missing info for stepwise questioning ----
    if not state:
        ask("state", "What state do you live in?")
    if age is None:
        ask("age", "How old are you?")
    # No federal minimum marriage age law exists Oo. We could _probably_ go with 16 though but
    # for now lets do 10 since asking is not terrible and some states do allow it.
    if age is not None and age < 10 and married is None:
        married = False
    if age is not None and age >= 10 and married is None:
        ask("married", "Are you married or single?")
    if household_size is None:
        ask(
            "household_size",
            "How many people are in your household for taxes (household size)?",
        )
    if monthly_income is None:
        ask(
            "monthly_income",
            "About how much is your household's monthly income before taxes?",
        )

    # https://en.wikipedia.org/wiki/Lina_Medina :/ -- and at the other end, a
    # 95-year-old's determination should not sit blocked on this either.
    if age is not None and pregnant is None:
        if 4 < age <= 60:
            ask("pregnant", "Are you currently pregnant?")
        else:
            # Don't stall the determination on a question we'd never ask.
            pregnant = False
    if kids is None:
        ask(
            "children_in_household",
            "How many children (under 19) live in your household?",
        )

    if receiving_ssdi is None:
        ask(
            "receiving_ssdi",
            "Are you receiving SSDI or otherwise considered disabled for benefits?",
        )
    # Only ask about SSDI duration when SSDI itself was answered yes. Someone
    # who said "disabled, but not on SSDI" cannot answer it, and the prompt
    # forbids the model from inventing a value -- so asking anyway stalls the
    # conversation on a question with no possible answer.
    ssdi_length_pending = False
    if ssdi_answer and ssdi_length is None:
        # Needed at any age: 24+ months of SSDI is its own Medicare pathway,
        # so a 67-year-old on SSDI must be asked too, not just under-65s.
        ssdi_length_pending = ask("ssdi_length", _Q_SSDI_MONTHS)
    if age is not None and age < 65:
        # ESRD and ALS each open a Medicare pathway at any age, but they are
        # rare enough that asking every healthy young adult about kidney
        # failure burns two turns on answers that cannot change the result.
        # Ask only when there's already a disability/Medicare signal, and
        # ask both at once rather than serializing them across turns.
        settled_by_ssdi = ssdi_length is not None and ssdi_length >= 24
        if (bool(receiving_ssdi) or bool(on_medicare)) and not settled_by_ssdi:
            if esrd is None:
                ask("esrd", _Q_ESRD)
            if als is None:
                ask("als", _Q_ALS)
        else:
            if esrd is None:
                esrd = False
            if als is None:
                als = False

    # ---- Medicare pathway: enrolled, 65+, 24+ months of SSDI, ESRD, or ALS ----
    is_65_plus = age is not None and age >= 65
    has_esrd_or_als = bool(esrd) or bool(als)
    # Gate on ssdi_answer: ssdi_length means *SSDI* months, so a duration
    # supplied alongside a non-SSDI disability must not confer Medicare.
    ssdi_24_months = bool(ssdi_answer) and ssdi_length is not None and ssdi_length >= 24
    medicare_pathway = (
        bool(on_medicare) or is_65_plus or has_esrd_or_als or ssdi_24_months
    )
    if medicare_pathway:
        if on_medicare is None:
            ask("on_medicare", "Are you currently on Medicare?")
        if on_medicare:
            # Already enrolled (any age -- an affirmative answer alone is a
            # pathway, so an under-65 enrollee isn't told they're ineligible).
            eligible_medicare = True
        elif has_esrd_or_als:
            # ESRD and ALS have their own Medicare pathways regardless of age.
            eligible_medicare = True
        elif ssdi_24_months:
            # Medicare enrollment is automatic after 24 months of SSDI.
            eligible_medicare = True
        elif is_65_plus:
            if years_worked is None:
                if not ask(
                    "years_worked",
                    "How many years did you (or spouse or ex-spouse) work and pay medicare taxes?",
                ):
                    # Declined: we can't confirm premium-free Part A, but a
                    # 65+ applicant still has the buy-in and MSP pathways.
                    # Falling through silently left them with no Medicare
                    # answer AND no alternatives at all.
                    indeterminate.append("years_worked")
                    alts.append(
                        "You may be eligible to buy Medicare Part A even if you don't qualify for premium-free Part A."
                    )
                    alts.append(
                        "Medicare Savings Programs (QMB/SLMB/QI) can help pay Medicare premiums when income and assets are low."
                    )
            elif years_worked >= 10:
                # 10 years (40 quarters) of Medicare taxes = premium-free Part A.
                eligible_medicare = True
            elif ssdi_length_pending:
                # Don't rule Medicare out yet: 24+ months of SSDI would
                # qualify them and that question is still outstanding.
                #
                # Gate on whether the question was actually QUEUED, not on
                # the answers. Two ways to get this wrong, both of which
                # ended in a silent Medicare False with no question and no
                # alternatives: `receiving_ssdi` is ORed with `disabled`, so
                # "disabled, not on SSDI" waited on a question gated behind
                # the SSDI answer and never asked; and `ssdi_answer and
                # ssdi_length is None` still waited when the duration was
                # DECLINED, which suppresses the ask. Deferring only while
                # the ask is outstanding covers both.
                pass
            else:
                eligible_medicare = False
                alts.append(
                    "You may be eligible to buy Medicare Part A even if you don't qualify for premium-free Part A."
                )
                alts.append(
                    "Medicare Savings Programs (QMB/SLMB/QI) can help pay Medicare premiums when income and assets are low."
                )
    elif age is not None and on_medicare is None:
        # Under 65 with no disability pathway: almost never on Medicare, so
        # assume not enrolled instead of stalling the flow on a question
        # whose answer is essentially always "no".
        on_medicare = False

    if state in _TERRITORY_CODES:
        # Territories run Medicaid under capped block grants with their own
        # income rules, so the 50-state heuristics below don't apply. This
        # runs AFTER the Medicare pathway above on purpose: Medicare is
        # federal and does operate in PR/GU/VI/AS/MP, so a 67-year-old
        # resident still gets a real Medicare answer. The Medicaid side
        # comes back as not modeled (determination_made stays False) rather
        # than as a "not eligible" verdict.
        alts.append(
            "Medicaid in US territories follows different rules than the states"
            "—contact your territory's Medicaid agency for eligibility limits."
        )
        # Drop the Medicaid-only questions the stepwise block queued above: no
        # answer to them can produce a territory Medicaid estimate, so asking
        # spent several turns to arrive at the same "not modeled". Keep the
        # Medicare-relevant ones -- Medicare is federal and does operate here,
        # so those can still resolve into a real answer.
        keep = [
            question
            for field, question in zip(asked_fields, missing, strict=True)
            if field in _MEDICARE_QUESTION_FIELDS
        ]
        asked_fields[:] = [f for f in asked_fields if f in _MEDICARE_QUESTION_FIELDS]
        missing[:] = keep
        return _result()

    # LTC pathways. Note: living_situation is accepted as context but is not
    # asked for and does not gate the determination -- applying_reason already
    # carries the nursing-home vs home-care distinction, and gating on a value
    # nothing reads cost every LTC applicant an extra round trip.
    if applying_reason in _LTC_REASONS:
        if assets_total is None:
            ask("assets_total", _Q_ASSETS)
        if home_owner is None:
            ask("home_owner", _Q_HOME_OWNER)
        if home_owner and home_equity is None:
            ask("home_equity", _Q_HOME_EQUITY)
    else:
        # Only the ABD branch reads assets. Children and pregnancy are scored
        # on income alone and are matched earlier in the category chain, so
        # asking them for a number nothing consumes just costs a turn.
        abd_candidate = (
            (age is not None and age >= 65) or (receiving_ssdi is True)
        ) and not ((age is not None and age < 19) or pregnant is True)
        # Expansion applies no asset test, so for an adult already under the
        # threshold the answer cannot change the verdict -- don't spend a
        # turn collecting it.
        covered_without_assets = magi_expansion_covers(require_inputs=True)
        if abd_candidate and assets_total is None and not covered_without_assets:
            ask("assets_total", _Q_ASSETS)

    # mypy isn't smart enough to infer the types with a loop :/
    if (
        state is None
        or age is None
        or married is None
        or household_size is None
        or monthly_income is None
        or pregnant is None
        or kids is None
        or receiving_ssdi is None
        or on_medicare is None
    ):
        blocking = [f for f in _REQUIRED_FOR_ESTIMATE if f in declined_set]
        if blocking and not missing:
            # Everything answerable has been answered and what's left was
            # declined -- say so rather than returning a bare "not eligible".
            alts.append(
                "We can't estimate eligibility without "
                f"{', '.join(f.replace('_', ' ') for f in blocking)}"
                "—your state Medicaid agency can check without it."
            )
        return _result()

    # ---- with core info present, evaluate categories ----
    # One number for every year: the published FPL table is not projected
    # forward (see BASE_ELIGIBILITY_YEAR). The income test therefore cannot
    # differ between the base and target years -- the work overlay below is
    # the only thing that separates the two verdicts.
    pfpl_base = pct_fpl(monthly_income, household_size)

    # Set by the LTC branch below: that branch computes its own target-year verdict
    # (the LTC income cap differs by year), so the work overlay must not
    # recompute it -- but only when the branch actually ran.
    ltc_evaluated = False

    is_child = age < 19
    is_adult_magi = 19 <= age <= 64
    is_abd_age = age >= 65
    is_abd_disability = bool(receiving_ssdi)
    is_preg = bool(pregnant)

    # Category decisions → base-year eligibility (pre-work overlay)
    if is_child:
        eligible_base = pfpl_base <= THRESH_CHILD
        if not eligible_base:
            alts.append(
                "CHIP: Children may still qualify for CHIP at higher incomes than Medicaid."
            )
        # skip work overlay for children
        work_req_exempt = True
    elif is_preg:
        eligible_base = pfpl_base <= THRESH_PREG
    elif (
        is_abd_age or is_abd_disability or on_medicare
    ) and applying_reason not in _LTC_REASONS:
        # Someone explicitly applying for long-term care falls through to the
        # LTC branch below (LTC applicants are almost always 65+/disabled, so
        # without this carve-out they'd be evaluated under the wrong rules).
        def abd_alternatives() -> None:
            """Pathways worth naming when the ABD test does not come out yes."""
            if on_medicare:
                alts.append(
                    "Medicare Savings Programs (QMB/SLMB/QI) can help pay Part A/B premiums and cost-sharing."
                )
            if medically_needy:
                alts.append(
                    "Medically-needy/spend-down Medicaid may help if medical bills are high."
                )
            # No else: the ~17 states without a medically-needy program for
            # aged/disabled adults have nothing to ask about, and sending
            # someone to their agency for a program that doesn't exist there
            # wastes a call. The generic pointers below (state page,
            # navigator, appeal) still apply.

        if assets_total is None:
            if magi_expansion_covers():
                # Score it rather than asking: expansion applies no asset
                # test, so the answer is already determined. Treating this
                # as indeterminate on a declined asset figure was worse than
                # a wrong answer -- the same person who reported assets far
                # OVER the ABD limit came back eligible via this pathway,
                # while declining the question came back "we can't estimate".
                eligible_base = True
            elif not ask("assets_total", _Q_ASSETS):
                # Declined, and no asset-free pathway applies: an asset test
                # we cannot run is not a failed asset test. Without this the
                # else-arm never ran, so a 66-year-old at 69% FPL who simply
                # didn't know their asset total got a flat "may not be
                # eligible" and none of the pathways below.
                indeterminate.append("assets_total")
                abd_alternatives()
        else:
            asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE
            assets_ok = assets_total <= asset_limit
            # NOTE: medically_needy is a spend-down PATHWAY, not an income
            # waiver -- treating it as one reported six-figure earners as
            # "probably eligible". It stays a suggestion below instead.
            income_ok_base = pfpl_base <= 100.0
            eligible_base = assets_ok and income_ok_base
            if not eligible_base:
                eligible_base = magi_expansion_covers()
            if not eligible_base:
                abd_alternatives()
    elif applying_reason in _LTC_REASONS:
        ltc_evaluated = True
        # The stepwise block above already queued these questions; here we
        # only decide whether we can score yet.
        if (
            assets_total is None
            or home_owner is None
            or (home_owner and home_equity is None)
        ):
            # Keep any Medicare verdict and alternatives accumulated above --
            # the old early return here clobbered eligible_medicare back to
            # False and threw away the Part-A/MSP suggestions.
            alts.append(
                "If not eligible, ask about HCBS waivers or medically-needy/spend-down."
            )
            return _result()

        asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE

        # assets_total is narrowed by the guard above; zero assets passes.
        assets_ok = assets_total <= asset_limit
        home_ok = True
        if home_owner:
            home_ok = (home_equity or 0.0) <= HOME_EQUITY_CAP_DEFAULT
        income_ok_base = monthly_income <= LTC_INCOME_CAP
        eligible_base = assets_ok and home_ok and income_ok_base
        eligible_target = eligible_base
        if not income_ok_base:
            alts.append(
                "Ask about a Qualified Income Trust (Miller trust) if income is just over the LTC cap."
            )
        if not assets_ok:
            alts.append(
                "Talk to an elder-law professional about spend-down and exempt resources for LTC Medicaid."
            )
        if not home_ok:
            alts.append(
                "Home equity may exceed state limits—ask about exceptions, liens, or planning options."
            )
        if medically_needy:
            alts.append(
                "Medically-needy/spend-down Medicaid may help if bills are very high."
            )
    elif is_adult_magi:
        if state in _EXPANSION_STATES:
            eligible_base = pfpl_base <= THRESH_ADULT_MAGI
        elif state in _WAIVER_100FPL_STATES:
            # No ACA expansion, but adults are covered up to 100% FPL under a
            # waiver (Wisconsin), so there is no coverage gap.
            eligible_base = pfpl_base <= 100.0
            if not eligible_base:
                # Above 100% FPL means marketplace subsidies are available.
                alts.append(
                    "Above your state's 100% FPL waiver limit—consider ACA marketplace subsidies (available starting at 100% FPL)."
                )
                if kids and kids > 0:
                    alts.append(
                        "If you’re a caretaker relative, check caretaker-relative Medicaid rules in your state."
                    )
        else:
            eligible_base = False
            if pfpl_base >= 100.0:
                alts.append(
                    "In non-expansion states, childless adults often aren’t eligible—consider ACA marketplace subsidies (available starting at 100% FPL)."
                )
            else:
                alts.append(
                    "Your state hasn't expanded Medicaid and marketplace subsidies generally start at 100% FPL (the 'coverage gap')—community health centers with sliding-scale fees can help in the meantime."
                )
            if kids and kids > 0:
                alts.append(
                    "If you’re a caretaker relative, check caretaker-relative Medicaid rules in your state."
                )

    # ---- target-year eligibility = base eligibility + federal work overlay ----
    # The overlay only applies from WORK_REQUIREMENT_FIRST_YEAR on, so a user
    # who asked about the current year gets their base verdict back rather
    # than being quizzed about work hours that don't apply to them yet.
    # Exemptions (if caller knows): pregnancy, SSDI/disabled, Medicare, ESRD/ALS,
    # children, and 65+ (the requirement targets able-bodied adults 19-64) are
    # treated as exempt by default.
    presumed_exempt = (
        is_preg
        or is_abd_disability
        or bool(on_medicare)
        or is_child
        or is_abd_age
        or has_esrd_or_als
        or applying_reason in _LTC_REASONS
    )
    exempt = (
        (work_req_exempt is True)
        or presumed_exempt
        or target_year < WORK_REQUIREMENT_FIRST_YEAR
    )

    if ltc_evaluated:
        # LTC applicants are medically frail -- never subject them to the
        # work overlay or the not-eligible-in-the-base-year clamp. Keyed on
        # the branch actually running, NOT on applying_reason:
        # a child or pregnant LTC applicant is scored by the child/pregnancy
        # branch earlier in the chain, and keying on the reason skipped their
        # exemption entirely, handing a 10-year-old a final "not eligible".
        pass
    elif not eligible_base:
        # The income test is year-independent, so failing it fails both years
        # and the work overlay can't rescue the target year.
        eligible_target = False
    else:
        if exempt:
            eligible_target = True
        else:
            # We need a 3-month lookback assessment for 80 hours per month
            def compute_monthly_sums() -> Optional[List[float]]:
                # Derive monthly totals from weekly data if available.
                # Scale by weeks-per-month rather than slicing into 4-week
                # buckets: bucketing dropped any weeks past the 12th and
                # undercounted real calendar months by ~8%, so the same
                # person got opposite verdicts depending on whether the model
                # sent a weekly list or a weekly average.
                if weekly_hours:
                    weeks = len(weekly_hours)
                    if weeks >= REQUIRED_MONTHS:
                        # Partition the weeks across the 3 months so real
                        # month-to-month variation survives -- averaging the
                        # whole window and repeating it made [60]*4 + [0]*8
                        # (two genuinely empty months) pass the per-month
                        # rule. Each bucket is then scaled to a calendar
                        # month (13 weeks per quarter, not 12), which keeps
                        # this in agreement with the weekly-average path
                        # instead of undercounting by ~8%.
                        per_bucket = weeks / REQUIRED_MONTHS
                        weeks_in_month = 13.0 / REQUIRED_MONTHS
                        buckets: List[float] = []
                        for index in range(REQUIRED_MONTHS):
                            start = int(round(index * per_bucket))
                            end = int(round((index + 1) * per_bucket))
                            chunk = weekly_hours[start:end]
                            if not chunk:
                                continue
                            buckets.append((sum(chunk) / len(chunk)) * weeks_in_month)
                        if len(buckets) == REQUIRED_MONTHS:
                            return buckets
                if total_hours_3mo is not None:
                    # Average monthly hours from total
                    avg_monthly = float(total_hours_3mo) / REQUIRED_MONTHS
                    return [avg_monthly] * REQUIRED_MONTHS
                return None

            monthly_data = compute_monthly_sums()
            # Ask for hours if we can't compute. Each phrasing maps directly
            # onto a documented kwarg (avg_monthly_qualifying_hours_last_3mo /
            # total_qualifying_hours_last_3mo) so the answer has a slot to
            # land in instead of being silently dropped.
            if monthly_data is None:
                asked_avg = ask(
                    "avg_monthly_qualifying_hours_last_3mo",
                    f"For {target_year}, about how many qualifying hours per month (work, school, volunteering, or caregiving) do you average?",
                )
                asked_total = ask(
                    "total_qualifying_hours_last_3mo",
                    "If easier, share your total qualifying hours over the last 3 months.",
                )
                eligible_target = False  # unknown until we get this
                if not asked_avg and not asked_total:
                    # Both hour questions declined, so there is no way to
                    # apply the 80-hrs/month rule. Without this the "unknown"
                    # above shipped as a confident "may not be eligible under
                    # the target year's rules" to someone who just couldn't
                    # estimate.
                    indeterminate.append("qualifying work hours")
            else:
                # Check monthly requirement
                avg_monthly = sum(monthly_data) / REQUIRED_MONTHS
                months_meeting = sum(
                    1 for m in monthly_data if m >= WORK_REQ_MONTHLY_HOURS
                )
                meets_rule = (
                    avg_monthly >= WORK_REQ_MONTHLY_HOURS
                    and months_meeting >= MIN_MONTHS_MEETING
                )
                if target_year >= WORK_REQUIREMENT_UNIVERSAL_YEAR:
                    eligible_target = bool(meets_rule)
                else:
                    # Transition window. The requirement is in force in the
                    # states that went early and nowhere else yet, and we
                    # don't track which is which -- so a shortfall here is
                    # NOT a denial. Say the income/category test passed and
                    # let the renderer carry the "this may already apply
                    # where you live" caveat.
                    eligible_target = True
                    work_req_conditional = not meets_rule
                # Coaching keys off the hours, not the verdict: in the
                # transition window the verdict stays positive, but someone
                # under 80 hours still needs to hear what is coming.
                if not meets_rule:
                    alts.append(
                        f"For {target_year}, try to reach ~80 hrs/month each month via job, school, or volunteering."
                    )
                    alts.append(
                        "Keep good records (pay stubs, schedules, volunteer logs)—we know this is frustrating."
                    )

    # ---- general alternatives / supportive pointers ----
    if assumed_fields:
        # The user couldn't answer these, so the estimate leans on an
        # assumption -- say which, so they can correct it if it's wrong.
        readable = ", ".join(f.replace("_", " ") for f in assumed_fields)
        alts.append(
            f"We assumed the most conservative answer for: {readable}. "
            "If any of those are wrong the estimate may change."
        )

    if veteran:
        alts.append(
            "Since there’s veteran status in the household, compare with VA health benefits."
        )
    if not eligible_base or not eligible_target:
        alts.extend(
            [
                "Visit your state Medicaid page for exact rules and to apply.",
                "If denied, you can appeal; gather documentation and deadlines.",
                "If income is close, check allowable deductions or changes (childcare, alimony, pre-tax).",
                "Children may qualify for CHIP even if adults don't.",
            ]
        )

    # If we found no path and also have no further questions, route to a professional.
    no_path = not eligible_base and not eligible_target
    if no_path and not missing:
        alts.append(
            "We can’t find a pathway with the current info—consider speaking with a benefits navigator or attorney."
        )

    if indeterminate:
        readable = ", ".join(f.replace("_", " ") for f in indeterminate)
        alts.append(
            f"We could not check {readable} because that answer wasn't "
            "available—your state Medicaid agency can check it directly."
        )
    return _result(determination_made=not indeterminate)


@lru_cache(maxsize=1)
def _load_medicaid_resources() -> "Optional[pd.DataFrame]":
    """Read the Medicaid resources CSV once per process.

    The file is ~400 KB and only ever yields one state's row, so re-parsing
    it on every lookup was pure overhead on a chat turn.

    The cache hands every caller the SAME DataFrame for the life of the
    process, so callers must treat it as read-only: slice and filter (which
    build new objects) rather than mutating in place. An ``inplace=True``
    rename or a column assignment here would corrupt every later lookup.
    """
    file_path = DATA_DIR / DEFAULT_FILE
    if not file_path.exists():
        matches = list(DATA_DIR.glob("*medicaid*.csv"))
        if not matches:
            return None
        file_path = matches[0]
    try:
        return pd.read_csv(file_path)
    except Exception:
        logger.opt(exception=True).warning("Could not read Medicaid resources CSV")
        return None


_BOOL_FIELDS = frozenset(
    {
        "married",
        "pregnant",
        "receiving_ssdi",
        "disabled",
        "on_medicare",
        "veteran_or_spouse_of_veteran",
        "home_owner",
        "als",
        "esrd",
        "on_medicaid_past",
        "work_req_exempt_2026",
        "work_req_exempt",
    }
)
_INT_FIELDS = frozenset(
    {"age", "ssdi_length", "household_size", "children_in_household", "years_worked"}
)
_FLOAT_FIELDS = frozenset(
    {
        "monthly_income",
        "assets_total",
        "home_equity",
        "avg_monthly_qualifying_hours_last_3mo",
        "avg_weekly_qualifying_hours_last_3mo",
        "total_qualifying_hours_last_3mo",
    }
)


def summarize_eligibility_inputs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Report what is_eligible actually understood from a tool payload.

    The model does its own parsing of the user's free text and keeps the
    running answers in its context, so it needs to see what landed on our
    side: values we normalized (``$1,200`` -> 1200.0, ``Medi-Cal`` -> ca),
    values we could not read, keys we do not know (silently dropped
    otherwise, which looks to the model exactly like acceptance), and the
    fields the user declined.

    Returns ``{"recorded", "unreadable", "unrecognized", "declined"}``.
    """
    recorded: Dict[str, Any] = {}
    unreadable: List[str] = []
    unrecognized: List[str] = []
    declined: List[str] = []

    for name, raw in kwargs.items():
        if name not in _KNOWN_ELIGIBILITY_FIELDS:
            unrecognized.append(name)
            continue
        if _is_unknown_marker(raw):
            declined.append(name)
            continue
        if raw is None:
            continue
        if name == "target_year":
            # Echo the year we will actually score (clamped/normalized), so
            # the model doesn't keep believing in a year we can't model.
            year = _parse_target_year(raw)
            if year is None:
                unreadable.append(name)
            else:
                recorded[name] = year
        elif name == "state":
            try:
                normalized: Any = _normalize_state(raw)
            except ValueError:
                normalized = None
            if normalized is None:
                unreadable.append(name)
            else:
                recorded[name] = normalized
        elif name in _BOOL_FIELDS:
            parsed_bool = _parse_bool(raw)
            if parsed_bool is None:
                unreadable.append(name)
            else:
                recorded[name] = parsed_bool
        elif name in _INT_FIELDS or name in _FLOAT_FIELDS:
            number = _parse_numeric(raw)
            if number is None:
                unreadable.append(name)
            else:
                recorded[name] = int(number) if name in _INT_FIELDS else number
        else:
            recorded[name] = raw

    return {
        "recorded": recorded,
        "unreadable": sorted(unreadable),
        "unrecognized": sorted(unrecognized),
        "declined": sorted(declined),
    }


def get_medicaid_info(query: Dict[str, Any]) -> Optional[str]:
    """
    query example: {"state":"StateName"}
    Returns a clean, professional format with key contact info.

    Returns None when the state can't be determined, so the caller can ask
    the user which state they're in. (Returning prose here would be
    indistinguishable from data: the chat tool wraps any string it gets in
    "Here's the official Medicaid information for X".)
    """
    raw_state = str(query.get("state") or query.get("State") or "").strip()
    if not raw_state:
        # Check before normalizing: _normalize_state("") raises, which would
        # otherwise report a garbled state the user never actually gave.
        return None
    try:
        state_short = _normalize_state(raw_state)
    except ValueError:
        # A state we can't recognize (typo, garbled LLM value) should read as
        # a re-ask, not an exception that kills the whole tool call -- same
        # hardening is_eligible has.
        return None
    if state_short is None:
        # A placeholder like "unknown"/"StateName": without a real state we'd
        # dump every state's contact info, which is useless in chat.
        return None

    # _normalize_state only returns 2-letter codes, every one of which is in
    # the shared reverse map (kept in sync with _STATE_MAP).
    state = _ABBR_TO_NAME[state_short]

    df = _load_medicaid_resources()
    if df is None:
        return None

    # Filter by state
    if state and "state" in df.columns:
        df = df[df["state"].astype(str).str.lower() == state.lower()]

    if df.empty:
        # No row for this state. The territories (PR/GU/VI/AS/MP) normalize to
        # a display name but have no entry in medicaid_resources.csv, and the
        # caller wraps whatever comes back as "Here's the official Medicaid
        # information for X:" -- so returning prose here presented "no data
        # found" to the model as retrieved data. None routes it to the same
        # re-ask/decline path as an unrecognized state.
        return None

    # Focus on the most important contact info
    important_cols = [
        "agency",
        "agency_phone",
        "helpline",
        "helpline_contact",
        "agency_website",
    ]
    available_cols = [col for col in important_cols if col in df.columns]

    if not available_cols:
        return f"Medicaid data found for {state}, but no contact information available."

    # Get the first few rows with contact info
    contact_info = df[available_cols].drop_duplicates()

    # Format in clean, simple style matching work requirements format
    result = []

    for _, row in contact_info.iterrows():
        # Main Medicaid Agency Section
        if (
            "agency" in available_cols
            and pd.notna(row["agency"])
            and str(row["agency"]).strip()
        ):
            result.append(f"{row['agency']}")

        # Contact Information
        if (
            "agency_phone" in available_cols
            and pd.notna(row["agency_phone"])
            and str(row["agency_phone"]).strip()
        ):
            phone = str(row["agency_phone"]).strip()
            result.append(f"Phone: {phone}")

        if (
            "agency_website" in available_cols
            and pd.notna(row["agency_website"])
            and str(row["agency_website"]).strip()
        ):
            website = str(row["agency_website"]).strip()
            result.append(f"Website: {website}")

        # Add spacing before legal section
        if (
            "helpline" in available_cols
            and pd.notna(row["helpline"])
            and str(row["helpline"]).strip()
        ):
            result.append("")  # Empty line for spacing
            result.append("Legal Resources:")
            result.append(f"{row['helpline']}")

            if (
                "helpline_contact" in available_cols
                and pd.notna(row["helpline_contact"])
                and str(row["helpline_contact"]).strip()
            ):
                phone = str(row["helpline_contact"]).strip()
                result.append(f"Phone: {phone}")

    work_req_section = _format_work_requirement_section(df.iloc[0], state)
    if work_req_section:
        result.append("")
        result.extend(work_req_section)

    return "\n".join(result)


def _has_value(row: "pd.Series", col: str) -> bool:
    """``True`` when ``row[col]`` exists, isn't NaN, and isn't the CSV's "N/A"."""
    if col not in row or pd.isna(row[col]):
        return False
    value = str(row[col]).strip()
    return bool(value) and value.upper() != "N/A"


def _format_work_requirement_section(row: "pd.Series", state: str) -> List[str]:
    """Build the guidance-only work-requirement block for ``get_medicaid_info``.

    Always leads with the universal federal deadline (applies to every state
    regardless of that state's own waiver history), then layers on whatever
    curated waiver narrative and scraped freshness-check data
    (``medicaid_work_requirements_fetcher``) the CSV happens to have. Never
    a legal determination -- state-by-state rollout is still moving, so this
    is a pointer for the user to verify, not a verdict.
    """
    lines = [
        "Work Requirement Status (guidance only -- confirm with the state; "
        "hard implementation isn't required nationwide until "
        f"{WORK_REQUIREMENT_UNIVERSAL_YEAR}):",
        (
            f"Federal law requires {state} Medicaid to have a work/community-"
            f"engagement requirement in place by January 1, "
            f"{WORK_REQUIREMENT_UNIVERSAL_YEAR} (some states earlier, from "
            f"{WORK_REQUIREMENT_FIRST_YEAR})."
        ),
    ]
    if _has_value(row, "work_requirement_waiver"):
        lines.append(
            f"{state}'s own waiver status as of our last review: "
            f"{str(row['work_requirement_waiver']).strip()}"
        )
    if _has_value(row, "waiver_activity"):
        lines.append(str(row["waiver_activity"]).strip())
    if _has_value(row, "work_requirement_last_checked") and _has_value(
        row, "work_requirement_mentioned"
    ):
        checked = str(row["work_requirement_last_checked"]).strip()
        mentioned = str(row["work_requirement_mentioned"]).strip().lower()
        source_url = (
            str(row["work_requirement_source_url"]).strip()
            if _has_value(row, "work_requirement_source_url")
            else ""
        )
        if mentioned == "yes":
            lines.append(
                f"As of {checked}, we found a public mention of work/"
                f"community-engagement requirements on the state's site"
                + (f": {source_url}" if source_url else ".")
            )
        elif mentioned == "no":
            lines.append(
                f"As of {checked}, we did not find a public mention of work "
                "requirements on the state's homepage"
                + (f" ({source_url})" if source_url else "")
                + " -- that does not mean it doesn't apply, check directly."
            )
    return lines


def get_medicaid_work_requirement_status(state: str) -> Optional[Dict[str, str]]:
    """Plain-data work-requirement guidance for one state, for non-chat callers.

    Returns a dict with ``state``, ``work_requirement_waiver``,
    ``waiver_activity``, ``agency_website``, ``work_requirement_last_checked``
    -- any of which may be ``""`` when the CSV has nothing for that column --
    or ``None`` when the state can't be resolved or has no CSV row.

    Used by ``common_view_logic._get_outside_help_details`` to surface the
    same guidance on the appeal "next steps" page, without pulling in the
    HTML formatting ``get_medicaid_info`` does for the chat surface.
    """
    if not state:
        return None
    try:
        state_short = _normalize_state(state)
    except ValueError:
        return None
    if state_short is None:
        return None
    state_name = _ABBR_TO_NAME[state_short]

    df = _load_medicaid_resources()
    if df is None or "state" not in df.columns:
        return None
    matches = df[df["state"].astype(str).str.lower() == state_name.lower()]
    if matches.empty:
        return None
    row = matches.iloc[0]

    def _val(col: str) -> str:
        return str(row[col]).strip() if _has_value(row, col) else ""

    return {
        "state": state_name,
        "work_requirement_waiver": _val("work_requirement_waiver"),
        "waiver_activity": _val("waiver_activity"),
        "agency_website": _val("agency_website"),
        "work_requirement_last_checked": _val("work_requirement_last_checked"),
    }
