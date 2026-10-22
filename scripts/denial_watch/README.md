# Denial Watch data

`/denial-watch` shows only public data. Its numbers live in
`fighthealthinsurance/static/denial_watch.json`, which `build_bundle.py` makes
from the files below. The site never reads the database for this page, and
nothing here runs in the app.

## Refresh

1. Download the sources into a directory (the default is `scripts/denial_watch/raw/`,
   which git ignores):

   | File | Source |
   |---|---|
   | `tic2022.xlsx` … `tic2025.xlsx` | `https://data.healthcare.gov/datafile/py<YEAR>/transparency_in_coverage_PUF.xlsx` |
   | `tic2026.xlsx` | `Transparency_in_Coverage_PUF.xlsx` inside `https://download.cms.gov/marketplace-puf/2026/transparency-in-coverage-puf.zip` |
   | `imr.csv` | CSV on the [DMHC IMR dataset page](https://data.chhs.ca.gov/dataset/independent-medical-review-imr-determinations-trend) |
   | `partd.csv` | "Medicare Part D Spending by Drug" CSV from [data.cms.gov](https://data.cms.gov/summary-statistics-on-use-and-payments/medicare-medicaid-spending-by-drug/medicare-part-d-spending-by-drug) |
   | `sahie_2023.csv`, `sahie_2024.csv` | `https://www2.census.gov/programs-surveys/sahie/datasets/time-series/estimates-acs/sahie-<YEAR>-csv.zip` (the Census API now needs a key; the zips don't) |
   | `places_county.csv` | `https://data.cdc.gov/api/v3/views/i46a-9kgh/export.csv` (the old `rows.csv?accessType=DOWNLOAD` link was retired in October 2026) |
   | `effectuated_monthly.csv` | The CSV on [data.cms.gov's Health Insurance Exchanges Monthly Effectuated Enrollment page](https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/health-insurance-marketplace/health-insurance-exchanges-monthly-effectuated-enrollment), renamed (the current build used `Health Insurance Exchanges Monthly Effectuated Enrollment Data_asof_20260505.csv`) |

2. Build:

   ```sh
   pip install pandas openpyxl
   python scripts/denial_watch/build_bundle.py --raw-dir scripts/denial_watch/raw
   ```

3. Compare the printed figures with the page's copy. The headline sentences in
   `templates/denial_watch.html` name some numbers, such as the about 7% to 27% state
   range, 72% in California, the correlations, the trend's in-network shares, and
   the out-of-network and all-claims totals in Sources and method; the tests check them. When a new year arrives, adding
   it means extending `CLAIMS_YEAR` and `LATEST_PY`, then rereading the copy.

## Rules the build follows

- CMS suppresses small counts (`*`, `**`), and those cells are left out, never
  treated as zero. Appeal win rates use only insurers that reported both appeals
  filed and appeals won.
- Denial reasons are plan-level, in-network, and not exclusive (one claim can
  count under several), so the page shows them as shares of reasons, not of
  claims. CMS's out-of-network reason counts closed-network denials, so it is
  left out, as KFF leaves it out.
- The California IMR file has some exact duplicate rows, which are dropped.
- `medicare_prices.csv` tags each shown drug that has a Medicare-negotiated
  price, one row per brand with the CMS page it was checked against. The build
  doesn't look drugs up, so after a refresh check any newly shown drug against
  CMS's selected-drug lists. Update the file when CMS publishes the 2028 prices
  (due by November 30, 2026) and picks the next drugs (by February 1, 2027).
- Marketplace coverage is the February count of people whose marketplace plan
  was in effect, nationally (`State` Total, `Platform` All), every year from
  one CMS file. CMS's earlier reports counted the same month sooner and give
  higher numbers (its early 2025 report had 23.4 million for February 2025,
  the final file 21.8 million), so never mix in a figure from another report.
- data.cms.gov keeps only its latest effectuated file, so `EFFECTUATED_SHA256`
  records the one the page was written from and the build stops on any other.
  For a new file, update `EFFECTUATED_FILE`, `EFFECTUATED_AS_OF` and the
  sha256, then reread the marketplace copy: its heading says how the latest
  year compares with every other.
- CMS updates a year's months until that year's December is in, so the year
  without a December is marked not final (the page says preliminary). The
  build stops if any earlier year has no December, if a year is missing, or if
  the state rows don't add up to the national total.
- Only the national total is charted: the HealthCare.gov and state-run splits
  move when a state switches platform (Georgia and Illinois have since moved
  to their own marketplaces).
- CMS's methodology says Minnesota's latest data leave out a subgroup of
  enrollments after a reporting change, so each February row keeps
  Minnesota's count (`mn`) and the copy gives the drop without it too.
- CMS revised its 2021 counts for a new method (its Early 2022 effectuated
  enrollment report, footnote 1). No source says whether the file's 2016 to
  2020 counts were redone the same way.
- `marketplace_signups.csv` holds plans picked in each open enrollment (`Cnsmr`
  in the Total/All row of that year's OEP State-Level Public Use File,
  HealthCare.gov and state-run marketplaces together), with the CMS page, the
  zip, its sha256 and the date it was read; the build stops on a row missing
  any of them. The page compares each year's February coverage with it.
- KFF's figures are quoted and linked, never redrawn: its content is
  CC BY-NC-ND 4.0, and it asks for links to its pages, not its PDFs.
- The open-enrollment box's dates and plan year come from `views.py`
  (`DENIAL_WATCH_MEDICARE_ENROLLMENT`, `DENIAL_WATCH_ENROLLMENT_BOX_ENDS`); the
  box hides itself after the season. Each fall, also recheck its Sources line,
  which cites that year's CMS announcement.
- `medicare_prices.csv` must say `yes` or `no` for `price_published`, and the
  build stops on anything else or on a `price_until` before `price_from`.
- Insurer rows need at least 50,000 in-network claims. An insurer's appeal win
  rate needs at least 30 appeals, and a state's needs 50. When CMS withholds an
  insurer's number won, its row says "not reported" rather than "too few".
- The files for 2020 and 2021 claims have one claims count, which CMS's PY2023
  data dictionary defines as in-network only ("Does not include out-of-network
  claims"). So the trend's in-network line runs from 2020, and its
  out-of-network and all-claims lines start with 2022 claims.
- An insurer that reports more out-of-network denials than claims gets no
  out-of-network or all-claims figure, and is left out of those totals
  nationally and for its state; its state gets no all-claims figure.
  `KNOWN_DENIED_OVER_CLAIMS` lists the expected cases by plan year, and the
  build stops if a refresh finds a different set, so a person rechecks them.
  It also stops on more in-network denials than claims, and on denials reported
  against 0 claims on either side, which have no rule yet.
- "Network-only" means every plan the insurer sold in the latest claims year
  (the PY2024 plan list for 2024 claims) was an HMO or EPO. Those plans mostly
  don't cover out-of-network care by design, so those insurers get no
  out-of-network rate, and neither does an insurer missing from that plan list.
  The others need at least 1,000 out-of-network claims for a rate.
- The page shows none of those insurer rates: the insurer table counts
  in-network claims only. Many insurers that also sold PPO or POS plans had
  most of their out-of-network claims on HMO or EPO plans, or gave CMS's
  closed-network reason for most of those denials, so an insurer's
  out-of-network rate, or an all-claims rate that folds it in, would mostly
  show plan type. The bundle keeps them so a narrower view can come back
  without a builder change.
- Out-of-network rates aren't mapped by state either: most states' figures
  would mostly show which kinds of plans their insurers sell. There's no
  out-of-network change from 2023 (it mostly reflects which insurers reported)
  and no out-of-network appeals figure (appeals aren't split by network).
- `oon_meta.closed_network` sums CMS's plan-level closed-network reason
  (`Plan_Number_Claims_Denied_Due_To_Out_Of_Network`) over the insurers in the
  national out-of-network total. A claim can count under more than one reason,
  so the page gives only a rough share ("about half"). The PY2022 and PY2023
  files put this reason under `Plan_Number_Claims_Denied_Out_of_Network`; the
  build reads only the newer name.
