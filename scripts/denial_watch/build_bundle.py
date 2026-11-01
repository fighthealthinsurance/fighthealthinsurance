"""Build static/denial_watch.json, the numbers behind the Denial Watch page.

Every number on the page comes from the public files listed in README.md next
to this script. Download them into a raw directory, then:

    pip install pandas openpyxl
    python scripts/denial_watch/build_bundle.py --raw-dir /path/to/raw

It writes fighthealthinsurance/static/denial_watch.json and prints the headline
figures so a person can check them against the last build before committing.
This runs by hand, not in the app: the site only ever reads the JSON.
"""

import csv
import hashlib
import io
import json
import math
import pathlib
import re
import urllib.parse

import argparse
import datetime
import sys
import typing

if typing.TYPE_CHECKING:
    import pandas as pd

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
DEFAULT_OUT = REPO / "fighthealthinsurance" / "static" / "denial_watch.json"
RAW = HERE / "raw"  # replaced by --raw-dir

# PUF plan year -> the calendar year its claims come from.
CLAIMS_YEAR = {2022: 2020, 2023: 2021, 2024: 2022, 2025: 2023, 2026: 2024, 2027: 2025}
LATEST_PY = 2027
MIN_ISSUER_CLAIMS = 50_000  # in-network claims, for the insurer table
MIN_APPEALS_FOR_RATE = 30  # an insurer's appeal win rate is shown only on this many appeals
MIN_STATE_APPEALS = 50
MIN_OON_CLAIMS = 1_000  # an insurer's out-of-network rate needs this many out-of-network claims
NETWORK_ONLY = {"HMO", "EPO"}  # plan types that mostly don't pay for out-of-network care
PLAN_TYPES_PY = CLAIMS_YEAR[LATEST_PY]  # the plans sold in the latest claims year

# (state, issuer id) reporting more out-of-network denials than claims, by plan year. They get no
# out-of-network or all-claims figure, and the build stops if a refresh finds a different set.
KNOWN_DENIED_OVER_CLAIMS = {
    2024: {("KS", 18558)},
    2025: {("ND", 37160)},
    2026: {("AK", 73836), ("ND", 37160)},
    2027: {("MI", 98185)},
}

# (state, issuer id) whose appeals-won count looks like a reporting error, so it's left out like a
# suppressed cell: 0 won after hundreds the year before, a rate CMS marked suppressed, or a repeat of last year.
DOUBTFUL_WON = {
    2026: {
        ("AR", 37903), ("AR", 62141), ("AR", 70525), ("MS", 90714),  # 0 won
        ("KS", 34368), ("AZ", 91450), ("WV", 50328),  # rate suppressed, counts printed
        ("UT", 22013), ("OR", 77969),  # same counts as the 2025 file
    },
    2027: {("UT", 22013), ("TX", 66252), ("LA", 98780)},  # rate suppressed, counts printed
}

# (state, issuer id) whose in-network denials look like a reporting error, so they're left out like a missing cell.
DOUBTFUL_DENIED_IN = {
    2027: {("FL", 66966)},  # 448,953 of 466,619 denied (96%), after 6.5% the year before
}

# Plan-level reason columns (in-network, and a claim can count under more than one).
# CMS's out-of-network reason counts closed-network denials, so it stays out, as in KFF's.
REASONS = {
    "plan_number_claims_denied_referral_required": "Prior authorization or referral required",
    "plan_number_claims_denied_services_excluded": "Service excluded from the plan",
    "plan_number_claims_denied_not_medically_necessary_excluding_behavioral_health": '"Not medically necessary" (physical health)',
    "plan_number_claims_denied_not_medically_necessary_behavioral_health_only": '"Not medically necessary" (behavioral health)',
    "plan_number_claims_denied_due_to_enrolle_benefit_limit_reached": "Benefit limit reached",
    "plan_number_claims_denied_due_to_member_not_covered": "Person not covered on the date of service",
    "plan_number_claims_denied_due_to_investigational_experimental_cosmetic_proceduce": "Experimental, investigational or cosmetic",
    "plan_number_claims_denied_due_to_administrative_reason": "Administrative (missing information, duplicate, late filing, billing)",
    "plan_number_claims_denied_other": "Other / not specified",
}
# The PY2027 file splits each reason by network and fixes two misspelled names.
REASON_COLUMNS_2027 = {
    col: col.replace("enrolle_", "enrollee_").replace("proceduce", "procedure") + "_in_network" for col in REASONS
}
PLAN_DENIED_IN = "plan_number_claims_denied_in_network"
# The PY2027 file splits plan-level in-network denials into these two, which add up to the older count.
PLAN_DENIED_IN_2027 = (
    "plan_number_claims_denied_in_network_non_behavioral_health",
    "plan_number_claims_denied_in_network_behavioral_health_only",
)
# CMS's closed-network reason: out-of-network care the plan doesn't cover (this column name from PY2024 on).
CLOSED_NETWORK = "plan_number_claims_denied_due_to_out_of_network"
CLOSED_NETWORK_2027 = "plan_number_claims_denied_due_to_out_of_network_provider"


def num(x):
    """A reported count, or None for suppressed/blank cells ('*', '**', '***', 'N/A')."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    if isinstance(x, str) and re.fullmatch(r"\s*[\d,]+(\.\d+)?\s*", x):
        return float(x.replace(",", ""))
    return None


def norm(h):
    return re.sub(r"[^A-Za-z_]", "", h or "").lower()


def read_puf(py):
    import openpyxl

    wb = openpyxl.load_workbook(RAW / f"tic{py}.xlsx", read_only=True)
    ws = next(w for w in wb.worksheets if "Ind QHP" in w.title)
    hdr, issuers, reasons, plan_denied = None, {}, {}, 0.0
    for row in ws.iter_rows(values_only=True):
        if hdr is None:
            if row and "Issuer_ID" in row:
                hdr = [norm(c) for c in row]
            continue
        r = dict(zip(hdr, row))
        if not r.get("state"):
            continue
        kind = r.get("qhporsadp") or r.get("qhpsadp")
        if kind and kind != "QHP":
            continue
        add_plan_row(issuers, r)
        if (r["state"], r["issuer_id"]) in DOUBTFUL_DENIED_IN.get(py, ()):
            continue  # its plan-level reasons stay out with its issuer-level denials
        for col in REASONS:
            v = num(r.get(col if col in r else REASON_COLUMNS_2027[col]))
            if v:
                reasons[col] = reasons.get(col, 0.0) + v
        in_network = [PLAN_DENIED_IN] if PLAN_DENIED_IN in r else PLAN_DENIED_IN_2027
        plan_denied += sum(num(r.get(col)) or 0.0 for col in in_network)
    drop_doubtful_won(issuers, py)
    for key in DOUBTFUL_DENIED_IN.get(py, ()):
        issuers[key]["den_in"] = None  # a KeyError means the file changed: recheck the list
    return list(issuers.values()), reasons, plan_denied


def add_plan_row(issuers, r):
    """Fold one plan row into its insurer: claim counts once, plan types and the closed-network reason per plan."""
    key = (r["state"], r["issuer_id"])
    closed_col = CLOSED_NETWORK_2027 if CLOSED_NETWORK_2027 in r else CLOSED_NETWORK
    if key not in issuers:  # issuer fields repeat on every plan row
        issuers[key] = dict(issuer_cells(r), types=set(), closed=0.0 if closed_col in r else None)
    if r.get("plan_type"):
        issuers[key]["types"].add(str(r["plan_type"]).strip())
    if issuers[key]["closed"] is not None:
        issuers[key]["closed"] += num(r.get(closed_col)) or 0.0


def issuer_cells(r):
    """One insurer's claim and appeal counts from a plan row, keyed by normalized header."""

    def g(*keys):
        return next((num(r.get(k)) for k in keys if k in r), None)

    return dict(
        state=r["state"], issuer=str(r["issuer_name"]).strip(), id=r["issuer_id"],
        # The 2020-2021 files' one count is in-network only (CMS PY2023 data dictionary).
        rec_in=g("issuer_claims_received_in_network", "issuer_claims_received"),
        den_in=g("issuer_claims_denied_in_network", "issuer_claims_denials", "issuer_claims_denied"),
        rec_out=g("issuer_claims_received_out_of_network"), den_out=g("issuer_claims_denied_out_of_network"),
        ap_filed=g("issuer_internal_appeals_claims_filed", "issuer_internal_appeals_filed"),
        ap_over=g("issuer_number_internal_appeals_claims_overturned", "issuer_number_internal_appeals_overturned"),
    )


def drop_doubtful_won(issuers, py):
    """Blank the appeals-won count for DOUBTFUL_WON rows; appeals filed still count."""
    for key in DOUBTFUL_WON.get(py, ()):
        issuers[key]["ap_over"] = None  # a KeyError means the file changed: recheck the list


def side(i, net):
    """(claims, denied) for 'in', 'out' or 'all' (both added), or None when a cell is missing or claims are 0."""
    if net == "all":
        cells = (i["rec_in"], i["den_in"], i["rec_out"], i["den_out"])
        if None in cells:
            return None
        claims, denied = i["rec_in"] + i["rec_out"], i["den_in"] + i["den_out"]
    else:
        claims, denied = i[f"rec_{net}"], i[f"den_{net}"]
    return (claims, denied) if claims and denied is not None else None


def impossible(i):
    """More out-of-network denials than claims, which can't be right."""
    p = side(i, "out")
    return bool(p) and p[1] > p[0]


def check_impossible(issuers, py):
    """Stop when the impossible rows aren't the known ones, so a person rechecks them."""
    found = {(i["state"], i["id"]) for i in issuers if impossible(i)}
    known = KNOWN_DENIED_OVER_CLAIMS.get(py, set())
    if found != known:
        sys.exit(f"plan year {py}: more out-of-network denials than claims, new {sorted(found - known)}, "
                 f"gone {sorted(known - found)}; recheck KNOWN_DENIED_OVER_CLAIMS")
    bad_in = sorted((i["state"], i["id"]) for i in issuers if (p := side(i, "in")) and p[1] > p[0])
    if bad_in:
        sys.exit(f"plan year {py}: more in-network denials than claims for {bad_in}; the page has no rule for that")
    # side() skips 0 claims, so denials against 0 claims would slip into the all-claims totals.
    no_claims = sorted((i["state"], i["id"], net) for i in issuers for net in ("in", "out")
                       if i[f"rec_{net}"] == 0 and (i[f"den_{net}"] or 0) > 0)
    if no_claims:
        sys.exit(f"plan year {py}: denials reported against 0 claims for {no_claims}; the page has no rule for that")


def network_only(types):
    """True when every plan is an HMO or EPO, False when any isn't, None when no plan types are known."""
    return set(types) <= NETWORK_ONLY if types else None


def total(issuers, net):
    """Claims, denied, rate and insurer count on one side; impossible rows stay out of 'out' and 'all'."""
    pairs = [p for i in issuers if (p := side(i, net)) and (net == "in" or not impossible(i))]
    if not pairs:
        return None
    claims, denied = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    return dict(claims=claims, denied=denied, rate=denied / claims, issuers=len(pairs))


def rate_cell(i, net, net_only=None):
    """An insurer's out-of-network or all-claims rate, or why it has none."""
    p = side(i, net)
    if not p:
        why = "missing"
    elif impossible(i):
        why = "error"
    elif net == "out" and net_only:
        why = "net_only"
    elif net == "out" and net_only is None:
        why = "types_unknown"
    elif net == "out" and p[0] < MIN_OON_CLAIMS:
        why = "few"
    else:
        why = None
    return dict(r=None if why else round(p[1] / p[0], 4), c=int(p[0]) if p else None,
                d=int(p[1]) if p else None, why=why)


def state_views(issuers, types):
    """Per state: the all-claims rate, unless an insurer there is impossible, and who sold network-only plans."""
    out = {}
    for st in sorted({i["state"] for i in issuers}):
        here = [i for i in issuers if i["state"] == st]
        bad = sorted(i["issuer"] for i in here if impossible(i))
        al = None if bad else total(here, "all")
        kinds = [network_only(types.get((i["state"], i["id"]))) for i in here if side(i, "out")]
        out[st] = dict(
            all=dict(rate=al["rate"] if al else None, claims=al["claims"] if al else None,
                     denied=al["denied"] if al else None, issuers=al["issuers"] if al else 0,
                     why="error" if bad else None if al else "missing", left_out=bad),
            # other: sold some plan that isn't an HMO or EPO; the rest aren't in the plan list.
            out=dict(issuers=len(kinds), left_out=bad, net_only=kinds.count(True), other=kinds.count(False)),
        )
    return out


def matched_appeals(issuers):
    """Appeals filed and overturned, counting only issuers that reported both numbers."""
    both = [i for i in issuers if i["ap_filed"] is not None and i["ap_over"] is not None]
    return sum(i["ap_filed"] for i in both), sum(i["ap_over"] for i in both)


def state_rates(issuers):
    out = {}
    for i in issuers:
        if not (i["rec_in"] and i["den_in"] is not None):
            continue
        s = out.setdefault(i["state"], dict(rec=0.0, den=0.0, n=0, ap=0.0, ov=0.0))
        s["rec"] += i["rec_in"]
        s["den"] += i["den_in"]
        s["n"] += 1
        if i["ap_filed"] is not None and i["ap_over"] is not None:
            s["ap"] += i["ap_filed"]
            s["ov"] += i["ap_over"]
    return {
        k: dict(rate=v["den"] / v["rec"], denied=v["den"], claims=v["rec"], issuers=v["n"],
                appeals=v["ap"], overturned=v["ov"],
                won=(v["ov"] / v["ap"] if v["ap"] >= MIN_STATE_APPEALS else None))
        for k, v in out.items()
    }


def in_table(i):
    """An insurer the insurer table lists, so one the map's lowest-to-highest range covers."""
    return bool(i["rec_in"] and i["rec_in"] >= MIN_ISSUER_CLAIMS and i["den_in"] is not None)


def table_row(i):
    """An insurer's cells in the insurer table: its in-network rate, and appeals only on enough of them."""
    # a without o: enough appeals, but the number won was withheld.
    ok = i["ap_filed"] is not None and i["ap_filed"] >= MIN_APPEALS_FOR_RATE
    return dict(n=i["issuer"], r=round(i["den_in"] / i["rec_in"], 4), c=int(i["rec_in"]), d=int(i["den_in"]),
                a=int(i["ap_filed"]) if ok else None,
                o=int(i["ap_over"]) if ok and i["ap_over"] is not None else None)


def earlier_states(per_year, shown):
    """Each state off the latest map, from the last year CMS's files include it, kept apart from the latest year."""
    out = {}
    for py in sorted(per_year, reverse=True):
        issuers = per_year[py][0]
        for st, v in state_rates(issuers).items():
            if st in shown or st in out:
                continue
            r = sorted(round(i["den_in"] / i["rec_in"], 4) for i in issuers if i["state"] == st and in_table(i))
            out[st] = dict(year=CLAIMS_YEAR[py], py=py, rate=v["rate"], claims=v["claims"], denied=v["denied"],
                           issuers=v["issuers"], span=[r[0], r[-1]] if len(r) > 1 else None)
    return out


STATE_REPORTS_CSV = HERE / "state_reports.csv"


def load_state_reports(shown, earlier, path=STATE_REPORTS_CSV):
    """Grey states' in-network rates from their insurers' own reports on CMS's form, each row checked against its source_url.

    Only the every-state chart and the grey state's readout show them, never the map, the national figures or the ranges.
    """
    out, name, latest = {}, pathlib.Path(path).name, CLAIMS_YEAR[LATEST_PY]
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            st = r["state"]
            if st not in STATE_NAMES:
                sys.exit(f"{name}: {st!r} isn't a state code")
            if st in out:
                sys.exit(f"{name}: two rows for {st}")
            # A state with a CMS figure already has one; a second, from another source, would give it two bars.
            if st in shown or st in earlier:
                sys.exit(f"{name}: {st} already has a CMS figure")
            # A state's report can lag the map by a year; the page names each report's year.
            year = int(r["claims_year"]) if r["claims_year"].isdigit() else 0
            if year not in (latest, latest - 1):
                sys.exit(f"{name}: {st} is for {r['claims_year']} claims, too far from the map's {latest}")
            for k in ("insurers", "in_network_claims"):
                if not re.fullmatch(r"[1-9][0-9]*", r[k]):
                    sys.exit(f"{name}: {k} must be a count above 0 for {st}, not {r[k]!r}")
            if not re.fullmatch(r"[0-9]+", r["in_network_denied"]):
                sys.exit(f"{name}: in_network_denied must be a count for {st}, not {r['in_network_denied']!r}")
            claims, denied = int(r["in_network_claims"]), int(r["in_network_denied"])
            if denied > claims:
                sys.exit(f"{name}: {st} has more in-network denials than claims")
            if not r["publisher"].strip():
                sys.exit(f"{name}: {st} needs the publisher the page names")
            if not r["source_url"].startswith("https://"):
                sys.exit(f"{name}: source_url must be https for {st}")
            try:
                datetime.date.fromisoformat(r["accessed"])
            except ValueError:
                sys.exit(f"{name}: accessed must be a date like 2026-10-25 for {st}")
            out[st] = dict(year=year, rate=denied / claims, claims=claims, denied=denied, insurers=int(r["insurers"]),
                           publisher=r["publisher"].strip(), url=r["source_url"])
    return out


STATE_REPORT_INSURERS_CSV = HERE / "state_report_insurers.csv"


def load_state_report_insurers(reports, path=STATE_REPORT_INSURERS_CSV):
    """Each insurer behind a state_reports.csv row, as read_puf would give it, and the insurers that row leaves out.

    The counted rows must add up to the state's row there, so the insurer table and the every-state chart can't disagree.
    """
    out, name = {}, pathlib.Path(path).name
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            st, who = r["state"], r["insurer"].strip()
            if st not in reports:
                sys.exit(f"{name}: {st} has no row in {STATE_REPORTS_CSV.name}")
            if r["claims_year"] != str(reports[st]["year"]):
                sys.exit(f"{name}: {who} is for {r['claims_year']} claims, and {st}'s report is for {reports[st]['year']}")
            e = out.setdefault(st, dict(rows=[], left_out=[]))
            if not who or who in [i["issuer"] for i in e["rows"]] + [x["n"] for x in e["left_out"]]:
                sys.exit(f"{name}: {st} has a row with no insurer name, or two rows for {who}")
            if not r["source_url"].startswith("https://"):
                sys.exit(f"{name}: source_url must be https for {who}")
            try:
                datetime.date.fromisoformat(r["accessed"])
            except ValueError:
                sys.exit(f"{name}: accessed must be a date like 2026-10-25 for {who}")
            if r["left_out"].strip():
                e["left_out"].append(dict(n=who, why=r["left_out"].strip()))
                continue
            if not re.fullmatch(r"[1-9][0-9]*", r["in_network_claims"]) or not re.fullmatch(r"[0-9]+", r["in_network_denied"]):
                sys.exit(f"{name}: {who} needs in-network claims and denials, or a left_out reason")
            claims, denied = int(r["in_network_claims"]), int(r["in_network_denied"])
            if denied > claims:
                sys.exit(f"{name}: {who} has more in-network denials than claims; give it a left_out reason")
            if not r["page"].strip():
                sys.exit(f"{name}: {who} needs the page of its source the counts are on")
            filed, over = r["internal_appeals_filed"], r["internal_appeals_overturned"]
            if not re.fullmatch(r"([0-9]+)?", filed) or not re.fullmatch(r"([0-9]+)?", over) or (over and not filed):
                sys.exit(f"{name}: {who}'s appeals must be counts, with appeals filed wherever appeals overturned are given")
            if over and int(over) > int(filed):
                sys.exit(f"{name}: {who} has more appeals overturned than filed")
            e["rows"].append(dict(state=st, issuer=who, rec_in=float(claims), den_in=float(denied),
                                  ap_filed=float(filed) if filed else None, ap_over=float(over) if over else None))
    for st, rep in reports.items():
        rows = out.get(st, {}).get("rows", [])
        got = (len(rows), sum(i["rec_in"] for i in rows), sum(i["den_in"] for i in rows))
        if got != (rep["insurers"], rep["claims"], rep["denied"]):
            sys.exit(f"{name}: {st}'s counted insurers give (insurers, claims, denied) {got}, "
                     f"and {STATE_REPORTS_CSV.name} has {(rep['insurers'], rep['claims'], rep['denied'])}")
    return out


def grey_issuers(per_year, earlier, reports, report_insurers):
    """The insurer table for grey states: CMS's last year for each earlier state, and each state report's insurers.

    Same rules as the latest year's table. Each says its year and source, and none joins the map or its ranges.
    """
    def entry(rows, **meta):
        return dict(meta, rows=sorted((table_row(i) for i in rows if in_table(i)), key=lambda x: x["n"]))

    out = {st: entry([i for i in per_year[e["py"]][0] if i["state"] == st], year=e["year"], kind="earlier",
                     publisher="CMS", url=None, appeals=True, left_out=[])
           for st, e in earlier.items()}
    for st, ins in report_insurers.items():
        rep = reports[st]
        # A report with no appeal counts, such as Covered California's, gets no appeals column.
        out[st] = entry(ins["rows"], year=rep["year"], kind="state", publisher=rep["publisher"], url=rep["url"],
                        appeals=any(i["ap_filed"] is not None for i in ins["rows"]),
                        left_out=sorted(ins["left_out"], key=lambda x: x["n"]))
    return out


def build_denials():
    trend, per_year = [], {}
    for py, cy in CLAIMS_YEAR.items():
        issuers, reasons, plan_denied = read_puf(py)
        check_impossible(issuers, py)
        per_year[py] = (issuers, reasons, plan_denied)
        filed, over = matched_appeals(issuers)
        t = dict(year=cy, days=366 if cy % 4 == 0 else 365, appeals=filed, overturned=over,
                 appeals_all=sum(i["ap_filed"] or 0 for i in issuers),
                 states=len({i["state"] for i in issuers}), issuers=len(issuers),
                 left_out=[dict(s=i["state"], id=i["id"], n=i["issuer"], claims=i["rec_out"], denied=i["den_out"])
                           for i in issuers if impossible(i)])
        # Out-of-network and all claims are null before 2022 claims, which weren't split.
        for net in ("in", "out", "all"):
            tot = total(issuers, net) or {}
            for k in ("claims", "denied", "rate", "issuers"):
                t[f"{net}_{k}"] = tot.get(k)
        trend.append(t)

    issuers, reasons, plan_denied = per_year[LATEST_PY]
    types = {(i["state"], i["id"]): i["types"] for i in per_year[PLAN_TYPES_PY][0]}
    states = state_rates(issuers)
    prev = state_rates(per_year[LATEST_PY - 1][0])
    views = state_views(issuers, types)
    for k, v in states.items():
        if k in prev:
            v["prev"] = prev[k]["rate"]
        v.update(views[k])

    table = []
    for i in issuers:
        if not in_table(i):
            continue
        pt = types.get((i["state"], i["id"]))
        net_only = network_only(pt)
        table.append(dict(table_row(i), s=i["state"], pt=sorted(pt) if pt else None, net_only=net_only,
                          out=rate_cell(i, "out", net_only), all=rate_cell(i, "all")))

    reason_rows = sorted(({"k": REASONS[k], "v": v} for k, v in reasons.items()), key=lambda x: -x["v"])
    earlier = earlier_states(per_year, set(states))
    reports = load_state_reports(set(states), earlier)
    return dict(trend=trend, states=states, issuers=table, reasons=reason_rows,
                reason_meta=dict(entries=sum(reasons.values()), plan_denied_in=plan_denied),
                oon_meta=oon_meta(issuers, types), earlier=earlier, state_reports=reports,
                grey_issuers=grey_issuers(per_year, earlier, reports, load_state_report_insurers(reports)))


def oon_meta(issuers, types):
    """The out-of-network rules, and how the latest year's reporting insurers split by plan type."""
    rows = [i for i in issuers if side(i, "out") and not impossible(i)]
    kind = {True: "net_only", False: "other", None: "unknown"}
    groups = {k: [] for k in kind.values()}
    for i in rows:
        groups[kind[network_only(types.get((i["state"], i["id"])))]].append(i)
    # Plan-level and not exclusive (one claim can count under several reasons), so only a rough share.
    closed = sum(i["closed"] or 0.0 for i in rows)
    out = total(rows, "out")
    return dict(min_claims=MIN_OON_CLAIMS, plan_types_year=PLAN_TYPES_PY,
                net_only=total(groups["net_only"], "out"), other=total(groups["other"], "out"),
                unknown_plan_types=sorted(i["issuer"] for i in groups["unknown"]),
                closed_network=dict(denied=closed, share=closed / out["denied"] if out else None))


IMR_LABELS = {
    "Mental Health": "Mental health care", "Pain Management": "Pain management",
    "Ear-Nose-Thro Proc": "Ear, nose & throat procedures", "Pharmacy": "Prescription drugs",
    "Acute Med Svc Inpt": "Inpatient hospital care", "Cardio-Vasc Proc": "Heart & vascular procedures",
    "DME": "Medical equipment", "Gen Surg Proc": "General surgery", "Cancer Care": "Cancer care",
    "Autism Related Tx": "Autism treatment", "Diag Imag & Screen": "Imaging & screening",
    "Reconstr/Plast Proc": "Reconstructive surgery", "Rehab/ Svc - Outpt": "Outpatient rehab",
    "Orthopedic Proc": "Orthopedic procedures", "Rehab/Svcs SNF Inpt": "Skilled nursing / inpatient rehab",
    "Elect/Therm/Radfreq": "Nerve & radiofrequency treatments", "Diag/ MD Eval": "Diagnostic evaluations",
    "Special Proc": "Special procedures",
}


def build_imr():
    import pandas as pd

    d = pd.read_csv(RAW / "imr.csv", usecols=["ReferenceID", "ReportYear", "TreatmentCategory", "Determination"])
    before = len(d)
    d = d.drop_duplicates()
    dup_ids = int(d.ReferenceID.duplicated().sum())
    d["o"] = d.Determination.str.startswith("Overturned")
    trend = [dict(y=int(y), n=int(g.size), r=round(float(g.mean()), 3))
             for y, g in d.groupby("ReportYear").o if 2010 <= y <= 2025]
    recent = d[(d.ReportYear >= 2021) & (d.ReportYear <= 2025)]
    cats = [dict(k=IMR_LABELS.get(k, k), n=int(g.size), r=round(float(g.mean()), 3))
            for k, g in recent.groupby("TreatmentCategory").o if g.size >= 100]
    cats.sort(key=lambda x: -x["r"])
    return dict(imr_trend=trend, imr_cat=cats,
                imr_meta=dict(duplicates_dropped=before - len(d), duplicate_ids_left=dup_ids))


def build_drugs():
    import pandas as pd

    p = pd.read_csv(RAW / "partd.csv")
    p = p[p.Mftr_Name == "Overall"].copy()
    cols = ["Brnd_Name", "Gnrc_Name", "Tot_Spndng_2024", "Tot_Benes_2024", "Avg_Spnd_Per_Bene_2024",
            "Tot_Spndng_2020", "Avg_Spnd_Per_Clm_2024"]
    for c in cols[2:]:
        p[c] = pd.to_numeric(p[c], errors="coerce")

    def rows(df):
        out = []
        for t in df[cols].itertuples(index=False):
            out.append({k: (None if isinstance(v, float) and math.isnan(v)
                            else v.strip().rstrip("*") if isinstance(v, str) else float(v))
                        # CMS puts "*" on a brand name when its spending per dosage
                        # unit mixes routes of administration (data dictionary).
                        # Nothing here shows a per-unit figure, so the mark is dropped.
                        for k, v in zip(cols, t)})
        return out

    g = p[p.Tot_Spndng_2020 >= 1e8].copy()
    g["x"] = g.Tot_Spndng_2024 / g.Tot_Spndng_2020
    lists = dict(
        top=rows(p.nlargest(10, "Tot_Spndng_2024")),
        per=rows(p[p.Tot_Benes_2024 >= 1000].nlargest(10, "Avg_Spnd_Per_Bene_2024")),
        growth=rows(g.nlargest(10, "x")),
        cheap=rows(p[p.Tot_Benes_2024 >= 2_000_000].nsmallest(10, "Avg_Spnd_Per_Clm_2024")),
    )
    attach_drug_uses(lists, load_drug_uses())
    attach_medicare_prices(lists, load_medicare_prices())
    # No 2020 total: this file lists only drugs still dispensed in 2024.
    return dict(total=dict(y2024=float(p.Tot_Spndng_2024.sum())), **lists)


DRUG_USES_CSV = HERE / "drug_uses.csv"


def load_drug_uses(path=DRUG_USES_CSV):
    """Brand listing -> plain main use, each checked against its source_url."""
    with open(path, newline="") as f:
        return {r["brand"]: r["use"] for r in csv.DictReader(f)}


def attach_drug_uses(lists, uses):
    """Label every shown drug, or stop so a refresh can't ship one unlabelled."""
    missing = sorted({r["Brnd_Name"] for rows in lists.values() for r in rows} - uses.keys())
    if missing:
        sys.exit(f"add a checked use to {DRUG_USES_CSV.name} for: {', '.join(missing)}")
    for rows in lists.values():
        for r in rows:
            r["use"] = uses[r["Brnd_Name"]]


MEDICARE_PRICES_CSV = HERE / "medicare_prices.csv"


def medicare_price_label(first, last, published):
    """The tag on a drug with a Medicare-negotiated price, by year, in plain words."""
    if not published:
        return f"Picked for a Medicare price in {first}"
    if last == first:
        return f"Medicare price in {first} only"
    if last:
        return f"Medicare price {first} to {last}"
    return f"Medicare price from {first}"


def load_medicare_prices(path=MEDICARE_PRICES_CSV):
    """Brand listing -> its Medicare-negotiated price years, each checked against its CMS source_url."""
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            first = int(r["price_from"])
            last = int(r["price_until"]) if r["price_until"] else None
            # Stop on a typo rather than ship a wrong tag.
            if r["price_published"] not in ("yes", "no"):
                sys.exit(f"{pathlib.Path(path).name}: price_published must be yes or no for {r['brand']}")
            if last is not None and last < first:
                sys.exit(f"{pathlib.Path(path).name}: price_until is before price_from for {r['brand']}")
            if not r["source_url"].startswith("https://"):
                sys.exit(f"{pathlib.Path(path).name}: source_url must be https for {r['brand']}")
            label = medicare_price_label(first, last, r["price_published"] == "yes")
            out[r["brand"]] = {"from": first, "until": last, "label": label}
    return out


def attach_medicare_prices(lists, prices):
    """Tag each shown drug that has a Medicare-negotiated price; the rest get no tag."""
    for rows in lists.values():
        for r in rows:
            if r["Brnd_Name"] in prices:
                r["medicare_price"] = dict(prices[r["Brnd_Name"]])


def build_sahie():
    out = {}
    for y in (2023, 2024):
        lines = (RAW / f"sahie_{y}.csv").read_text(encoding="latin-1").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("year"))
        yr = {}
        for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
            k = {a.strip(): b.strip() for a, b in row.items() if a}
            if (k["geocat"], k["agecat"], k["racecat"], k["sexcat"], k["iprcat"]) == ("40", "0", "0", "0", "0"):
                yr[k["state_name"]] = dict(pct=float(k["PCTUI"]), n=int(k["NUI"]))
        out[str(y)] = yr
    return out


PLACES = {"GHLTH": "fair_poor_health", "DIABETES": "diabetes", "DEPRESSION": "depression"}


def build_places():
    import pandas as pd

    d = pd.read_csv(RAW / "places_county.csv", dtype=str)
    # The v3 export writes populations with thousands commas.
    d["p"] = pd.to_numeric(d.TotalPop18plus.str.replace(",", ""), errors="coerce")
    res = {}
    for st, g in d.groupby("StateAbbr"):
        res[st] = {}
        for m, name in PLACES.items():
            v = pd.to_numeric(g[m + "_CrudePrev"], errors="coerce")
            w = g.p[v.notna()]
            if w.sum():
                res[st][name] = round(float((v[v.notna()] * w).sum() / w.sum()), 1)
    return res


def correlations(sahie, places):
    """State-level Pearson r between the uninsured rate and each health measure (for the copy)."""
    import pandas as pd

    abbr = {n: a for a, n in STATE_NAMES.items()}
    out = {}
    for m in PLACES.values():
        xs, ys = [], []
        for name, v in sahie["2024"].items():
            a = abbr.get(name)
            if a in places and m in places[a]:
                xs.append(v["pct"])
                ys.append(places[a][m])
        out[m] = round(float(pd.Series(xs).corr(pd.Series(ys))), 2)
    return out


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


PRIOR_AUTH_CSV = HERE / "prior_auth_2025.csv"


def build_prior_auth(path=PRIOR_AUTH_CSV):
    """Insurers' own 2025 prior-authorization reports (CMS-0057-F), as posted.

    prior_auth_2025.csv is hand-curated: each row was copied from the
    insurer's or state's own page (source_url), with a plain display name and
    place. Rows whose scope or units were unclear are left out of that file.
    """
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(
                line=r["line"], n=r["insurer"], where=r["where"],
                std=float(r["std_denied_pct"]) / 100 if r["std_denied_pct"] else None,
                exp=float(r["exp_denied_pct"]) / 100 if r["exp_denied_pct"] else None,
                std_posted=r["std_denied_pct"] or None, exp_posted=r["exp_denied_pct"] or None,
                req=int(r["std_requests"]) if r["std_requests"] else None,
                unit=r["unit"],
                note=r["note"], url=r["source_url"],
            ))
    return rows


# data.cms.gov keeps only its latest effectuated file, so the build checks it has the one the copy was written from.
EFFECTUATED_FILE = "Health Insurance Exchanges Monthly Effectuated Enrollment Data_asof_20260505.csv"
EFFECTUATED_AS_OF = "2026-05-05"
EFFECTUATED_SHA256 = "4611c6aa318499668440d7e5b76796d143eecfb895a38646b7eaa46d064c9eb0"
MARKETPLACE_SIGNUPS_CSV = HERE / "marketplace_signups.csv"
OE_HISTORY_CSV = HERE / "open_enrollment_history.csv"
OE_HISTORY_FIRST = 2014
# CMS has no all-marketplace file before 2017, so 2014 to 2016 come from HHS ASPE's reports, whose tables cite CMS.
OE_HISTORY_PUBLISHERS = ("CMS", "HHS ASPE, from CMS data")
OE_HISTORY_HOSTS = ("www.cms.gov", "aspe.hhs.gov", "www.aspe.hhs.gov")


def february_totals(rows):
    """National February coverage in effect by year, and the one year that isn't final yet.

    Every year comes from one file: earlier CMS reports counted the same month
    on an earlier date and give a higher number.
    """
    feb, states, decembers = {}, {}, set()
    for r in rows:
        y = int(r["Year"])
        if r["State"] == "Total":
            if r["Platform"] != "All":
                continue
            if r["Month"] == "December":
                decembers.add(y)
            if r["Month"] == "February":
                if y in feb:
                    sys.exit(f"effectuated file: two national February rows for {y}")
                feb[y] = int(r["Effectuated_Enr"])
        elif r["Month"] == "February":
            states.setdefault(y, {})[r["State"]] = int(r["Effectuated_Enr"])
    years = sorted(feb)
    if not years or years != list(range(years[0], years[-1] + 1)):
        sys.exit(f"effectuated file: the February years have a gap: {years}")
    for y in years:
        if feb[y] <= 0:
            sys.exit(f"effectuated file: February {y} has a national count of {feb[y]}")
        if sum(states.get(y, {}).values()) != feb[y]:
            sys.exit(f"effectuated file: the state rows for February {y} don't add up to the national total")
    # CMS restates a year's months until its December is in, so only a year without one may be open.
    open_years = [y for y in years if y not in decembers]
    if open_years not in ([], [years[-1]]):
        sys.exit(f"effectuated file: years without a December {open_years}; only the latest may be open")
    out = [dict(y=y, n=feb[y], mn=states[y].get("MN")) for y in years]
    return out, (open_years[0] if open_years else None)


def load_marketplace_signups(path=MARKETPLACE_SIGNUPS_CSV, zip_dir=None):
    """Plans picked in each open enrollment, HealthCare.gov and state-run marketplaces together.

    file_sha256 is recorded when the zip is fetched; a zip found in zip_dir is checked against it.
    """
    rows, name = [], pathlib.Path(path).name
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if not re.fullmatch(r"20[0-9]{2}", r["year"]):
                sys.exit(f"{name}: year must be a year like 2026, not {r['year']!r}")
            if not re.fullmatch(r"[1-9][0-9]*", r["plan_selections"]):
                sys.exit(f"{name}: plan_selections must be a count above 0 for {r['year']}, not {r['plan_selections']!r}")
            if not r["source_url"].startswith("https://www.cms.gov/"):
                sys.exit(f"{name}: source_url must be a cms.gov page for {r['year']}")
            # The zip and its sha256 are the record of which CMS file the count came from.
            if not (r["file"].startswith("https://www.cms.gov/files/zip/") and r["file"].endswith(".zip")):
                sys.exit(f"{name}: file must be the CMS zip the count came from for {r['year']}")
            if not re.fullmatch(r"[0-9a-f]{64}", r["file_sha256"]):
                sys.exit(f"{name}: file_sha256 must be the zip's sha256 for {r['year']}")
            try:
                datetime.date.fromisoformat(r["accessed"])
            except ValueError:
                sys.exit(f"{name}: accessed must be a date like 2026-10-22 for {r['year']}")
            if zip_dir is not None:
                check_signup_zip(pathlib.Path(zip_dir) / r["file"].rsplit("/", 1)[-1], r["file_sha256"], f"{name} {r['year']}")
            y = int(r["year"])
            if any(x["y"] == y for x in rows):
                sys.exit(f"{name}: two rows for {y}")
            rows.append(dict(y=y, n=int(r["plan_selections"]), url=r["source_url"]))
    if not rows:
        sys.exit(f"{name}: no plan picks")
    return sorted(rows, key=lambda r: r["y"])


def load_open_enrollment_history(picks, path=OE_HISTORY_CSV):
    """Plans picked in every open enrollment from 2014, with the HealthCare.gov and state-run split.

    A year also in marketplace_signups.csv (picks) must give the same count, so the page's two tables can't disagree.
    """
    rows, name = [], pathlib.Path(path).name
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if not re.fullmatch(r"20[0-9]{2}", r["year"]):
                sys.exit(f"{name}: year must be a year like 2026, not {r['year']!r}")
            for k in ("plan_selections", "healthcare_gov", "state_based"):
                if not re.fullmatch(r"[1-9][0-9]*", r[k]):
                    sys.exit(f"{name}: {k} must be a count above 0 for {r['year']}, not {r[k]!r}")
            y, n, fed, sbm = int(r["year"]), int(r["plan_selections"]), int(r["healthcare_gov"]), int(r["state_based"])
            if n != fed + sbm:
                sys.exit(f"{name}: plan_selections for {y} isn't healthcare_gov plus state_based ({fed:,} + {sbm:,} = {fed + sbm:,}, not {n:,})")
            if r["publisher"] not in OE_HISTORY_PUBLISHERS:
                sys.exit(f"{name}: publisher must be one of {OE_HISTORY_PUBLISHERS} for {y}, not {r['publisher']!r}")
            url = urllib.parse.urlsplit(r["source_url"])
            if url.scheme != "https" or url.hostname not in OE_HISTORY_HOSTS:
                sys.exit(f"{name}: source_url must be https on {', '.join(OE_HISTORY_HOSTS)} for {y}")
            if not r["source_file"].strip():
                sys.exit(f"{name}: source_file must name the file the count came from for {y}")
            rows.append(dict(y=y, n=n, fed=fed, sbm=sbm, pub=r["publisher"]))
    rows.sort(key=lambda r: r["y"])
    years = [r["y"] for r in rows]
    if not years or years != list(range(OE_HISTORY_FIRST, OE_HISTORY_FIRST + len(years))):
        sys.exit(f"{name}: years must run from {OE_HISTORY_FIRST} without a gap or a repeat, not {years}")
    counts = {r["y"]: r["n"] for r in rows}
    for p in picks:
        if p["y"] in counts and counts[p["y"]] != p["n"]:
            sys.exit(f"{name}: {p['y']} has {counts[p['y']]:,} plans picked, and {MARKETPLACE_SIGNUPS_CSV.name} has {p['n']:,}")
    return rows


def check_signup_zip(path, sha256, where):
    if not path.exists():
        print(where, f"{path.name} isn't in the raw directory, so its sha256 wasn't checked")
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        sys.exit(f"{where}: {path} has sha256 {digest}, not the file_sha256 recorded for it")
    print(where, f"{path.name} matches its recorded sha256")


def marketplace_entry(feb, prelim, picks, history=()):
    """The bundle's marketplace data, once the plan picks cover the Februaries the page compares.

    history is every open enrollment's plan picks, for the page's long line; it isn't compared with February.
    """
    years = [r["y"] for r in feb]
    extra = sorted({p["y"] for p in picks} - set(years))
    if extra:
        sys.exit(f"plan picks for {extra} have no February count to compare with")
    # The page compares the last two Februaries, each with that year's plan picks.
    if len(years) < 2:
        sys.exit(f"effectuated file: the page compares two Februaries, and the file has {years}")
    missing = sorted(set(years[-2:]) - {p["y"] for p in picks})
    if missing:
        sys.exit(f"plan picks have no row for {missing}; the page compares February {years[-2]} and {years[-1]} with them")
    return dict(feb=feb, prelim=prelim, as_of=EFFECTUATED_AS_OF, file=EFFECTUATED_FILE, picks=picks, history=list(history))


def marketplace_report(mk):
    """The yearly changes and plan-pick shares to check by eye, worked out before the bundle is written."""
    feb = {r["y"]: r["n"] for r in mk["feb"]}
    lines = [f"marketplace February {b['y']} {b['n']:,} {b['n'] / a['n'] - 1:+.2%}" + (" not final" if b["y"] == mk["prelim"] else "")
             for a, b in zip(mk["feb"], mk["feb"][1:])]
    lines += [f"plan picks {p['y']} {p['n']:,} February coverage {feb[p['y']] / p['n']:.1%} of them" for p in mk["picks"]]
    lines += [f"open enrollment {h['y']} {h['n']:,} picked ({h['pub']})" for h in mk.get("history", [])]
    return lines


def build_marketplace(path=None, signups_path=MARKETPLACE_SIGNUPS_CSV, history_path=OE_HISTORY_CSV):
    path = path or RAW / "effectuated_monthly.csv"
    digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    if digest != EFFECTUATED_SHA256:
        sys.exit(f"{path} isn't {EFFECTUATED_FILE} (sha256 {digest}): update EFFECTUATED_FILE, "
                 "EFFECTUATED_AS_OF and EFFECTUATED_SHA256, then reread the page's marketplace copy")
    with open(path, newline="", encoding="utf-8-sig") as f:
        feb, prelim = february_totals(csv.DictReader(f))
    picks = load_marketplace_signups(signups_path, zip_dir=RAW)
    return marketplace_entry(feb, prelim, picks, load_open_enrollment_history(picks, history_path))


# Where people get coverage: FHI's tally of Census's CPS ASEC public-use file, which cps_coverage_mix.py writes.
COVERAGE_MIX_CSV = HERE / "coverage_mix.csv"
COVERAGE_MIX_SURVEY = dict(
    name="Current Population Survey, 2026 Annual Social and Economic Supplement",
    page="https://www.census.gov/data/datasets/time-series/demo/cps/cps-asec.2026.html",
    file="https://www2.census.gov/programs-surveys/cps/datasets/2026/march/asecpub26csv.zip",
    sha256="fe819d0d2fc4470c282e76c247b8aa8b6054811619730307e9f2fe6186707d93",
    released="2026-09-15",
)
# The donut's slices in the page's order; the page names the marketplace one.
COVERAGE_MIX_KEYS = ("job", "marketplace", "other_individual", "medicare", "medicaid_chip", "military_va", "more_than_one", "uninsured")


def load_coverage_mix(path=COVERAGE_MIX_CSV):
    """Everyone at the survey's interview, each person counted once, for the donut both pages draw.

    The CSV also keeps the calendar-year measure and the under-65 and 65-and-over rows, as a record; the page uses one donut.
    """
    name = pathlib.Path(path).name
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["measure"] == "now" and r["age"] == "all"]
    keys = [r["key"] for r in rows]
    if keys != list(COVERAGE_MIX_KEYS):
        sys.exit(f"{name}: the at-interview, all-ages rows must be {', '.join(COVERAGE_MIX_KEYS)} in that order, not {keys}")
    if len({r["reference_period"] for r in rows}) != 1:
        sys.exit(f"{name}: the at-interview rows give more than one reference period")
    out = []
    for r in rows:
        n, pct, pct_moe = int(r["people"]), float(r["percent"]), float(r["percent_moe90"])
        if n <= 0 or not 0 < pct < 100:
            sys.exit(f"{name}: {r['key']} has {n:,} people, {pct}%")
        # NaN or infinity would be written as JSON no browser can parse, and every chart on the page would fail.
        if not (math.isfinite(pct_moe) and pct_moe >= 0) or int(r["people_moe90"]) < 0:
            sys.exit(f"{name}: {r['key']} has a margin of error of {r['people_moe90']} people, {r['percent_moe90']}%")
        out.append(dict(k=r["key"], label=r["category"], n=n, moe=int(r["people_moe90"]), pct=pct, pct_moe=pct_moe))
    # Each person is in one slice, so the shares add to 100 but for rounding.
    total = sum(r["pct"] for r in out)
    if abs(total - 100) > 0.05:
        sys.exit(f"{name}: the shares add to {total:.2f}%, not 100%")
    return dict(COVERAGE_MIX_SURVEY, period=rows[0]["reference_period"], rows=out)


# People under 65 on July 1 of each year, the marketplace chart's comparison: the 2010-2020 intercensal series to 2019 and
# Vintage 2025 from 2020. Census redoes 2020 on with each vintage, so a newer one replaces Vintage 2025 for every year from 2020.
POPULATION_CSV = HERE / "population_under_65.csv"
POPULATION_SERIES = {
    "2010-2020 intercensal": (range(2010, 2020), "nc-est2020int-agesex-res.csv"),
    "Vintage 2025": (range(2020, 2026), "nc-est2025-agesex-res.csv"),
}
POPULATION_HOST = "www2.census.gov"


def load_population(feb_years, path=POPULATION_CSV, raw_dir=None):
    """People under 65 on July 1, for the July before every February the marketplace chart shows.

    A Census file found in raw_dir is checked against its sha256 and its counts are worked out again.
    """
    rows, files, name = [], {}, pathlib.Path(path).name
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            y = int(r["year"])
            years, file = POPULATION_SERIES.get(r["series"], ((), None))
            if y not in years or r["source_file"] != file:
                sys.exit(f"{name}: {y} must come from {next((s for s, (ys, _) in POPULATION_SERIES.items() if y in ys), 'no series')}, not {r['series']} ({r['source_file']})")
            url = urllib.parse.urlsplit(r["source_url"])
            if url.scheme != "https" or url.hostname != POPULATION_HOST or not url.path.endswith("/" + file):
                sys.exit(f"{name}: source_url for {y} must be the {file} link on {POPULATION_HOST}")
            if not re.fullmatch(r"[0-9a-f]{64}", r["file_sha256"]) or files.setdefault(file, r["file_sha256"]) != r["file_sha256"]:
                sys.exit(f"{name}: {y} needs the one sha256 of {file}")
            under_65, total = int(r["under_65"]), int(r["total"])
            if not 0 < under_65 < total:
                sys.exit(f"{name}: {y} has {under_65:,} under 65 of {total:,}")
            rows.append(dict(y=y, n=under_65, total=total, series=r["series"], file=file))
    rows.sort(key=lambda r: r["y"])
    years = [r["y"] for r in rows]
    if not years or years != list(range(years[0], years[0] + len(years))):
        sys.exit(f"{name}: years must run without a gap or a repeat, not {years}")
    missing = sorted({y - 1 for y in feb_years} - set(years))
    if missing:
        sys.exit(f"{name}: no July {missing} for the Februaries after them")
    if raw_dir is not None:
        for file, sha256 in files.items():
            check_population_file(pathlib.Path(raw_dir) / file, sha256, [r for r in rows if r["file"] == file])
    return dict(under_65=[dict(y=r["y"], n=r["n"]) for r in rows],
                files=[dict(series=s, file=f, years=f"{min(r['y'] for r in rows if r['file'] == f)}-{max(r['y'] for r in rows if r['file'] == f)}")
                       for s, (_, f) in POPULATION_SERIES.items() if f in files])


def check_population_file(path, sha256, rows):
    """Census's national file by single year of age: everyone (SEX 0), ages 0 to 64, in POPESTIMATE<year>."""
    if not path.exists():
        print(f"{path.name} isn't in the raw directory, so its counts weren't checked")
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        sys.exit(f"{path} has sha256 {digest}, not the one population_under_65.csv records")
    with open(path, newline="") as f:
        people = [r for r in csv.DictReader(f) if r["SEX"] == "0"]
    for r in rows:
        col = f"POPESTIMATE{r['y']}"
        total = next(int(p[col]) for p in people if p["AGE"] == "999")
        under_65 = sum(int(p[col]) for p in people if p["AGE"] != "999" and int(p["AGE"]) < 65)
        if (under_65, total) != (r["n"], r["total"]):
            sys.exit(f"{path.name}: July {r['y']} has {under_65:,} under 65 of {total:,}, not {r['n']:,} of {r['total']:,}")
    print(f"{path.name} matches its sha256 and population_under_65.csv")


# The records behind the page's other counts: Medicare and Medicaid and CHIP in the latest marketplace February.
COVERAGE_COUNTS_CSV = HERE / "coverage_counts.csv"
COVERAGE_COUNTS = {"Medicare": "medicare", "Medicaid and CHIP": "medicaid_chip"}
COVERAGE_COUNTS_HOSTS = ("data.cms.gov", "download.medicaid.gov", "data.medicaid.gov")


def load_coverage_counts(month, path=COVERAGE_COUNTS_CSV):
    """Medicare and Medicaid and CHIP for the month given (the latest marketplace February), one checked row each."""
    out, name = {}, pathlib.Path(path).name
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = COVERAGE_COUNTS.get(r["coverage"])
            if key is None or key in out:
                sys.exit(f"{name}: one row each for {', '.join(COVERAGE_COUNTS)}, not {r['coverage']!r} again or instead")
            if r["month"] != month:
                sys.exit(f"{name}: {r['coverage']} is for {r['month']}, and the page compares {month}")
            if not re.fullmatch(r"[1-9][0-9]*", r["people"]):
                sys.exit(f"{name}: {r['coverage']} needs a count above 0, not {r['people']!r}")
            for col in ("source_url", "source_page"):
                url = urllib.parse.urlsplit(r[col])
                if url.scheme != "https" or url.hostname not in COVERAGE_COUNTS_HOSTS:
                    sys.exit(f"{name}: {col} for {r['coverage']} must be https on {', '.join(COVERAGE_COUNTS_HOSTS)}")
            if not re.fullmatch(r"[0-9a-f]{64}", r["file_sha256"]) or not r["source_file"].strip() or not r["how"].strip():
                sys.exit(f"{name}: {r['coverage']} needs the file it came from, its sha256 and how it was counted")
            try:
                datetime.date.fromisoformat(r["accessed"])
            except ValueError:
                sys.exit(f"{name}: accessed must be a date like 2026-10-26 for {r['coverage']}")
            out[key] = dict(n=int(r["people"]), page=r["source_page"], file=r["source_file"])
    if set(out) != set(COVERAGE_COUNTS.values()):
        sys.exit(f"{name}: needs a row for each of {', '.join(COVERAGE_COUNTS)}")
    return dict(month=month, **out)


def coverage_report(bundle):
    """The coverage donut's shares and the population comparison, to check by eye against the page's copy."""
    mix, feb = bundle["coverage_mix"], bundle["marketplace"]["feb"]
    under_65 = {r["y"]: r["n"] for r in bundle["population"]["under_65"]}
    lines = [f"coverage {mix['period']}: {r['label']} {r['pct']:.1f}% (MOE {r['pct_moe']})" for r in mix["rows"]]
    first = feb[0]
    for r in feb:
        u = under_65[r["y"] - 1]
        lines.append(f"February {r['y']} marketplace {r['n'] / first['n'] - 1:+.1%} since {first['y']}, under 65 (July {r['y'] - 1}) "
                     f"{u / under_65[first['y'] - 1] - 1:+.2%}, {100 * r['n'] / u:.1f} per 100 under 65")
    counts = bundle["coverage_counts"]
    lines.append(f"{counts['month']}: Medicare {counts['medicare']['n']:,}, Medicaid and CHIP {counts['medicaid_chip']['n']:,}")
    return lines


SOURCES = [
    dict(name="CMS Transparency in Coverage public use files", years="plan years 2022-2027 (claims 2020-2025)",
         url="https://www.cms.gov/marketplace/resources/data/public-use-files"),
    dict(name="California DMHC Independent Medical Review determinations", years="2010-2025",
         url="https://data.chhs.ca.gov/dataset/independent-medical-review-imr-determinations-trend"),
    dict(name="Census Small Area Health Insurance Estimates", years="2023-2024",
         url="https://www.census.gov/programs-surveys/sahie.html"),
    dict(name="CDC PLACES county data", years="2025 release",
         url="https://data.cdc.gov/d/i46a-9kgh"),
    dict(name="Insurers' and states' own prior-authorization reports (CMS-0057-F)", years="2025",
         url="https://www.cms.gov/initiatives/burden-reduction/overview/interoperability/policies-regulations/cms-interoperability-prior-authorization-final-rule-cms-0057-f"),
    dict(name="CMS Medicare Part D Spending by Drug", years="2020-2024",
         url="https://data.cms.gov/summary-statistics-on-use-and-payments/medicare-medicaid-spending-by-drug/medicare-part-d-spending-by-drug"),
    dict(name="CMS Medicare drug price negotiation: selected drugs and negotiated prices", years="2026-2028",
         url="https://www.cms.gov/initiatives/medicare-prescription-drug-affordability/overview/medicare-drug-price-negotiation-program/selected-drugs-negotiated-prices"),
    dict(name="CMS Health Insurance Exchanges Monthly Effectuated Enrollment", years="February 2016-2026",
         url="https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/health-insurance-marketplace/health-insurance-exchanges-monthly-effectuated-enrollment"),
    dict(name="CMS Marketplace Open Enrollment Period public use files", years="2025",
         url="https://www.cms.gov/data-research/statistics-trends-reports/marketplace-products/2025-marketplace-open-enrollment-period-public-use-files"),
    dict(name="CMS Marketplace Open Enrollment Period public use files", years="2026",
         url="https://www.cms.gov/data-research/statistics-trends-reports/marketplace-products/2026-marketplace-open-enrollment-period-public-use-files"),
    dict(name="CMS Marketplace Open Enrollment Period state-level public use files", years="2017-2026",
         url="https://www.cms.gov/data-research/statistics-trends-and-reports/marketplace-products"),
    dict(name="HHS ASPE Health Insurance Marketplace enrollment report, from CMS data", years="2014",
         url="https://www.aspe.hhs.gov/sites/default/files/migrated_legacy_files/44226/ib_2014Apr_enrollment.pdf"),
    dict(name="HHS ASPE Health Insurance Marketplace enrollment report, from CMS data", years="2015",
         url="https://www.aspe.hhs.gov/sites/default/files/migrated_legacy_files/137886/ib_2015mar_enrollment.pdf"),
    dict(name="HHS ASPE Health Insurance Marketplace enrollment report, from CMS data", years="2016",
         url="https://aspe.hhs.gov/sites/default/files/private/pdf/187866/Finalenrollment2016.pdf"),
    dict(name="Census Current Population Survey, Annual Social and Economic Supplement, public-use file (FHI's tally)", years="2026",
         url=COVERAGE_MIX_SURVEY["page"]),
    dict(name="Census population estimates by single year of age, 2010-2020 intercensal", years="July 2015-2019",
         url="https://www2.census.gov/programs-surveys/popest/datasets/2010-2020/intercensal/national/asrh/nc-est2020int-agesex-res.csv"),
    dict(name="Census population estimates by single year of age, Vintage 2025", years="July 2020-2025",
         url="https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/national/asrh/nc-est2025-agesex-res.csv"),
    dict(name="CMS Medicare Monthly Enrollment", years="February 2026",
         url="https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/medicare-and-medicaid-reports/medicare-monthly-enrollment"),
    dict(name="State Medicaid and CHIP Applications, Eligibility Determinations, and Enrollment Data", years="February 2026",
         url="https://data.medicaid.gov/dataset/6165f45b-ca93-5bb5-9d06-db29c692a360"),
]


def main(argv=None):
    global RAW
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", type=pathlib.Path, default=RAW, help="directory holding the downloaded source files")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    RAW = args.raw_dir
    missing = [n for n in [f"tic{py}.xlsx" for py in CLAIMS_YEAR] + ["imr.csv", "partd.csv", "sahie_2023.csv", "sahie_2024.csv", "places_county.csv", "effectuated_monthly.csv"]
               if not (RAW / n).exists()]
    if missing:
        sys.exit(f"missing source files in {RAW}: {', '.join(missing)} (see README.md)")

    bundle = build_denials()
    bundle.update(build_imr())
    bundle["drugs"] = build_drugs()
    bundle["sahie"] = build_sahie()
    bundle["places"] = build_places()
    bundle["corr"] = correlations(bundle["sahie"], bundle["places"])
    bundle["prior_auth"] = build_prior_auth()
    bundle["marketplace"] = build_marketplace()
    marketplace_lines = marketplace_report(bundle["marketplace"])
    feb = bundle["marketplace"]["feb"]
    bundle["coverage_mix"] = load_coverage_mix()
    bundle["population"] = load_population([r["y"] for r in feb], raw_dir=RAW)
    bundle["coverage_counts"] = load_coverage_counts(f"{feb[-1]['y']}-02")
    marketplace_lines += coverage_report(bundle)
    bundle["sources"] = SOURCES
    bundle["built"] = datetime.date.today().isoformat()
    args.out.write_text(json.dumps(bundle, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n")
    print("wrote", args.out)

    for t in bundle["trend"]:
        print(t["year"], f"appeals {t['appeals']:,.0f}", f"won {t['overturned'] / t['appeals']:.2%}", f"states {t['states']}")
        for net in ("in", "out", "all"):
            if t[f"{net}_claims"] is not None:
                print(" ", net, f"{t[f'{net}_denied']:,.0f} of {t[f'{net}_claims']:,.0f}",
                      f"= {t[f'{net}_rate']:.2%}", f"({t[f'{net}_issuers']} insurers)")
        for x in t["left_out"]:
            print("  left out of out and all:", x["s"], x["id"], x["n"], f"{x['denied']:,.0f} denied of {x['claims']:,.0f}")
    print("states with no all-claims rate", {k: v["all"]["left_out"] for k, v in bundle["states"].items() if v["all"]["why"]})
    for k, e in bundle["earlier"].items():
        print("earlier", k, e["year"], f"(plan year {e['py']})", f"in {e['denied']:,.0f} of {e['claims']:,.0f} = {e['rate']:.2%}",
              f"({e['issuers']} insurers)", "lowest to highest", e["span"])
    for k, e in bundle["state_reports"].items():
        print("state report", k, e["year"], f"in {e['denied']:,} of {e['claims']:,} = {e['rate']:.2%}",
              f"({e['insurers']} insurers, {e['publisher']})")
    for k, g in bundle["grey_issuers"].items():
        print("grey insurers", k, g["year"], g["publisher"], f"{len(g['rows'])} listed",
              [(i["n"], i["r"], i["a"], i["o"]) for i in g["rows"]], "left out", [(x["n"], x["why"]) for x in g["left_out"]])
    om = bundle["oon_meta"]
    print(f"out-of-network by {PLAN_TYPES_PY} plan types: network-only", om["net_only"], "other", om["other"], "unknown", om["unknown_plan_types"])
    print("closed-network reason", f"{om['closed_network']['denied']:,.0f}", f"= {om['closed_network']['share']:.1%} of out-of-network denials")
    whys = [i["out"]["why"] for i in bundle["issuers"]]
    print("insurer rows with an out-of-network rate", whys.count(None),
          "without", {w: whys.count(w) for w in ("net_only", "few", "error", "types_unknown", "missing")})
    rm = bundle["reason_meta"]
    print("reason entries", f"{rm['entries']:,.0f}", "plan-level in-network denials", f"{rm['plan_denied_in']:,.0f}")
    print("imr", bundle["imr_meta"], [r for r in bundle["imr_trend"] if r["y"] in (2017, 2025)])
    print("issuers", len(bundle["issuers"]), "corr", bundle["corr"])
    for line in ("ma", "mcd", "mkt", "ffs"):
        std = [r["std"] for r in bundle["prior_auth"] if r["line"] == line and r["std"] is not None]
        print("prior auth", line, len(std), f"{min(std):.2%} to {max(std):.2%}")
    print("\n".join(marketplace_lines))


if __name__ == "__main__":
    main()
