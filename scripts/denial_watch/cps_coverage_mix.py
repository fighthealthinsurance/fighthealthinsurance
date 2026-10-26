"""Tally where people get coverage, each person counted once, from Census's CPS ASEC.

The source is the U.S. Census Bureau's Current Population Survey, Annual Social
and Economic Supplement (CPS ASEC), public-use file. For the 2026 survey,
https://www.census.gov/data/datasets/time-series/demo/cps/cps-asec.2026.html
links the CSV file:

    https://www2.census.gov/programs-surveys/cps/datasets/2026/march/asecpub26csv.zip
    sha256 fe819d0d2fc4470c282e76c247b8aa8b6054811619730307e9f2fe6186707d93
    (released September 15, 2026)

Unzip it and pass the person file (pppub26.csv) and the replicate weights
(asec_csv_repwgt_2026.csv). The same page has the data dictionary
(asec2026_ddl_pub_full.pdf), which defines the coverage variables used below.
Census releases a new survey each September; for it, change the file names,
the year in REFERENCE and the column names if the dictionary changed them.

Each person is counted once:

- anyone with Medicare goes under Medicare, whatever else they have;
- anyone else with exactly one kind of coverage goes under that kind;
- anyone else with two or more kinds goes under "more than one kind";
- everyone else is uninsured.

There are two measures. "now" is coverage at the interview (February to April
of the survey year; the page uses this one). "year" is coverage at any time in
the year before, where uninsured means all year, as in Census's headline figures.

Margins of error are at 90%, from the 160 replicate weights with Census's
formula for this survey: Var = 4/160 * sum over r of (X_r - X_0)^2. Checked
against Census's published Table 1 of report P60-291 and table H-02: the
totals and margins match to the rounding unit.

    python -I scripts/denial_watch/cps_coverage_mix.py pppub26.csv asec_csv_repwgt_2026.csv scripts/denial_watch/coverage_mix.csv

It reads about 550 MB and takes about 10 seconds. build_bundle.py reads the
CSV it writes.
"""

import csv
import math
import sys
from array import array

NREP = 160
AGES = ("all", "under65", "65plus")
# The page's order: private plans, public programs, more than one, none.
CATEGORIES = [
    ("job", "Job-based plan only"),
    ("marketplace", "Marketplace plan only"),
    ("other_individual", "Plan bought outside the marketplace only"),
    ("medicare", "Medicare, alone or with another kind"),
    ("medicaid_chip", "Medicaid or CHIP only"),
    ("military_va", "Military (TRICARE) only or VA only"),
    ("more_than_one", "More than one kind, no Medicare"),
    ("uninsured", "Uninsured"),
]
REFERENCE = {
    "now": "At interview, Feb-Apr 2026",
    "year": "Any time in calendar year 2025 (uninsured = all year)",
}
# Each kind of coverage and its column, at interview and for the year before. VA counts with CHAMPVA.
KINDS = {
    "employer": ("NOW_GRP", "GRP"),
    "marketplace": ("NOW_MRK", "MRK"),
    "other_individual": ("NOW_NONM", "NONM"),
    "tricare": ("NOW_MIL", "MIL"),
    "medicare": ("NOW_MCARE", "MCARE"),
    "medicaid_chip": ("NOW_MCAID", "MCAID"),
}
VA = (("NOW_VACARE", "NOW_CHAMPVA"), ("VACARE", "CHAMPVA"))
ONE_KIND = {"employer": "job", "tricare": "military_va", "va": "military_va"}


def category(p, now):
    """The one place a person is counted, or None outside the year measure's universe."""
    i = 0 if now else 1
    covered = p["NOW_COV" if now else "COV"]
    # The year measure leaves out babies born after the year (COV is 0 for them).
    if covered not in ("1", "2"):
        return None
    if covered == "2":
        return "uninsured"
    held = [k for k, cols in KINDS.items() if p[cols[i]] == "1"]
    if any(p[c] == "1" for c in VA[i]):
        held.append("va")
    if "medicare" in held:
        return "medicare"
    if len(held) == 1:
        return ONE_KIND.get(held[0], held[0])
    if not held:
        sys.exit(f"person {p['PH_SEQ']}/{p['PPPOS']} is covered but has no kind of coverage")
    return "more_than_one"


def replicate_weights(path):
    weights = {}
    with open(path, newline="") as f:
        rows = csv.reader(f)
        head = next(rows)
        cols = [head.index(f"pwwgt{i}") for i in range(NREP + 1)]
        seq, pos = head.index("h_seq"), head.index("PPPOS")
        for r in rows:
            key = (r[seq], r[pos])
            if key in weights:
                sys.exit(f"two replicate weight rows for {key}")
            weights[key] = array("d", (float(r[c]) for c in cols))
    return weights


def tally(person_path, weights):
    """Weighted counts by (measure, age, category), with "total" for each measure and age; index 0 is the full sample."""
    # People with the same cells are summed first, so each person's 161 weights are added once.
    groups = {}
    with open(person_path, newline="") as f:
        for p in csv.DictReader(f):
            w = weights.pop((p["PH_SEQ"], p["PPPOS"]))
            # The full-sample replicate weight is the person's survey weight, which MARSUPWT gives in hundredths.
            if abs(w[0] - int(p["MARSUPWT"]) / 100) > 0.01:
                sys.exit(f"person {p['PH_SEQ']}/{p['PPPOS']}: pwwgt0 isn't MARSUPWT")
            ages = ("all", "under65" if int(p["A_AGE"]) < 65 else "65plus")
            cells = []
            for measure, now in (("now", True), ("year", False)):
                cat = category(p, now)
                if cat is not None:
                    cells += [(measure, age, key) for age in ages for key in (cat, "total")]
            acc = groups.setdefault(tuple(cells), [0.0] * (NREP + 1))
            for i in range(NREP + 1):
                acc[i] += w[i]
    if weights:
        sys.exit(f"{len(weights)} replicate weight rows have no person")
    sums = {}
    for cells, acc in groups.items():
        for cell in cells:
            total = sums.setdefault(cell, [0.0] * (NREP + 1))
            for i in range(NREP + 1):
                total[i] += acc[i]
    return sums


def moe90(values):
    x0 = values[0]
    return 1.645 * math.sqrt(4.0 / NREP * sum((x - x0) ** 2 for x in values[1:]))


def rows(sums):
    out = []
    for measure in ("now", "year"):
        for age in AGES:
            total = sums[(measure, age, "total")]
            for order, (key, label) in enumerate(CATEGORIES, 1):
                n = sums.get((measure, age, key))
                if n is None:
                    continue
                share = [100 * a / b for a, b in zip(n, total)]
                out.append(dict(
                    measure=measure, reference_period=REFERENCE[measure], age=age, order=order, key=key, category=label,
                    people=round(n[0]), people_moe90=round(moe90(n)), percent=round(share[0], 2), percent_moe90=round(moe90(share), 2),
                ))
    return out


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    person_path, weights_path, out_path = argv[1:]
    table = rows(tally(person_path, replicate_weights(weights_path)))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    for r in table:
        print(r["measure"], r["age"], f"{r['category']:42}", f"{r['people']:>12,}", f"{r['percent']:6.2f}%", f"MOE {r['percent_moe90']:.2f}")


if __name__ == "__main__":
    main(sys.argv)
