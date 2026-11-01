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
   | `tic2026.xlsx`, `tic2027.xlsx` | The xlsx inside `https://download.cms.gov/marketplace-puf/<YEAR>/transparency-in-coverage-puf.zip` |
   | `imr.csv` | CSV on the [DMHC IMR dataset page](https://data.chhs.ca.gov/dataset/independent-medical-review-imr-determinations-trend) |
   | `partd.csv` | "Medicare Part D Spending by Drug" CSV from [data.cms.gov](https://data.cms.gov/summary-statistics-on-use-and-payments/medicare-medicaid-spending-by-drug/medicare-part-d-spending-by-drug) |
   | `sahie_2023.csv`, `sahie_2024.csv` | `https://www2.census.gov/programs-surveys/sahie/datasets/time-series/estimates-acs/sahie-<YEAR>-csv.zip` (the Census API now needs a key; the zips don't) |
   | `places_county.csv` | `https://data.cdc.gov/api/v3/views/i46a-9kgh/export.csv` (the old `rows.csv?accessType=DOWNLOAD` link was retired in October 2026) |
   | `effectuated_monthly.csv` | The CSV on [data.cms.gov's Health Insurance Exchanges Monthly Effectuated Enrollment page](https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/health-insurance-marketplace/health-insurance-exchanges-monthly-effectuated-enrollment), renamed (the current build used `Health Insurance Exchanges Monthly Effectuated Enrollment Data_asof_20260505.csv`) |
   | `<YEAR>-oep-state-level-public-use-file.zip` (optional) | The `file` link in each row of `marketplace_signups.csv`, not renamed; when it's here the build checks it against that row's sha256 |
   | None (`open_enrollment_history.csv` is checked in) | Each row's `source_url`: CMS's OEP state-level public use file zips for 2017 to 2026 (all listed on [CMS's marketplace products page](https://www.cms.gov/data-research/statistics-trends-and-reports/marketplace-products)) and HHS ASPE's enrollment reports for 2014 to 2016. The build doesn't read them; reopen one to check a row |
   | `nc-est2020int-agesex-res.csv`, `nc-est2025-agesex-res.csv` (optional) | The `source_url` in `population_under_65.csv`, not renamed; when they're here the build checks each against its sha256 and works the counts out again |
   | None (`coverage_mix.csv` is checked in) | `asecpub26csv.zip` from [Census's 2026 CPS ASEC page](https://www.census.gov/data/datasets/time-series/demo/cps/cps-asec.2026.html); `cps_coverage_mix.py` tallies it into the CSV (see below) |
   | None (`coverage_counts.csv` is checked in) | Each row's `source_url`: the Medicare Monthly Enrollment CSV on [data.cms.gov](https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/medicare-and-medicaid-reports/medicare-monthly-enrollment) and the Medicaid and CHIP enrollment release on [data.medicaid.gov](https://data.medicaid.gov/dataset/6165f45b-ca93-5bb5-9d06-db29c692a360); `how` says what was summed |

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

## Marketplace enrollment in context

The open enrollment page and Denial Watch's coverage section share a donut of
where people get coverage (`templates/partials/coverage_mix.html`, with its
sources and method in `templates/partials/coverage_mix_method.html`), and the
open enrollment page sets each February's marketplace coverage beside the
people under 65. Three checked-in files feed them:

| File | What it holds | Source | When to refresh |
|---|---|---|---|
| `coverage_mix.csv` | Everyone, each person counted once, at the interview (`now`, February to April) and over the year before (`year`), for all ages, under 65 and 65 and over. The page uses `now`, all ages | FHI's tally of Census's Current Population Survey, Annual Social and Economic Supplement (CPS ASEC) public-use file, by `cps_coverage_mix.py` | Each September, when Census releases the new CPS ASEC |
| `population_under_65.csv` | People under 65 on July 1, 2015 to 2025, with the total, the series, the file and its sha256 | Census's national population estimates by single year of age: the 2010-2020 intercensal file to 2019, Vintage 2025 from 2020 | Each April, when Census publishes the new vintage by age (Vintage 2026 is due April 2027); its December release has totals only |
| `coverage_counts.csv` | Medicare and Medicaid and CHIP for the marketplace's latest February | CMS's Medicare Monthly Enrollment file and its Medicaid and CHIP enrollment release | Monthly; update with the marketplace's February, since the build stops on another month |

To retally the survey, download the zip, unzip `pppub26.csv` and
`asec_csv_repwgt_2026.csv` somewhere outside the repository, then:

```sh
python -I scripts/denial_watch/cps_coverage_mix.py pppub26.csv asec_csv_repwgt_2026.csv scripts/denial_watch/coverage_mix.csv
```

It takes about 10 seconds. For a new year, update the file names in its
docstring, `REFERENCE`, and `COVERAGE_MIX_SURVEY` in `build_bundle.py`, and
check the totals against Census's published Table 1 of its health insurance
report and table H-02 before reading the page's copy again.

The copy in `coverage_mix.html`, `coverage_mix_method.html` and the open
enrollment page's population chart names some of these numbers (nearly half
job-based only, about 4 in 100 marketplace, the records counts, more than
doubled against about 1%, about 7 and 4 for every 100 under 65);
`tests/sync/test_coverage_mix.py` checks them.

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
  any of them. The page compares each year's February coverage with it, so
  the build also stops on a count that isn't above 0, two rows for one year,
  a file with no rows, a year with no February count, or a missing row for
  either of the last two Februaries.
- `open_enrollment_history.csv` holds plans picked in every open enrollment
  from 2014, with the HealthCare.gov and state-run parts, for the open
  enrollment page's long line. 2017 on is the Total/All row of each year's CMS
  OEP State-Level Public Use File. CMS has no all-marketplace file before
  2017, so 2014 to 2016 come from HHS ASPE's enrollment reports, whose tables
  cite CMS; `publisher` says which (`CMS` or `HHS ASPE, from CMS data`). The
  build puts it under the marketplace entry as `history` and stops on a gap
  in the years from 2014, a count that isn't above 0, a total that isn't
  HealthCare.gov plus state-run, another publisher, a `source_url` that isn't
  https on www.cms.gov or aspe.hhs.gov, a missing `source_file`, or a year
  whose count differs from `marketplace_signups.csv`. The windows and CMS's
  definition of a plan pick changed over the years (2018 and 2022), which the
  page's note and Sources and method say, so reread them when a year is
  added. The HealthCare.gov and state-run parts shift when a state changes
  platform, so only the total is charted.
- `file_sha256` is a record of the zip the count was read from, taken when it
  was downloaded. The zips stay out of git: put them in the raw directory
  under their own names (`2026-oep-state-level-public-use-file.zip`) and the
  build checks each against its sha256 and stops on a mismatch. A zip that
  isn't there is reported as not checked.
- The coverage donut counts each person once: anyone with Medicare under
  Medicare, anyone else with exactly one kind under that kind, two or more
  other kinds under "more than one kind". The build stops unless the
  at-interview, all-ages rows are the eight slices in the page's order and
  their shares add to 100 within rounding. The donut is a survey estimate and
  is never drawn with CMS's counts, which come from records.
- The population is never mixed across vintages: Vintage 2025 redid 2020 on,
  and pairing the intercensal file with Vintage 2024 would fake a jump of
  about 1.7 million at 2020. The build stops on a row from another series or
  file, a gap in the years, or a February with no July before it. A new
  vintage replaces every year from 2020 (update `POPULATION_SERIES`), and the
  page's note on where the two series join needs rereading.
- `coverage_counts.csv` has one row each for Medicare (the 50 states and DC
  added up, so it matches the marketplace's and Medicaid's geography) and
  Medicaid and CHIP (the updated count), for the month the marketplace's
  latest February is. The build stops on another month, a missing row or a
  row without its https source, file, sha256 and what was summed.
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
- `earlier` holds each state the latest map leaves grey that an older file
  still includes, from the last year it's in, by the map's rules: the
  in-network rate and insurer count from `state_rates`, and the lowest to
  highest rate among insurers the table would list. Only a grey state's
  readout and its insurer table (`grey_issuers`, below) show it, never the
  map, the national figures or the ranges. The page's state-map note names
  those states and years, so reread it when the set changes.
- `state_reports.csv` holds grey states whose own insurers' reports on CMS's
  Transparency in Coverage form give an in-network rate for the map's year or
  the year before
  (California from Covered California's postings, Pennsylvania from its
  Insurance Department's report), one checked row per state with the source,
  the date it was read and a note on what was summed or left out. The build
  puts them under `state_reports` and stops on a state the map or `earlier`
  already covers, a claims year further back, more denials than claims, or a row
  missing its publisher, https source or date. Only the every-state chart, the
  grey state's readout and its insurer table show them. The readout lines for California and
  Pennsylvania in `denial_watch.js` are hand-written, and a test ties their
  numbers and links to the bundle, so reread them when a row changes. These
  can count plans sold outside the marketplace, which CMS's figures don't, so
  they never colour the map or join the national figures or ranges.
- `state_report_insurers.csv` holds each insurer behind a `state_reports.csv`
  row, with the file and page its counts are on, and the insurers a report
  leaves out, each with the short `left_out` reason the page shows under the
  table. The build stops unless the counted rows add up to the state's row in
  `state_reports.csv` (insurers, claims and denials), and on a state with no
  row there, another claims year, two rows for one insurer, more denials than
  claims, more appeals overturned than filed, or a counted row with no page.
  Covered California posts each insurer's plan rows, not its totals, so
  California's figures add up those rows, and it posts no appeal counts, so
  its table has no appeals column.
- `grey_issuers` is the insurer table for grey states: each `earlier` state's
  insurers from that year's file, and each `state_report_insurers.csv` state's
  counted insurers, by the table's rules (50,000 in-network claims, and an
  appeal win rate only on 30 appeals). Each entry carries its claims year and
  source, which the table states above the rows, and never joins the map, its
  ranges or the national figures. `DOUBTFUL_WON` covers only the latest year:
  the earlier states' appeal counts were checked against its three patterns
  on October 26, 2026 and none matched, so recheck them when `earlier` changes.
- `DOUBTFUL_DENIED_IN` lists insurers whose in-network denials look like a
  reporting error (for PY2027, Capital Health Plan in Florida: 96% after 6.5%).
  Their in-network denials count as missing everywhere, and the page says so.
- The PY2027 file renamed many columns and split plan-level figures by network
  and behavioral health. `read_puf` and `issuer_cells` read both shapes. Its
  appeals columns separate claims from requests made before care; the build
  uses appeals of claims, and doesn't use the new pre-service counts yet.
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
