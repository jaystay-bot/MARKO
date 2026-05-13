# NEXT_N.md

Recommended next atomic task:

N-MARKO-BOOKERMOVE-WEEKLY-LEADS-EXPORT

## Goal

Generate a static weekly BookerMove leads JSON export from existing MARKO lead data using `docs/bookermove-lead-bridge-contract.md`.

## Gate Before Start

- Keep `leads.json` and `marko_log.json` clean.
- Keep `python smoke_test.py` green.
- Do not push or deploy until Jay explicitly requests sync/deploy.

## Scope

- Add a read-only exporter that maps current MARKO lead records to the BookerMove contract.
- Write `exports/bookermove-leads-va.json`.
- Include only Virginia moving-company leads.
- Include `priority`, `score`, `leakage_signals`, `recommended_action`, `outreach_angle`, and compliance notes.
- Add a JSON shape sanity test.
- Add Playwright proof that MARKO still loads after export generation.

## Non-Goals

- No BookerMove repo edits.
- No scraper expansion.
- No database.
- No paid APIs.
- No email or SMS automation.
- No deploy.
