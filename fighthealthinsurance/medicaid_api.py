import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

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
}

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

# Add 2-letter codes themselves as valid keys (so "CA"→"ca")
for full, abbr in list(_STATE_MAP.items()):
    _ALIASES[abbr] = full

# Precompute candidate keys (full names + aliases)
_CANDIDATE_KEYS = set(_STATE_MAP.keys()) | set(_ALIASES.keys())

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

# String truthiness for LLM-supplied "booleans": tool payloads are raw LLM
# JSON, which sometimes carries "false"/"no" instead of JSON booleans -- and
# bool("false") is True. Unknown strings read as unanswered (re-ask) rather
# than guessed.
_TRUE_STRINGS = frozenset({"true", "yes", "y", "1", "on"})
_FALSE_STRINGS = frozenset({"false", "no", "n", "0", "off", ""})


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


def is_eligible(**kwargs) -> Tuple[bool, bool, bool, List[str], List[str]]:
    """
    Perform an approximate eligibility check for Medicaid / Medicare based on the provided parameters.
    Returns a tuple of (2025 eligibility, 2026 eligibility, medicare, alternatives, missing_info).

    IMPORTANT: This uses simplified heuristics. Medicaid rules vary by state and change often.
    Treat results as a best guess only and confirm with state resources.

    Federal work/community engagement requirement:
      - Effective 12/31/2025 (i.e., for 2026 eligibility and onward), assume a federal requirement
        of 80 qualifying hours PER MONTH with a 3-month lookback (12 weeks).
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
      - 2026 work overlay: requires >=80 qualifying hours per month, averaged across a
        3-month lookback, with each of the 3 months individually meeting 80 hours.
      - Questions we would never ask (e.g. "are you on Medicare?" for a healthy
        30-year-old, or "are you pregnant?" for a toddler) are defaulted instead of
        stalling the determination waiting on an answer that will never come.

    Returns:
      (eligible_2025: bool, eligible_2026: bool, eligible_medicare: bool, alternatives: List[str], missing_info: List[str])
    """

    # ---- helpers ----
    def get_bool(name: str) -> Optional[bool]:
        v = kwargs.get(name, None)
        if v is None:
            return None
        if isinstance(v, str):
            # LLM payloads sometimes carry "false"/"no" string booleans;
            # bool("false") is True, which flipped verdicts. See
            # _TRUE_STRINGS/_FALSE_STRINGS.
            s = v.strip().lower()
            if s in _TRUE_STRINGS:
                return True
            if s in _FALSE_STRINGS:
                return False
            return None
        return bool(v)

    def get_int(name: str) -> Optional[int]:
        v = kwargs.get(name, None)
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    def get_float(name: str) -> Optional[float]:
        v = kwargs.get(name, None)
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

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

    def _result() -> Tuple[bool, bool, bool, List[str], List[str]]:
        """Single exit point: casts + dedupes so every return stays consistent."""
        return (
            bool(eligible_2025),
            bool(eligible_2026),
            bool(eligible_medicare),
            list(dict.fromkeys(alts)),
            list(dict.fromkeys(missing)),
        )

    # ---- prioritize missing info for stepwise questioning ----
    if not state:
        missing.append("What state do you live in?")
    if age is None:
        missing.append("How old are you?")
    # No federal minimum marriage age law exists Oo. We could _probably_ go with 16 though but
    # for now lets do 10 since asking is not terrible and some states do allow it.
    if age is not None and age < 10 and married is None:
        married = False
    if age is not None and age >= 10 and married is None:
        missing.append("Are you married or single?")
    if household_size is None:
        missing.append(
            "How many people are in your household for taxes (household size)?"
        )
    if monthly_income is None:
        missing.append(
            "About how much is your household's monthly income before taxes?"
        )

    # https://en.wikipedia.org/wiki/Lina_Medina :/
    if age is not None and pregnant is None:
        if age > 4:
            missing.append("Are you currently pregnant?")
        else:
            # Don't stall the determination on a question we'd never ask.
            pregnant = False
    if kids is None:
        missing.append("How many children (under 19) live in your household?")

    if receiving_ssdi is None:
        missing.append(
            "Are you receiving SSDI or otherwise considered disabled for benefits?"
        )
    if receiving_ssdi and ssdi_length is None:
        # Needed at any age: 24+ months of SSDI is its own Medicare pathway,
        # so a 67-year-old on SSDI must be asked too, not just under-65s.
        missing.append(_Q_SSDI_MONTHS)
    if age is not None and age < 65:
        if (ssdi_length is None or ssdi_length < 24) and esrd is None:
            missing.append("Are you in end stage renal failure?")
        if esrd is not None and als is None:
            missing.append("Do you have ALS?")

    # ---- Medicare pathway: enrolled, 65+, 24+ months of SSDI, ESRD, or ALS ----
    is_65_plus = age is not None and age >= 65
    has_esrd_or_als = bool(esrd) or bool(als)
    ssdi_24_months = (
        bool(receiving_ssdi) and ssdi_length is not None and ssdi_length >= 24
    )
    medicare_pathway = (
        bool(on_medicare) or is_65_plus or has_esrd_or_als or ssdi_24_months
    )
    if medicare_pathway:
        if on_medicare is None:
            missing.append("Are you currently on Medicare?")
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
                missing.append(
                    "How many years did you (or spouse or ex-spouse) work and pay medicare taxes?"
                )
            elif years_worked >= 10:
                # 10 years (40 quarters) of Medicare taxes = premium-free Part A.
                eligible_medicare = True
            elif receiving_ssdi and ssdi_length is None:
                # Don't rule Medicare out yet: 24+ months of SSDI would
                # qualify them and that question is still outstanding.
                pass
            else:
                eligible_medicare = False
                alts.append(
                    "You may be eligible to buy Part-A medicare even if you don't qualify for premium-free part A."
                )
                alts.append(
                    "Medicare Savings Programs (QMB/SLMB/QI) can help pay Medicare premiums when income and assets are low."
                )
    elif age is not None and on_medicare is None:
        # Under 65 with no disability pathway: almost never on Medicare, so
        # assume not enrolled instead of stalling the flow on a question
        # whose answer is essentially always "no".
        on_medicare = False

    # LTC pathways
    if applying_reason in _LTC_REASONS:
        if living_situation is None:
            missing.append(_Q_LIVING_SITUATION)
        if assets_total is None:
            missing.append(_Q_ASSETS)
        if home_owner is None:
            missing.append(_Q_HOME_OWNER)
        if home_owner and home_equity is None:
            missing.append(_Q_HOME_EQUITY)
    else:
        if (age is not None and age >= 65) or (receiving_ssdi is True):
            if assets_total is None:
                missing.append(_Q_ASSETS)

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
        return _result()

    # ---- with core info present, evaluate categories ----
    pfpl_2025 = pct_fpl(monthly_income, household_size, 2025)
    pfpl_2026 = pct_fpl(monthly_income, household_size, 2026)

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
            missing.append(_Q_ASSETS)
        else:
            asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE
            assets_ok = assets_total <= asset_limit
            income_ok_2025 = pfpl_2025 <= 100.0
            eligible_2025 = assets_ok and (income_ok_2025 or medically_needy)
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
                else:
                    alts.append(
                        "Ask about medically-needy/spend-down Medicaid in your state."
                    )
    elif applying_reason in _LTC_REASONS:
        # For LTC we need some extra info
        if assets_total is None:
            missing.append(_Q_ASSETS)
        if home_owner is None:
            missing.append(_Q_HOME_OWNER)
        if living_situation is None:
            missing.append(_Q_LIVING_SITUATION)
        if home_owner and home_equity is None:
            missing.append(_Q_HOME_EQUITY)
        if (
            assets_total is None
            or home_owner is None
            or living_situation is None
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

        # assets_total is narrowed by the early return above; zero assets
        # passes the test.
        assert assets_total is not None
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
    else:
        alts.append("Consider ACA marketplace plans with subsidies.")
        if kids and kids > 0:
            alts.append("Children may qualify for CHIP.")

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
    )
    exempt = (work_req_exempt_2026 is True) or presumed_exempt

    if applying_reason in _LTC_REASONS:
        # The LTC branch computed eligible_2026 itself (the LTC income cap
        # differs by year, so income can pass 2026 while failing 2025), and
        # LTC applicants are medically frail -- never subject them to the
        # work overlay or the not-eligible-2025 clamp.
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
                # Derive monthly totals from weekly data if available
                if weekly_hours and len(weekly_hours) >= 12:
                    # Group into 3 months of 4 weeks each
                    return [
                        sum(weekly_hours[i * 4 : (i + 1) * 4])
                        for i in range(REQUIRED_MONTHS)
                    ]
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
                missing.append(
                    "For 2026, about how many qualifying hours per month (work, school, volunteering, or caregiving) do you average?"
                )
                missing.append(
                    "If easier, share your total qualifying hours over the last 3 months."
                )
                eligible_2026 = False  # unknown until we get this
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

    return _result()


def get_medicaid_info(query: Dict[str, Any]) -> str:
    """
    query example: {"state":"StateName","topic":"","limit":5}
    Returns a clean, professional format with key contact info.
    """
    raw_state = str(query.get("state") or query.get("State") or "").strip()
    try:
        state_short = _normalize_state(raw_state)
    except ValueError:
        # A state we can't recognize (typo, garbled LLM value) should read as
        # a re-ask, not an exception that kills the whole tool call -- same
        # hardening is_eligible has.
        return (
            f"We couldn't tell which state {raw_state!r} is. "
            "Please ask the user to confirm their state and try again."
        )
    if state_short is None:
        # No state (or a placeholder like "unknown"): without one we'd dump
        # every state's contact info, which is useless in chat.
        return (
            "We need to know which state they're in to look up Medicaid "
            "contact info. Please ask the user for their state and try again."
        )
    topic = (query.get("topic") or "").strip().lower()
    limit = int(query.get("limit") or 5)

    # _normalize_state only returns 2-letter codes, every one of which is in
    # the shared reverse map (kept in sync with _STATE_MAP).
    state = _ABBR_TO_NAME[state_short]

    # Pick the CSV
    file_path = DATA_DIR / DEFAULT_FILE
    if not file_path.exists():
        matches = list(DATA_DIR.glob("*medicaid*.csv"))
        if not matches:
            return f"Could not find Medicaid data file."
        file_path = matches[0]

    # Read CSV
    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        return f"Error reading data: {e}"

    # Filter by state
    if state and "state" in df.columns:
        df = df[df["state"].astype(str).str.lower() == state.lower()]

    if df.empty:
        return f"No Medicaid data found for {state}."

    # Focus on the most important contact info
    important_cols = [
        "agency",
        "agency_phone",
        "helpline",
        "helpline_contact",
        "agency_website",
    ]
    available_cols = [col for col in important_cols if col in df.columns]
    misc_cols = [col for col in df.columns if col not in important_cols]

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
        result.append("")
        result.append("***MISC info (less important)***")
        for col in misc_cols:
            if col in row and row[col]:
                result.append(row[col])

    return "\n".join(result)
