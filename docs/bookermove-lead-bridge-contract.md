# BookerMove Lead Bridge Contract

Status: contract-first, no automation in this N.

## Purpose

Define the smallest safe interface for moving qualified MARKO moving-company leads into BookerMove/TalkBot later without merging repositories, requiring MARKO runtime, adding a scraper, adding a database, or exposing internal acquisition mechanics to customers.

## Future Static Export Path

Preferred MARKO export path:

```text
exports/bookermove-leads-va.json
```

Alternative public-static path if the consumer needs HTTP access:

```text
public/exports/bookermove-leads-va.json
```

This N does not build the exporter. The first implementation should be a weekly JSON writer that reads existing MARKO lead data and emits a static file.

## Export Envelope

```json
{
  "generated_at": "2026-05-13T00:00:00.000Z",
  "market": "Virginia movers",
  "source_system": "MARKO",
  "schema_version": "1.0",
  "leads": []
}
```

## Lead Shape

Each item in `leads` must include:

```json
{
  "lead_id": "L016",
  "company_name": "Example Moving Co",
  "vertical": "moving_company",
  "city": "Richmond",
  "state": "VA",
  "service_area": "Richmond, VA metro",
  "website": "https://example.com",
  "phone": "804-555-0100",
  "email": "info@example.com",
  "lead_source": "manual_research",
  "leakage_signals": [
    "no_online_booking",
    "missing_quote_flow"
  ],
  "priority": "call_today",
  "score": 91,
  "recommended_action": "Call today and lead with after-hours quote capture.",
  "outreach_angle": "BookerMove can catch missed quote requests when the office is closed.",
  "last_verified_at": "2026-05-13T00:00:00.000Z",
  "source_confidence": "medium",
  "compliance_notes": "Public business contact info only; verify before outreach; respect opt-outs."
}
```

## Field Rules

`lead_id`: Stable MARKO lead id or export-scoped id. Required.

`company_name`: Public business name shown to BookerMove users. Required.

`vertical`: Must be `"moving_company"` for this bridge.

`city`: Business city or primary market city. Required.

`state`: Two-letter state code. For this Virginia export, use `"VA"`.

`service_area`: Human-readable service area. Use city/state if no richer area exists.

`website`: Public website URL when available. Empty string if unknown.

`phone`: Public business phone when available. Empty string if unknown.

`email`: Public business email when available. Empty string if unknown.

`lead_source`: Source label such as `manual_research`, `marko_export`, `csv_import`, or `purchased_compliant_list`.

`leakage_signals`: Array of normalized strings from the enum below.

`priority`: One of `call_today`, `warm`, `low`, `reject`.

`score`: Integer 0-100. Higher means better BookerMove fit.

`recommended_action`: One short operator action for Jay or BookerMove ops.

`outreach_angle`: Customer-safe pitch angle. Do not mention scraping, internal scoring, vendors, or private enrichment.

`last_verified_at`: ISO 8601 timestamp for last human or system verification.

`source_confidence`: Suggested values: `high`, `medium`, `low`.

`compliance_notes`: Per-lead or batch compliance reminder.

## Priority Enum

`call_today`: Strong fit, has phone or high-confidence contact, clear leakage signal.

`warm`: Some fit, useful contact or website signal, but less urgent.

`low`: Weak fit, limited contact data, or unclear leakage.

`reject`: Do not import into active BookerMove outreach.

## Leakage Signal Enum

Allowed examples:

```text
no_online_booking
no_contact_form
weak_after_hours_capture
outdated_website
missing_quote_flow
weak_sms_followup
```

The exporter may add new normalized signals later, but BookerMove should render unknown strings as plain labels instead of failing.

## Compliance Rules

- Use publicly available business contact information only.
- Preserve source attribution per lead.
- Verify before outreach.
- Respect opt-out and do-not-contact requests.
- Do not imply endorsement, partnership, or prior relationship.
- Do not auto-email or auto-text from this bridge.
- Do not scrape Zillow, MLS, restricted sites, or hidden enrichment APIs.
- Customer-facing BookerMove UI should hide internal scraper, vendor, and scoring-engine language.

## Future BookerMove Import Plan

BookerMove should read the weekly JSON file server-side and render a Leads tab in the ops/customer dashboard.

Initial UI should show:

- Company name
- City and state
- Phone, email, and website if present
- Priority filter
- Leakage reason labels
- Score
- Recommended call or email angle
- Source confidence and last verified time
- Compliance note

Later BookerMove actions can mark a lead as contacted, later, or reject inside BookerMove. Those actions should be BookerMove-owned state, not writes back into MARKO.

## Non-Goals For This N

- No MARKO runtime dependency inside BookerMove.
- No repo merge.
- No scraper expansion.
- No database migration.
- No paid API dependency.
- No outreach automation.
- No BookerMove code edits.
