# N2 — Census BPS + USPS/HUD Vacancy Aggregate Probe Spec

**Mode:** research / probe spec only. No code. No automated downloads.
Sister doc to N1 — covers the two federal aggregate sources that fill
the gaps left by the three county portals (Petersburg, Colonial Heights,
Hopewell, Ashland, plus background trends across the core three).

**Why federal aggregates:** county portals are deep but uneven. Federal
sources are shallow but uniform — they cover EVERY US county/ZIP at
publishable cadence with zero per-jurisdiction integration cost. The two
sources together give MARKO V1 ~95% Richmond-MSA coverage even if every
N1 county portal goes dark.

---

## 1. Source 1 — Census Building Permits Survey (BPS)

**What it is:** monthly Census-published count of residential building
permits (units permitted) by county, MSA, state, and US total. Published
since 1959. The "ground truth" for new-residential-construction trends.

**Probe URLs (verify in browser):**

- `https://www.census.gov/construction/bps/` — landing page
- `https://www2.census.gov/econ/bps/County/` — direct index of monthly
  county-level data files (typically `coXXyyyy.txt` format,
  XX = state, yyyy = year-month)
- `https://www.census.gov/construction/bps/msamonthly.html` — MSA-level
  view (Richmond-Petersburg MSA = code 40060)
- `https://api.census.gov/data/timeseries/eits/resconst` — JSON API for
  national/regional monthly time-series (for context, not zone-level)

**Fields available (county file format):**

| Field | Type | Notes |
|---|---|---|
| State FIPS | int | VA = 51 |
| County FIPS | int | Richmond city = 760, Henrico = 087, Chesterfield = 041, Hanover = 085, Petersburg = 730, Colonial Heights = 570, Hopewell = 670 |
| Year, Month | int | reporting period |
| 1-unit permits | int | single-family dwellings |
| 1-unit valuation | int | $ |
| 2-unit permits | int | duplexes |
| 3-4 unit permits | int | small multifamily |
| 5+ unit permits | int | large multifamily |
| Total units permitted | int | sum |

**Geography level:** county. Cannot drill below county. For
sub-county geography use county portals (N1) or USPS/HUD vacancy
(below).

**Refresh cadence:** monthly, ~45-day lag (e.g. data for March
publishes in mid-May).

**Access method:** plain-text fixed-width or CSV files at the index URL.
One HTTP GET per county per month. No auth, no rate limit issues at
this volume.

**Legal/safety:** federal public dataset. Zero risk.

**Richmond MSA coverage:**

| Jurisdiction | County FIPS | BPS coverage |
|---|---|---|
| Richmond city | 760 | yes (independent city = own county-equivalent) |
| Henrico | 087 | yes |
| Chesterfield | 041 | yes |
| Hanover (Ashland, Mechanicsville fringe) | 085 | yes |
| Petersburg | 730 | yes |
| Colonial Heights | 570 | yes |
| Hopewell | 670 | yes |
| Goochland | 075 | yes (covers Short Pump fringe west) |
| Powhatan | 145 | yes |
| Charles City | 036 | yes |
| New Kent | 127 | yes |

**100% coverage of locked target service area.** Nothing in the
Richmond MSA is outside BPS reach.

**Signal interpretation:** BPS is a `construction_surge` signal —
meaning new dwelling supply, not new occupancy. It leads `moved_in`
events by 6-18 months for single-family, 12-24 months for multifamily.
For V1, treat a county with permits >120% of its trailing 12-month
mean as a `nearby_homeowner` zone (per the V1 mapping plan §5).

**V1 yes/no:** **YES** — primary fallback when N1 county portals are
sparse. Also primary signal for the four gap jurisdictions
(Petersburg, Colonial Heights, Hopewell, Ashland-via-Hanover).

---

## 2. Source 2 — USPS / HUD Aggregated Vacancy Data

**What it is:** quarterly count of USPS-reported residential and
business address vacancies, redistributed by HUD at the Census tract
AND ZIP level. Generated from USPS NCOA data and stripped of any
individual-address PII before publication. The cleanest public
"vacancy now" signal in the US.

**Probe URLs (verify in browser):**

- `https://www.huduser.gov/portal/datasets/usps.html` — landing page
  describing the dataset, methodology, and access registration
- `https://www.huduser.gov/portal/usps/index.html` — current-quarter
  data download portal (registration required, free for research/non-
  commercial use)
- `https://www.huduser.gov/portal/datasets/usps/USPSVacancyData.html` —
  documentation of fields and methodology

**Fields available (ZIP-level file):**

| Field | Type | Notes |
|---|---|---|
| ZIP | string | 5-digit |
| Reporting quarter | string | YYYYQX |
| Total residential addresses | int | |
| Total residential vacant | int | |
| Vacancy % | float | derived |
| Avg vacancy duration (quarters) | float | "long-term vacant" if >4 |
| Total business addresses | int | (not used for V1 mover signal) |
| No-stat addresses | int | likely uninhabitable |

**Geography level:** ZIP code (true ZIP-level, not ZCTA). Also
available at Census tract. **Best public ZIP-level signal we have.**

**Refresh cadence:** quarterly, ~30-60 day lag from quarter end.

**Access method:** ZIP/CSV download per quarter. Free registration
account (one-time setup; not a paid API). One GET per quarter per
geography file.

**Legal/safety:** HUD-published, USPS-sourced, individual addresses
already stripped at publication. Zero PII risk.

**Richmond MSA ZIP coverage:**

All ZIPs in the locked target service area are present:

| City | Representative ZIPs |
|---|---|
| Richmond | 23219, 23220, 23221, 23222, 23223, 23224, 23225, 23226, 23227, 23228, 23230, 23231, 23234, 23235, 23236, 23294 |
| Henrico (incl. Short Pump, Glen Allen, Mechanicsville-fringe) | 23059, 23060, 23069, 23075, 23111, 23116, 23150, 23223, 23227, 23228, 23229, 23230, 23231, 23233, 23238, 23294 |
| Chesterfield (incl. Midlothian) | 23112, 23113, 23114, 23120, 23139, 23234, 23235, 23236, 23237, 23832, 23838 |
| Colonial Heights | 23834 |
| Petersburg | 23803, 23805 |
| Hopewell | 23860 |
| Ashland | 23005 |

**Signal interpretation:** vacancy delta vs trailing 4-quarter mean is
a `moved_out_trend` signal at ZIP granularity. A ZIP whose vacancy
spikes by >2 percentage points quarter-over-quarter is a strong
move-activity zone candidate. Sustained high vacancy with rising
duration is `turnover` — neutral move signal but useful context.

**V1 yes/no:** **YES** — only true ZIP-level vacancy signal in V1
without scraping. Pairs naturally with BPS county-level construction
to form composite zones.

---

## 3. Source 3 — ACS B25004 Vacancy Status (background trend)

**What it is:** American Community Survey 5-year estimates of vacant
housing units by reason (for-rent, for-sale, seasonal, etc.) at tract
and ZCTA level. Slow-moving, multi-year smoothed.

**Probe URLs (verify in browser):**

- `https://www.census.gov/programs-surveys/acs.html` — landing page
- `https://api.census.gov/data/2022/acs/acs5?get=group(B25004)&for=zip%20code%20tabulation%20area:23220&in=state:51`
  — example direct API call returning B25004 data for ZCTA 23220
- `https://api.census.gov/data/2022/acs/acs5/variables/group/B25004.json`
  — variable list for the table

**Fields available:**

| Variable | Description |
|---|---|
| B25004_001E | Total vacant housing units |
| B25004_002E | For rent |
| B25004_003E | Rented, not occupied |
| B25004_004E | For sale only |
| B25004_005E | Sold, not occupied |
| B25004_006E | For seasonal/recreational/occasional use |
| B25004_007E | For migrant workers |
| B25004_008E | Other vacant |

**Geography level:** Census tract, ZCTA (≈ ZIP), county.

**Refresh cadence:** annual (5-year rolling estimates published each
December, lagging by ~12 months).

**Access method:** Census Data API, JSON, no key required for low
volume. One GET per ZIP per year.

**Legal/safety:** zero risk.

**V1 yes/no:** **YES, but background only.** Used to compute the
"baseline" vacancy that USPS/HUD quarterly numbers are deltas against.
Provides the `confidence` boost when USPS/HUD spikes corroborate against
ACS-trend direction. Not a primary signal on its own.

---

## 4. Composite coverage map (federal sources only)

| Locked target city | BPS county-level | USPS/HUD ZIP-level | ACS B25004 ZCTA |
|---|---|---|---|
| Richmond | yes | yes | yes |
| Henrico | yes | yes | yes |
| Chesterfield | yes | yes | yes |
| Midlothian | (Chesterfield county) | yes (23112-23114) | yes |
| Short Pump / Glen Allen | (Henrico county) | yes (23059, 23060, 23233) | yes |
| Mechanicsville | (Henrico/Hanover) | yes (23111, 23116) | yes |
| Colonial Heights | yes | yes (23834) | yes |
| Petersburg | yes | yes (23803, 23805) | yes |
| Hopewell | yes | yes (23860) | yes |
| Ashland | (Hanover county) | yes (23005) | yes |

**100% federal-aggregate coverage of every locked target jurisdiction.**
This means even if all three N1 county portals fail manual probe,
MARKO V1 can still produce real Richmond-area zones — just at coarser
geo grain (ZIP and county instead of street-clustered).

---

## 5. Signal pairing rules (used in N3 model + N4 scoring)

| BPS county trend | USPS/HUD ZIP trend | Implied zone signal_type | Confidence boost |
|---|---|---|---|
| Permits ↑ >120% baseline | Vacancy ↑ in matching ZIP | `construction_surge` + `moved_in_trend` (composite) | high |
| Permits ↑ | Vacancy flat | `construction_surge` only | medium |
| Permits flat | Vacancy ↑ >2pp QoQ | `moved_out_trend` | medium |
| Permits flat | Vacancy flat | no zone published (below floor) | n/a |
| Calendar = PCS season | any | `seasonal_window` overlay (+5 mover relevance) | n/a |
| Calendar = college move-in/out | any | `seasonal_window` overlay (+5 mover relevance) | n/a |

These rules belong in N4 (scoring function), not N2 — listed here so
the source plan and the scoring plan trace cleanly to each other.

---

## 6. Risks and gaps

- **BPS reports permits, not occupancy.** Lag from permit-to-CO is
  weeks (small alterations) to 12+ months (new subdivisions). Score
  with `freshness_days` honestly — a 30-day-old permit has high
  freshness for a `construction_surge` signal, low freshness for a
  `moved_in_trend` claim.
- **USPS/HUD requires free registration.** Not a paid API but does
  add a one-time manual setup step Jay needs to complete. Zero
  ongoing friction once the credentials are issued.
- **ACS B25004 lags 12+ months.** Use only as the baseline for delta
  calculations, never as a current-state signal.
- **No address-level data anywhere in this spec.** All three federal
  sources are pre-aggregated. There is no path from these to
  individual homeowner targeting — by design.

---

## 7. Manual verification checklist (for Jay before N3 ingest)

For each source in §1 / §2 / §3, confirm in a browser:

- [ ] Landing URL loads and dataset is still published
- [ ] At least one Richmond-MSA county/ZIP file is downloadable
- [ ] Most recent observation date is within the published cadence
      (BPS ≤ 60 days lag, USPS/HUD ≤ 90 days lag)
- [ ] No paid tier required for the data we listed
- [ ] HUD USPS account registered (one-time, free)

Ship-blockers: any source going dark, paywall, or > 6 months stale.
Workaround: the other two sources are independent — losing one
degrades zone confidence but does not kill V1.

---

## 8. What this spec does NOT do

- Does not call any URL programmatically
- Does not download any dataset
- Does not store sample data
- Does not build any ingest code
- Does not change the locked BookerMove export contract
- Does not touch BookerMove
- Does not add automation, outreach, or paid APIs
- Does not enrich with any individual-level data

---

## 9. Recommended next N

`N3-MOVESIGNALZONE-MODEL-IMPLEMENTATION` — build the internal MARKO
MoveSignalZone model with validation + unit tests. No data ingest yet;
the N1 + N2 source plan defines what the model must eventually carry.
