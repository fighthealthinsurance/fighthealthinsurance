from pathlib import Path
from typing import Any, Dict, Tuple, List, Optional, Sequence
import json, re
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


def is_eligible(**kwargs) -> Tuple[bool, bool, List[str], List[str]]:
    """
    Perform an approximate eligibility check for Medicaid based on the provided parameters.
    Returns a tuple of (2025 eligibility, 2026 eligibility, alternatives, missing_info).

    IMPORTANT: This uses simplified heuristics. Medicaid rules vary by state and change often.
    Treat results as a best guess only and confirm with state resources.

    Federal work/community engagement requirement:
      - Effective 12/31/2025 (i.e., for 2026 eligibility and onward), assume a federal requirement
        of 80 qualifying hours PER WEEK with a 3-month lookback (12 weeks).
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
      - state_expanded_medicaid: bool
      - state_has_medically_needy: bool

      # 2026 federal work requirement (ALWAYS ASSUMED TRUE):
      - work_req_exempt_2026: Optional[bool]  # if caller knows the person is exempt from work rules
      - qualifying_hours_weekly_last_12: Optional[Sequence[float]]  # 12 numbers, one per week
      - avg_weekly_qualifying_hours_last_3mo: Optional[float]       # fallback if weekly list not available
      - total_qualifying_hours_last_3mo: Optional[float]            # fallback if neither weekly nor avg provided

    Heuristics (approx):
      - MAGI Adults (19–64) in expansion states: <= 138% FPL.
      - Children: <= 200% FPL (often higher) — otherwise suggest CHIP.
      - Pregnant: <= 200% FPL (often higher).
      - ABD & LTC: asset limits approx $2k single / $3k married; LTC income cap ~ $3,000/mo;
        home equity must be below a default cap (use $750k if unknown). Medically-needy may help.
      - 2026 work overlay: requires >=80 qualifying hours per week on average across the last 12 weeks.
        If weekly detail is provided, we also require at least 8 of 12 weeks to meet or exceed 80.

    Returns:
      (eligible_2025: bool, eligible_2026: bool, alternatives: List[str], missing_info: List[str])
    """

    # ---- helpers ----
    def get_bool(name: str) -> Optional[bool]:
        v = kwargs.get(name, None)
        return bool(v) if v is not None else None

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

    # Rough FPL table for 2025 (annual). These are approximations; use official values when available.
    # We'll work monthly: divide by 12. For 2026 we model +3% inflation on thresholds.
    def fpl_annual_2025(hh: int) -> float:
        if hh <= 0:
            hh = 1
        base = 15000.0
        add = 5300.0
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

    # Federal work requirement knobs
    WORK_REQ_WEEKLY_HOURS = 80.0
    REQUIRED_WEEKS = 12
    MIN_WEEKS_MEETING_80 = 8  # allow some variance if average is met

    # ---- extract inputs ----
    state = kwargs.get("state")
    married = get_bool("married")
    age = get_int("age")
    pregnant = get_bool("pregnant")
    receiving_ssdi = get_bool("receiving_ssdi") or get_bool("disabled")
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
    expanded = get_bool("state_expanded_medicaid")
    medically_needy = get_bool("state_has_medically_needy")

    # 2026 work requirement inputs
    work_req_exempt_2026 = get_bool("work_req_exempt_2026")
    weekly_hours = get_seq_of_floats("qualifying_hours_weekly_last_12")
    avg_weekly_hours = get_float("avg_weekly_qualifying_hours_last_3mo")
    total_hours_3mo = get_float("total_qualifying_hours_last_3mo")

    # ---- track outputs ----
    missing: List[str] = []
    alts: List[str] = []
    eligible_2025 = False
    eligible_2026 = False

    # ---- prioritize missing info for stepwise questioning ----
    if not state:
        missing.append("What state do you live in?")
    if age is None:
        missing.append("How old are you?")
    if married is None:
        missing.append("Are you married or single?")
    if household_size is None:
        missing.append(
            "How many people are in your household for taxes (household size)?"
        )
    if monthly_income is None:
        missing.append(
            "About how much is your household's monthly income before taxes?"
        )

    if expanded is None:
        missing.append("Do you know if your state expanded Medicaid under the ACA?")
    if medically_needy is None:
        missing.append(
            "Does your state offer a medically-needy/spend-down Medicaid program (if known)?"
        )

    if pregnant is None:
        missing.append("Are you currently pregnant?")
    if kids is None:
        missing.append("How many children (under 19) live in your household?")

    if receiving_ssdi is None:
        missing.append(
            "Are you receiving SSDI or otherwise considered disabled for benefits?"
        )
    if on_medicare is None:
        missing.append("Are you currently on Medicare?")

    # LTC pathways
    if applying_reason in ("ltc_nursing_home", "ltc_home_care"):
        if living_situation is None:
            missing.append(
                "Where are you living now (home, assisted living, rehab, nursing home)?"
            )
        if assets_total is None:
            missing.append(
                "About how much are your countable financial assets (not including your primary home)?"
            )
        if home_owner is None:
            missing.append("Do you own a home?")
        if home_owner and home_equity is None:
            missing.append(
                "If you own a home, about how much equity do you have in it?"
            )
    else:
        if (age is not None and age >= 65) or (receiving_ssdi is True):
            if assets_total is None:
                missing.append(
                    "About how much are your countable financial assets (not including your primary home)?"
                )

    # If we’re missing core info, stop early and suggest next questions.
    core_needed = any(
        x is None
        for x in (
            state,
            age,
            married,
            household_size,
            monthly_income,
            expanded,
            medically_needy,
            pregnant,
            kids,
            receiving_ssdi,
            on_medicare,
        )
    )
    if core_needed:
        alts.extend(
            [
                "If not eligible, consider ACA marketplace plans with income-based subsidies.",
                "Children may qualify for CHIP even if adults don't.",
                "If disabled or on Medicare, ask about Medicare Savings Programs (QMB/SLMB/QI).",
                "If high medical bills, medically-needy/spend-down Medicaid (if your state offers it) can sometimes help.",
                "If you're a veteran or spouse, VA benefits might be better.",
            ]
        )
        return (False, False, alts, missing)

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
    elif is_preg:
        eligible_2025 = pfpl_2025 <= THRESH_PREG
    elif is_abd_age or is_abd_disability or on_medicare:
        if assets_total is None:
            missing.append(
                "We need your countable assets to check ABD rules (approx, excluding your primary home)."
            )
            return (
                False,
                False,
                ["Consider Medicare Savings Programs (QMB/SLMB/QI) if on Medicare."],
                missing,
            )
        asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE
        assets_ok = assets_total <= asset_limit
        income_ok_2025 = pfpl_2025 <= 100.0
        eligible_2025 = assets_ok and (income_ok_2025 or medically_needy)
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
    elif applying_reason in ("ltc_nursing_home", "ltc_home_care"):
        need_fields = []
        if assets_total is None:
            need_fields.append("assets_total")
        if home_owner is None:
            need_fields.append("home_owner")
        if living_situation is None:
            need_fields.append("living_situation")
        if home_owner and home_equity is None:
            need_fields.append("home_equity")
        if need_fields:
            if "assets_total" in need_fields:
                missing.append(
                    "About how much are your countable financial assets (exclude your primary home)?"
                )
            if "home_owner" in need_fields:
                missing.append("Do you own a home?")
            if "living_situation" in need_fields:
                missing.append(
                    "Where are you living now (home, assisted living, rehab, nursing home)?"
                )
            if "home_equity" in need_fields:
                missing.append("If you own a home, about how much equity is in it?")
            return (
                False,
                False,
                [
                    "If not eligible, ask about HCBS waivers or medically-needy/spend-down."
                ],
                missing,
            )

        asset_limit = ABD_ASSET_LIMIT_MARRIED if married else ABD_ASSET_LIMIT_SINGLE
        assets_ok = assets_total <= asset_limit
        home_ok = True
        if home_owner:
            home_ok = (home_equity or 0.0) <= HOME_EQUITY_CAP_DEFAULT
        income_ok_2025 = monthly_income <= LTC_INCOME_CAP_2025
        eligible_2025 = assets_ok and home_ok and income_ok_2025
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
        if expanded:
            eligible_2025 = pfpl_2025 <= THRESH_ADULT_MAGI
        else:
            eligible_2025 = False
            alts.append(
                "In non-expansion states, childless adults often aren’t eligible—consider ACA marketplace subsidies."
            )
            if kids and kids > 0:
                alts.append(
                    "If you’re a caretaker relative, check caretaker-relative Medicaid rules in your state."
                )
    else:
        alts.append("Consider ACA marketplace plans with subsidies.")
        if kids and kids > 0:
            alts.append("Children may qualify for CHIP.")
        return (False, False, alts, missing)

    # ---- 2026 eligibility = 2025 base eligibility + federal work overlay ----
    # Exemptions (if caller knows): pregnancy, SSDI/disabled, Medicare are treated as exempt by default.
    presumed_exempt = is_preg or is_abd_disability or bool(on_medicare)
    exempt = (work_req_exempt_2026 is True) or presumed_exempt

    if not eligible_2025:
        # If not eligible in 2025, they won't be in 2026 either (even before work overlay).
        eligible_2026 = False
    else:
        if exempt:
            eligible_2026 = True
        else:
            # We need a 12-week lookback assessment.
            def compute_avg_weekly_from_inputs() -> Optional[float]:
                if weekly_hours and len(weekly_hours) >= REQUIRED_WEEKS:
                    return sum(weekly_hours[:REQUIRED_WEEKS]) / REQUIRED_WEEKS
                if avg_weekly_hours is not None:
                    return float(avg_weekly_hours)
                if total_hours_3mo is not None:
                    # 3 months ≈ 12 weeks
                    return float(total_hours_3mo) / REQUIRED_WEEKS
                return None

            avg_wk = compute_avg_weekly_from_inputs()
            # Ask for hours if we can't compute
            if avg_wk is None:
                missing.append(
                    "For 2026, about how many qualifying hours per WEEK did you average over the last 12 weeks?"
                )
                missing.append(
                    "If easier, share your total qualifying hours over the last 3 months."
                )
                missing.append(
                    "If you can, provide your hours for each of the last 12 weeks."
                )
                eligible_2026 = False  # unknown until we get this
            else:
                meets_avg = avg_wk >= WORK_REQ_WEEKLY_HOURS
                meets_weeks_rule = True
                if weekly_hours and len(weekly_hours) >= REQUIRED_WEEKS:
                    weeks_meeting = sum(
                        1
                        for h in weekly_hours[:REQUIRED_WEEKS]
                        if h >= WORK_REQ_WEEKLY_HOURS
                    )
                    meets_weeks_rule = weeks_meeting >= MIN_WEEKS_MEETING_80
                eligible_2026 = bool(meets_avg and meets_weeks_rule)
                if not eligible_2026:
                    alts.append(
                        "For 2026, try to reach ~80 hrs/week on average via job, school, or volunteering."
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

    return (
        bool(eligible_2025),
        bool(eligible_2026),
        list(dict.fromkeys(alts)),
        missing,
    )


def get_medicaid_info(query: Dict[str, Any]) -> str:
    """
    query example: {"state":"StateName","topic":"","limit":5}
    Returns a clean, professional format with key contact info.
    """
    state = (query.get("state") or "").strip()
    topic = (query.get("topic") or "").strip().lower()
    limit = int(query.get("limit") or 5)

    # Convert state abbreviation to full name if needed
    state_lower = state.lower()

    # Normalize state name to title case and handle abbreviations
    state_abbrev_map = {
        "ca": "California",
        "oh": "Ohio",
        "ny": "New York",
        "tx": "Texas",
        "fl": "Florida",
        "pa": "Pennsylvania",
        "il": "Illinois",
        "mi": "Michigan",
        "ga": "Georgia",
        "nc": "North Carolina",
        "nj": "New Jersey",
        "va": "Virginia",
        "wa": "Washington",
        "az": "Arizona",
        "ma": "Massachusetts",
        "tn": "Tennessee",
        "in": "Indiana",
        "mo": "Missouri",
        "md": "Maryland",
        "wi": "Wisconsin",
        "co": "Colorado",
        "mn": "Minnesota",
        "sc": "South Carolina",
        "al": "Alabama",
        "la": "Louisiana",
        "ky": "Kentucky",
        "or": "Oregon",
        "ok": "Oklahoma",
        "ct": "Connecticut",
        "ut": "Utah",
        "ia": "Iowa",
        "nv": "Nevada",
        "ar": "Arkansas",
        "ms": "Mississippi",
        "ks": "Kansas",
        "nm": "New Mexico",
        "ne": "Nebraska",
        "wv": "West Virginia",
        "id": "Idaho",
        "hi": "Hawaii",
        "nh": "New Hampshire",
        "me": "Maine",
        "ri": "Rhode Island",
        "mt": "Montana",
        "de": "Delaware",
        "sd": "South Dakota",
        "nd": "North Dakota",
        "ak": "Alaska",
        "vt": "Vermont",
        "wy": "Wyoming",
    }

    if state_lower in state_abbrev_map:
        state = state_abbrev_map[state_lower]
    else:
        # Try to match as full state name (title case)
        state = state.title()

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

    if not available_cols:
        return f"Medicaid data found for {state}, but no contact information available."

    # Get the first few rows with contact info
    contact_info = df[available_cols].drop_duplicates().head(limit)

    # Format in clean, simple style matching work requirements format
    result = []

    for idx, row in contact_info.iterrows():
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
