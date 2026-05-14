# S1 Locked — N-MARKO-VISUAL-REBUILD-V2

## Mode
ATOMIC · UI/UX ONLY · NO ENGINE CHANGES · MOBILE-FIRST · DEPLOY SAFE
· V1 RESTRAINT PRINCIPLES PRESERVED

## Scope
Replace the most-used legacy widgets with mobile-first equivalents
**inside the new Pipeline screen only**. Specifically:
- per-row primary action: **Mark contacted** (one-tap)
- per-row primary CTA: **Call** (existing tel:)
- expandable lead detail panel via `<details>` (no JS framework)
- expanded panel reveals: phone/email/site, pain points, generated
  script, sequence state, full disposition palette (BOOKED, INTERESTED,
  NOT_INTERESTED, CALLBACK, VOICEMAIL)

Other screens (Today / Campaigns / Settings / Legacy) untouched.
Engine untouched. No new Python deps. No new env vars. No animations.

## Allowed files
- `templates/pipeline.html` (full redesign of leads list)
- `dashboard.py` (pipeline_view route enrichment ONLY -- mirrors the
  existing today() route enrichment pattern; no new routes; no
  changes to other routes)
- `agent_state/*` per contract

## Forbidden files / surfaces
EVERY other file. Specifically: every other template (today.html,
campaigns.html, settings.html, _marko_shell.html, leak_*.html,
cockpit.html, audit_pdf.html, quote.html, index.html), every backend
module (commands, marko_*, niche_config, scraper, routing,
email_client, storage), env vars, vercel.json. ZERO new routes.

## Forbidden work
- editing engine modules
- adding new dashboard routes
- changing any existing route's behavior beyond pipeline_view enrichment
- adding paid APIs / Stripe / vector DB / animations / glow / gradients
- removing any existing functionality from /legacy escape hatch

## Deliverables
1. Pipeline row (collapsed, default state):
   - business name + status pill + score pill
   - Call button (tel:)
   - Mark contacted button (POST to existing /lead/<id>/contact)
   - chevron / "more" affordance to expand
2. Pipeline row (expanded, via `<details>`):
   - phone, email, website (tappable)
   - pain points (real `pain_points` field if present)
   - generated script preview (collapsed inside <pre>)
   - sequence state label
   - full disposition palette: 5 buttons, each POSTs to existing
     /lead/<id>/disposition/<X> route (BOOKED / INTERESTED /
     NOT_INTERESTED / CALLBACK / VOICEMAIL)
   - secondary actions: open in /legacy for full controls
3. Per-lead enrichment in `pipeline_view()`: cap at 50 rows for perf;
   skip enrichment for leads in DEAD/ARCHIVED status.

## verify_cmd
```
python _truth/visible_truth_watcher.py
```
Plus: visit /pipeline on mobile UA (375x812), confirm zero horizontal
overflow, confirm `<details>` expansion works without JS, confirm the
"Mark contacted" form posts to the correct route (verifier hits the
form action attribute and the backend response).

## Hard stops
- any existing route returns non-200 → FAIL
- /pipeline overflows on 375px → FAIL
- form action wrong (would post to a 404) → FAIL
- console errors during render → FAIL

## parallel_safe
false (binds a TCP port via watcher)

## Cost / time
Local-only. Build: ~25 min (pipeline template + route enrichment).
Verifier runtime: ~10s (watcher + mobile sweep). Net new deps: zero.

## Locked.
