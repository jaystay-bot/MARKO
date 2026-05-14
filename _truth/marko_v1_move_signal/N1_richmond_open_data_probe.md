# N1 — Richmond / Henrico / Chesterfield Open-Data Probe Spec

**Mode:** research / probe spec only. No code. No automated downloads. No
crawler infrastructure. Manual probe (operator opens each URL in a browser,
captures a sample row screenshot) is the verification step before N3.

**Scope:** the three core jurisdictions covering most of Richmond MSA
mover demand:

1. City of Richmond (Richmond city, an independent city in VA)
2. Henrico County (wraps Richmond on three sides; includes Short Pump,
   Glen Allen, Mechanicsville fringe)
3. Chesterfield County (south of the James; includes Midlothian; abuts
   Colonial Heights)

Petersburg, Colonial Heights, Hopewell, and Ashland are deferred — see
§4. They are smaller jurisdictions with sparse open-data presence and
will be handled by the federal aggregate sources (N2) and operator
manual recon, not by their own portal probes.

---

## 1. What we want from each portal

For each jurisdiction, MARKO V1 wants ONE of these (in priority order):

1. **Residential building permits** — new dwelling, addition, alteration.
   Used as a `construction_surge` signal feeding the
   `MoveSignalZone.signal_type` enum (N3).
2. **Demolition permits** — leading indicator of teardown/rebuild churn.
3. **Certificate of Occupancy issuances** — strong `moved_in_trend`
   signal at the address-rolled-up-to-neighborhood level.
4. **Code enforcement / property maintenance cases** — weak vacancy
   signal; only useful as a corroborating layer.
5. **Development plan submissions** — slow-burn supply-side signal;
   informational only for V1.

We DO NOT want:

- Individual property owner names or contacts
- Sale price or transaction-party data
- Anything requiring login, paid API key, or scraping past pagination
- Anything that requires more than one HTTP GET per dataset to download
  (bulk crawler infrastructure is forbidden in V1)

---

## 2. Per-portal probe spec

For each portal Jay should manually verify in a browser:

- Does the portal still exist at the URL listed?
- Are the datasets listed below still published?
- Is there a direct CSV / JSON / GeoJSON download link, or
  viewer-only?
- What is the most recent observation date visible (freshness check)?
- Capture one sample row screenshot per dataset for the N6 fixture work.

URLs are listed as **starting points**. ArcGIS Hub and Socrata portals
move dataset slugs around; treat any 404 as a "search the parent portal"
cue, not a blocker.

---

### 2.1 City of Richmond

**Portal name:** Richmond Open Data Portal (city of Richmond, VA)

**Probe URLs (manual verification required):**

- `https://data.richmondgov.com` — primary Socrata-style portal entry
  point. Search terms: `permits`, `building`, `certificate of occupancy`,
  `code enforcement`.
- `https://www.rva.gov/permits-inspections` — official permitting
  department; may link to the data portal or to the Accela permitting
  viewer.
- `https://richmond-gis-rva.opendata.arcgis.com` — GIS Hub if Socrata
  portal lacks geometry layers.

**Datasets to confirm:**

| Candidate dataset | What it gives us | V1 use |
|---|---|---|
| Building permits issued | permit type, address, issue date, valuation | `construction_surge`, rolled up to ZIP |
| Certificate of Occupancy issuances | new occupancy date, address | `moved_in_trend` (strongest single signal) |
| Code enforcement cases | property maintenance flags, vacant property | weak `vacancy` corroborator |
| Demolition permits | demo address, date | turnover precursor |

**Refresh cadence (presumed; verify on portal):** weekly for permits;
city of Richmond historically lags county portals.

**Geo precision:** address-level. Roll up to ZIP / neighborhood for V1
output (no individual targeting).

**Access method (verify):** Socrata REST API gives CSV / JSON download
without auth. ArcGIS Hub gives GeoJSON / Shapefile. Either is single-
GET friendly; neither requires a crawler.

**Legal/safety:** city-published public data. No restrictions, no PII
beyond property addresses (which are public record). MARKO never
republishes addresses as individual leads — they are aggregated to
neighborhood in the output.

**V1 yes/no:** **YES**, contingent on Jay's manual confirmation that
permits or CO data is downloadable and not stale beyond ~60 days.

**Risks:** Richmond city has historically had patchy open-data coverage
relative to the surrounding counties. If permits lag > 90 days or are
viewer-only, fall back to Census BPS county-level signal (N2).

---

### 2.2 Henrico County

**Portal name:** Henrico County GIS / Open Data Hub

**Probe URLs (manual verification required):**

- `https://data-henrico.opendata.arcgis.com` — ArcGIS Hub entry point.
- `https://gis.henrico.us` — county GIS landing page; usually links to
  the open-data hub.
- `https://henrico.us/services/permits-inspections` — permitting
  department; may expose permit search and data download.

**Datasets to confirm:**

| Candidate dataset | What it gives us | V1 use |
|---|---|---|
| Building Permits (residential) | permit type, address, issue date | `construction_surge` |
| Building Permits Issued (point layer) | geocoded permit features | direct zone clustering |
| Parcels | parcel boundary + use code | geometry baseline (not a signal itself) |
| Subdivision Plats | new subdivision filings | leading `moved_in_trend` |
| Certificate of Occupancy | if available | best `moved_in_trend` signal |

**Refresh cadence (presumed; verify on portal):** weekly to monthly for
permits; ArcGIS layers sometimes update nightly when fed from county
GIS systems.

**Geo precision:** address + lat/lon (ArcGIS feature service includes
geometry). Excellent — supports direct distance calc for the radius
logic (designed in §4 of the V1 plan).

**Access method (verify):** ArcGIS Feature Service REST endpoint —
single query parameters can return GeoJSON. One GET per quarterly
refresh window. No crawler needed.

**Legal/safety:** county-published public data.

**V1 yes/no:** **YES** — Henrico is the strongest county candidate
because it covers Short Pump / Glen Allen / Mechanicsville-fringe ZIPs
and historically publishes geocoded permit features.

**Risks:** ArcGIS dataset slugs change. Bookmark the layer's persistent
ID once verified, not the human-friendly URL.

---

### 2.3 Chesterfield County

**Portal name:** Chesterfield County Open Data Hub (ArcGIS Hub)

**Probe URLs (manual verification required):**

- `https://chesterfield-county-data-hub-chesterfieldcountygis.hub.arcgis.com`
  — ArcGIS Hub entry point (long URL is normal for ArcGIS Hub-hosted
  county sites).
- `https://www.chesterfield.gov/2114/Open-GIS-Data` — county landing
  page that links to the hub.
- `https://www.chesterfield.gov/123/Building-Inspection` — permitting
  department.

**Datasets to confirm:**

| Candidate dataset | What it gives us | V1 use |
|---|---|---|
| Building Permits | type, address, issue date | `construction_surge` |
| New Residential Construction | dwelling-type permits only | cleanest `construction_surge` |
| Certificate of Occupancy | if available | `moved_in_trend` |
| Subdivisions / Plats | new subdivision filings | leading `moved_in_trend` |
| Demolitions | demo address + date | turnover precursor |

**Refresh cadence (presumed; verify on portal):** Chesterfield has
historically been the best of the three for refresh frequency
(weekly+).

**Geo precision:** address + parcel + lat/lon via ArcGIS feature
service. Same quality as Henrico.

**Access method (verify):** ArcGIS Feature Service. Single GET per
refresh.

**Legal/safety:** county-published public data.

**V1 yes/no:** **YES** — covers Midlothian corridor and
Colonial-Heights-adjacent ZIPs, which are core target markets.

**Risks:** same ArcGIS slug-stability caveat as Henrico.

---

## 3. Composite coverage assessment

If all three portals verify in §2:

| Target city in mover service area | Covered by | Coverage quality |
|---|---|---|
| Richmond | Richmond city | medium (historically patchy) |
| Henrico | Henrico County | strong |
| Short Pump / Glen Allen | Henrico County | strong |
| Mechanicsville (Hanover-side) | Henrico (partial); Hanover not yet probed | partial |
| Chesterfield | Chesterfield County | strong |
| Midlothian | Chesterfield County | strong |
| Colonial Heights | not covered by core 3 | gap → N2 federal aggregate |
| Petersburg | not covered by core 3 | gap → N2 federal aggregate |
| Hopewell | not covered by core 3 | gap → N2 federal aggregate |
| Ashland (Hanover-side) | not covered by core 3 | gap → N2 federal aggregate |

**Coverage verdict:** the three core portals plus N2 federal aggregates
(Census BPS at county level + USPS/HUD vacancy at ZIP level) cover the
locked target service area for V1 without needing per-jurisdiction
probes for the smaller cities.

---

## 4. Deferred jurisdictions and why

| Jurisdiction | Why deferred |
|---|---|
| Hanover County (Ashland, parts of Mechanicsville) | not probed in N1; will rely on N2 federal aggregates and add later if Jay sees real customer demand in this corridor |
| Petersburg | sparse open-data presence; covered by Census BPS at county granularity |
| Colonial Heights | very small jurisdiction; same reasoning as Petersburg |
| Hopewell | same reasoning |
| Ashland | town-level government; same reasoning |
| Norfolk / Hampton Roads | explicitly out of V1 per the locked Richmond-first directive |

These do NOT block V1. The federal aggregate sources in N2 cover them
at coarser geo grain (county or ZIP), which is sufficient for the
zone-level output the locked BookerMove contract carries.

---

## 5. Risks and gaps

- **URL drift.** ArcGIS Hub and Socrata both reorganize regularly.
  Spec value is in the dataset NAMES and FIELD shapes, not the
  exact URLs. Manual probe is the way to lock URLs.
- **Permit issue date vs. completion date.** Issue date leads
  occupancy by weeks-to-months. Use `moved_in_trend` only when CO
  data is available; otherwise the signal is `construction_surge`
  (a slower-burn `nearby_homeowner` analog).
- **Address rollup discipline.** All three portals expose addresses.
  MARKO V1 must aggregate to ZIP / neighborhood at ingest time and
  never store individual addresses in `MoveSignalZone` records.
  This is a hard rule for the N3 model.
- **Refresh cadence mismatches.** Richmond city may be 90+ days
  behind Henrico/Chesterfield. Per-source `freshness_days` is built
  into the scoring (§3 of the V1 plan) to handle this honestly.
- **Volume.** Even at the slowest refresh, three counties producing
  hundreds of permits per month each is well under any rate-limit
  concern for a single quarterly GET.

---

## 6. Verification checklist (manual, for Jay before approving N3)

For each portal in §2.1 / §2.2 / §2.3, confirm in a browser:

- [ ] Portal URL loads and is not deprecated
- [ ] At least one of {Building Permits, CO, Subdivisions} is published
- [ ] Dataset has a direct CSV / JSON / GeoJSON download link
      (not viewer-only)
- [ ] Most recent record date is within the past 90 days
- [ ] No login or API key required
- [ ] Capture one sample-row screenshot per confirmed dataset for the
      N6 fixture set

Ship-blockers: a portal failing all four of {permits, CO, subdivision,
demo} OR being viewer-only. In that case the jurisdiction collapses
to a federal-aggregate-only signal and we note the gap in N3.

---

## 7. What this spec does NOT do

- Does not call any URL programmatically
- Does not download any dataset
- Does not store sample data
- Does not build any ingest code
- Does not change the locked BookerMove export contract
- Does not touch BookerMove
- Does not add automation, outreach, or paid APIs

All actual ingest work waits for N3 (model) and N5 (mapping). N1 + N2
are the read-only paper trail that justifies the data sources MARKO
will eventually consume.

---

## 8. Recommended next N

`N2-CENSUS-BPS-AND-USPS-HUD-VACANCY-PROBE` — same shape as this doc
but for the two federal aggregate sources that fill in the gaps for
the smaller jurisdictions and provide the corroborating layer for the
core three.
