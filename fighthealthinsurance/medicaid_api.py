import difflib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    # State Medicaid program names. The prompt asks the model to infer the
    # state from these; this is the backstop for when it passes the program
    # name straight through as the "state".
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
    # “state of …” forms
    "state of california": "california",
    "state of new york": "new york",
    "state of washington": "washington",
}

# Add 2-letter codes themselves as valid keys (so "CA"→"ca")
for full, abbr in list(_STATE_MAP.items()):
    _ALIASES[abbr] = full

# State program names are matched EXACTLY, never fuzzily. "medical" (how
# "Medi-Cal" survives punctuation stripping) sits 0.857 away from "medicad",
# a routine misspelling of "medicaid" -- above the 0.84 cutoff -- so leaving
# these in the fuzzy pool silently scored typo'd input under California's
# rules. A program name is either recognized exactly or it is not a state.
_PROGRAM_ALIASES = frozenset(
    {
        "medi-cal",
        "denalicare",
        "health first colorado",
        "husky health",
        "diamond state health plan",
        "med-quest",
        "medquest",
        "healthchoice illinois",
        "hoosier healthwise",
        "kansas medical assistance program",
        "mainecare",
        "masshealth",
        "mo healthnet",
        "nj familycare",
        "turquoise care",
        "soonercare",
        "tenncare",
        "star+plus",
        "star plus",
        "green mountain care",
        "cardinal care",
        "apple health",
        "forward health",
        "forwardhealth",
        "equality care",
    }
)

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
}

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


def is_eligible(
    **kwargs,
) -> Tuple[bool, bool, bool, List[str], List[str], bool]:
    """
    Perform an approximate eligibility check for Medicaid / Medicare based on the provided parameters.
    Returns a tuple of (2025 eligibility, 2026 eligibility, medicare,
    alternatives, missing_info, determination_made).

    IMPORTANT: This uses simplified heuristics. Medicaid rules vary by state and change often.
    Treat results as a best guess only and confirm with state resources.

    Federal work/community engagement requirement:
      - Per the CMS interim final rule (CMS-2454-IFC, June 2026), states must
        implement the requirement no later than JANUARY 1, 2027; a few have
        gone earlier, and states showing good-faith effort can get extensions.
        The ``eligible_2026`` flag therefore models "eligibility once the work
        requirement applies to them", not calendar-year 2026 specifically.
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

      # 2026 federal work requirement (ALWAYS ASSUMED TRUE):
      - work_req_exempt_2026: Optional[bool]  # if caller knows the person is exempt from work rules
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
      - 2026 work overlay: requires >=80 qualifying hours per month across a
        3-month lookback. Detailed weekly records are partitioned per month so
        each month is checked on its own; a single monthly average or a 3-month
        total has no per-month detail to check, so it is spread evenly and only
        the average can be tested.
      - Questions we would never ask (e.g. "are you on Medicare?" for a healthy
        30-year-old, or "are you pregnant?" for a toddler) are defaulted instead of
        stalling the determination waiting on an answer that will never come.

    Returns:
      (eligible_2025, eligible_2026, eligible_medicare, alternatives,
       missing_info, determination_made)

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

    # FPL table for 2025 (annual, 48 contiguous states + DC published guidelines:
    # $15,650 for one person plus $5,500 per additional person). Alaska and
    # Hawaii run higher; we keep the contiguous values as the approximation.
    # We'll work monthly: divide by 12. For 2026 we model +3% inflation on thresholds.
    def fpl_annual_2025(hh: int) -> float:
        if hh <= 0:
            hh = 1
        base = 15650.0
        add = 5500.0
        return base + add * (hh - 1)

    def pct_fpl(monthly_income: float, hh: int, year: int) -> float:
        annual = monthly_income * 12.0
        fpl = fpl_annual_2025(hh)
        if year == 2026:
            fpl *= 1.03  # simple inflation bump
        return (annual / fpl) * 100.0 if fpl > 0 else 9999.0

    # Policy knobs / defaults
    LTC_INCOME_CAP_2025 = (
        3000.0  # rough nursing home / HCBS cap (varies by state/waiver)
    )
    LTC_INCOME_CAP_2026 = LTC_INCOME_CAP_2025 * 1.03
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
    applying_reason = kwargs.get("applying_reason") or "standard"
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

    # 2026 work requirement inputs
    work_req_exempt_2026 = get_bool("work_req_exempt_2026")
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
    eligible_2025 = False
    eligible_2026 = False
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

    def _result(
        determination_made: bool = False,
    ) -> Tuple[bool, bool, bool, List[str], List[str], bool]:
        """Single exit point: casts + dedupes so every return stays consistent.

        ``determination_made`` separates "we scored this person and the answer
        is no" from "we couldn't score them at all" (a US territory, or a
        required answer the user declined). Both used to come back as
        False/False with no questions left, which the chat tool rendered as a
        confident "may not be eligible".
        """
        return (
            bool(eligible_2025),
            bool(eligible_2026),
            bool(eligible_medicare),
            list(dict.fromkeys(alts)),
            list(dict.fromkeys(missing)),
            determination_made,
        )

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
    if ssdi_answer and ssdi_length is None:
        # Needed at any age: 24+ months of SSDI is its own Medicare pathway,
        # so a 67-year-old on SSDI must be asked too, not just under-65s.
        ask("ssdi_length", _Q_SSDI_MONTHS)
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
            elif ssdi_answer and ssdi_length is None:
                # Don't rule Medicare out yet: 24+ months of SSDI would
                # qualify them and that question is still outstanding.
                #
                # Gate on ssdi_answer, not receiving_ssdi: the latter is
                # ORed with `disabled`, so someone who answered "disabled,
                # not on SSDI" waited here for an ssdi_length question that
                # is never asked (it is gated on ssdi_answer too). They fell
                # out with a silent Medicare False and no alternatives.
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
            for field, question in zip(asked_fields, missing)
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
        if abd_candidate and assets_total is None:
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
    pfpl_2025 = pct_fpl(monthly_income, household_size, 2025)

    # Set by the LTC branch below: that branch computes its own 2026 verdict
    # (the LTC income cap differs by year), so the work overlay must not
    # recompute it -- but only when the branch actually ran.
    ltc_evaluated = False

    is_child = age < 19
    is_adult_magi = 19 <= age <= 64
    is_abd_age = age >= 65
    is_abd_disability = bool(receiving_ssdi)
    is_preg = bool(pregnant)

    THRESH_ADULT_MAGI = 138.0
    THRESH_CHILD = 200.0
    THRESH_PREG = 200.0

    # Category decisions → 2025 base eligibility (pre-work overlay)
    if is_child:
        eligible_2025 = pfpl_2025 <= THRESH_CHILD
        if not eligible_2025:
            alts.append(
                "CHIP: Children may still qualify for CHIP at higher incomes than Medicaid."
            )
        # skip work overlay for children
        work_req_exempt_2026 = True
    elif is_preg:
        eligible_2025 = pfpl_2025 <= THRESH_PREG
    elif (
        is_abd_age or is_abd_disability or on_medicare
    ) and applying_reason not in _LTC_REASONS:
        # Someone explicitly applying for long-term care falls through to the
        # LTC branch below (LTC applicants are almost always 65+/disabled, so
        # without this carve-out they'd be evaluated under the wrong rules).
        if assets_total is None:
            if not ask("assets_total", _Q_ASSETS):
                # Declined: an asset test we cannot run is not a failed asset
                # test. Without this the else-arm never ran, so a 66-year-old
                # at 69% FPL who simply didn't know their asset total got a
                # flat "may not be eligible" and none of the pathways below.
                indeterminate.append("assets_total")
                if on_medicare:
                    alts.append(
                        "Medicare Savings Programs (QMB/SLMB/QI) can help pay Part A/B premiums and cost-sharing."
                    )
                if medically_needy:
                    alts.append(
                        "Medically-needy/spend-down Medicaid may help if medical bills are high."
                    )
        else:
            asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE
            assets_ok = assets_total <= asset_limit
            income_ok_2025 = pfpl_2025 <= 100.0
            # NOTE: medically_needy is a spend-down PATHWAY, not an income
            # waiver -- treating it as one reported six-figure earners as
            # "probably eligible". It stays a suggestion below instead.
            eligible_2025 = assets_ok and income_ok_2025
            if not eligible_2025 and is_adult_magi:
                # Disability doesn't bar the ACA expansion pathway: adults
                # 19-64 in expansion (or 100%-FPL-waiver) states qualify on
                # income alone with no asset test, so check MAGI before
                # concluding ineligibility.
                if state in _EXPANSION_STATES and pfpl_2025 <= THRESH_ADULT_MAGI:
                    eligible_2025 = True
                elif state in _WAIVER_100FPL_STATES and pfpl_2025 <= 100.0:
                    eligible_2025 = True
            if not eligible_2025:
                if on_medicare:
                    alts.append(
                        "Medicare Savings Programs (QMB/SLMB/QI) can help pay Part A/B premiums and cost-sharing."
                    )
                if medically_needy:
                    alts.append(
                        "Medically-needy/spend-down Medicaid may help if medical bills are high."
                    )
                # No else: the ~17 states without a medically-needy program
                # for aged/disabled adults have nothing to ask about, and
                # sending someone to their agency for a program that doesn't
                # exist there wastes a call. The generic pointers below
                # (state page, navigator, appeal) still apply.
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
        income_ok_2025 = monthly_income <= LTC_INCOME_CAP_2025
        income_ok_2026 = monthly_income <= LTC_INCOME_CAP_2026
        eligible_2025 = assets_ok and home_ok and income_ok_2025
        eligible_2026 = assets_ok and home_ok and income_ok_2026
        if not income_ok_2025:
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
            eligible_2025 = pfpl_2025 <= THRESH_ADULT_MAGI
        elif state in _WAIVER_100FPL_STATES:
            # No ACA expansion, but adults are covered up to 100% FPL under a
            # waiver (Wisconsin), so there is no coverage gap.
            eligible_2025 = pfpl_2025 <= 100.0
            if not eligible_2025:
                # Above 100% FPL means marketplace subsidies are available.
                alts.append(
                    "Above your state's 100% FPL waiver limit—consider ACA marketplace subsidies (available starting at 100% FPL)."
                )
                if kids and kids > 0:
                    alts.append(
                        "If you’re a caretaker relative, check caretaker-relative Medicaid rules in your state."
                    )
        else:
            eligible_2025 = False
            if pfpl_2025 >= 100.0:
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

    # ---- 2026 eligibility = 2025 base eligibility + federal work overlay ----
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
    exempt = (work_req_exempt_2026 is True) or presumed_exempt

    if ltc_evaluated:
        # The LTC branch computed eligible_2026 itself (the LTC income cap
        # differs by year, so income can pass 2026 while failing 2025), and
        # LTC applicants are medically frail -- never subject them to the
        # work overlay or the not-eligible-2025 clamp. Keyed on the branch
        # actually running, NOT on applying_reason: a child or pregnant LTC
        # applicant is scored by the child/pregnancy branch earlier in the
        # chain, and keying on the reason skipped their exemption entirely,
        # handing a 10-year-old a final "not eligible in 2026".
        pass
    elif not eligible_2025:
        # If not eligible in 2025, they won't be in 2026 either (even before work overlay).
        eligible_2026 = False
    else:
        if exempt:
            eligible_2026 = True
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
                    "For 2026, about how many qualifying hours per month (work, school, volunteering, or caregiving) do you average?",
                )
                asked_total = ask(
                    "total_qualifying_hours_last_3mo",
                    "If easier, share your total qualifying hours over the last 3 months.",
                )
                eligible_2026 = False  # unknown until we get this
                if not asked_avg and not asked_total:
                    # Both hour questions declined, so there is no way to
                    # apply the 80-hrs/month rule. Without this the "unknown"
                    # above shipped as a confident "may not be eligible under
                    # the 2026 rules" to someone who just couldn't estimate.
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
                eligible_2026 = bool(meets_rule)
                if not eligible_2026:
                    alts.append(
                        "For 2026, try to reach ~80 hrs/month each month via job, school, or volunteering."
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
    if not eligible_2025 or not eligible_2026:
        alts.extend(
            [
                "Visit your state Medicaid page for exact rules and to apply.",
                "If denied, you can appeal; gather documentation and deadlines.",
                "If income is close, check allowable deductions or changes (childcare, alimony, pre-tax).",
                "Children may qualify for CHIP even if adults don't.",
            ]
        )

    # If we found no path and also have no further questions, route to a professional.
    no_path = not eligible_2025 and not eligible_2026
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
        if name == "state":
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

    return "\n".join(result)
