# SESSION_LOG.md (append-only)

## 2026-05-12 — N028–N035: scoring + Call First + tier chips
**PASS.** Live: `marko-teal.vercel.app` commit `ab40512`.
- 42/42 smoke, 21/21 Playwright.
- Skipped: N038 Rust lane (no measured bottleneck), N051–N060 8 parallel agents (theater).
- Deploy: GitHub→Vercel auto, READY in 12s.

## 2026-05-12 — N271: storage abstraction (local + kv backends)
**PASS.** Live: `marko-teal.vercel.app` commit `c0a902a`.
- storage.py: read_json/write_json API, STORAGE_BACKEND=local|kv switch.
- Local backend = default = current file-on-disk behavior (atomic tmp+rename).
- KV backend = Upstash Redis via stdlib urllib REST (no new deps).
- _kv_key_from_path: leads.json -> marko:leads. Same call sites serve both.
- StorageNotConfigured raised when STORAGE_BACKEND=kv but creds missing.
- is_persistent() returns False on local+Vercel and kv-without-creds.
- Routed every JSON file access through storage:
  commands.load_json/save_json delegate; dashboard, scraper, enrich_batch
  go through commands. FileNotFoundError contract preserved.
- Banner now keyed off is_persistent (was: is_vercel). Live banner copy
  names STORAGE_BACKEND=kv + links to README.
- Tests: 268/268 smoke (+19 new), 39/39 Playwright. vercel build ok.
- README: 4-step Vercel persistence enable section.
- Operator next: provision Upstash via Vercel Marketplace, set
  STORAGE_BACKEND=kv in env, redeploy -> banner disappears, writes
  persist across cold starts.

## 2026-05-12 — N262: polish pass (find_lead + orphan wiring + perf snapshot)
**PASS.** Live: `marko-teal.vercel.app` commit `5a09af2`.
- 249/249 smoke. 34/34 Playwright (added: welcome partial renders, FCP under 5s).
- commands.find_lead(lead_id) replaces 7 inline next-generator sites in dashboard.py.
- templates/partials/_welcome.html: 22 lines extracted from index.html.
- enrich_batch.py wired as `python cli.py enrich [--write]`.
- mode_call.html wired via linter's /mode/call?i=N route. Confirmed live.
- /lead/<id>/close wired by linter; CLOSED_WON/CLOSED_LOST added to LEAD_STATUSES.
- Perf baseline captured in playwright (FCP 160ms, page 246KB on localhost).
- Skipped: print() cleanup (operator-useful CLI feedback), Call First partial
  (~100 lines, linter actively edits — risk > reward), Lighthouse CLI (added
  unnecessary tool dep; in-Playwright snapshot is the honest alternative).

## 2026-05-12 — N261: wire marko_brain + mockup catalog
**PASS.** Live: `marko-teal.vercel.app` commit `534c311` (after `fbafba0` core).
- 249/249 smoke. 32/32 Playwright.
- 34 routes (+2: /lead/<id>/brain, /mockup/<slug>/<variant>).
- marko_brain.py orphan→wired: closability + best_angle + recommended_first_action
  attached to every Call First card as _brain. Path pill + closability + reason
  + mockup link render inline.
- Mockup catalog whitelist (templates/mockup/*) cataloged at import time —
  path traversal impossible. 10 niche slugs × 2 variants live.
- niche_to_mockup_slug normalizes messy real-lead niches ("dog groomer",
  "med spa") to filename slugs ("groomers", "med_spas").
- best_mockup_variant: high-urgency niches default to emergency, slower to booking.
- Linter also shipped: enrich_batch.py, mode_call.html, templates.json
  pretty-print + BookerMove preset enrichments. Preserved.

## 2026-05-12 — N181+N182+N183+N191+N193: money mode + 5-tier + dispositions + compliance
**PASS.** Live: `marko-teal.vercel.app` commit `262df07` (CSS patch on top of `c7eff69`).
- 122/122 smoke (new: voicemail, why_they_buy, /voicemail + /why routes,
  DNC exclusion from queue, set_lead_disposition safety, pipeline_summary).
- 30/30 Playwright (new: MAKE MONEY TODAY section, 5-tier score CSS used).
- 26 routes (was 21). New: /lead/<id>/voicemail, /lead/<id>/why,
  /lead/<id>/disposition/<status>, /lead/<id>/stop, /api/compliance.
- 5-tier scoring shipped: MONEY (>=90), HOT (>=70), GOOD (>=40), LOW (>=20), DEAD (<20).
- N193 compliance gate refuses real /send when config blockers present.
  Dry-run defaults ON.
- External linter delivered + integrated: marko_compliance.py (full N193),
  marko_brain.py (N262, currently unwired - candidate for next-N integration),
  11 niche landing-page mockups (templates/mockup/, currently no route).
- Skipped: N185 (background workers), N187 (TalkBot identification needs
  unreliable inference), N190 (cashflow needs manual forms), N195 (needs
  accumulated reply data), N196-N260 (speculative).

## 2026-05-12 — N081+N084+N090: intel route + email gen + focus banner
**PASS.** Live: `marko-teal.vercel.app` commit `eec2e4d`. READY.
- 84/84 smoke (added 4 new test blocks: money estimate, script, email gen,
  /intel + /email/<kind> routes via Flask test_client).
- 28/28 Playwright (added focus banner, priority class, preview email btn).
- 21 url_map routes (+2: `/lead/<id>/intel`, `/lead/<id>/email/<kind>`).
- Preserved external linter changes: script-block + money-chip + welcome banner
  + tier chips + HOT badges all rendering live.
- marko_intel.py is no longer orphan — wired to two routes, used by index().
- Skipped: N082 Leaflet map (real lift, defer to focused turn),
  N085 lead scoring v2 (current ships HOT/GOOD/WEAK already),
  N086 market expansion (Flask-sync constraint), N087 action bar (UI overhaul),
  N088 pipeline metrics (needs real reply data), N091-N120 (speculative).

## 2026-05-12 — N041–N080: owner + pain-points + session resume
**PASS.** Live: `marko-teal.vercel.app` commit `7d0f448`. READY in 11s.
- 59/59 smoke (3 new test blocks: owner_extractor, pain_points, preset_route).
- 25/25 Playwright (4 new asserts: presets, welcome banner, HOT-only, Next button).
- vercel build: ok. 19 routes (added /campaign/preset/<id>).
- Shipped N041 owner, N044+N045 pain-points, N046 cold-call ammo chips,
  N048 welcome banner, N049 one-click presets, N050 touch counts, N061 subset.
- Skipped: N042 (no background workers in sync Flask),
  N051–N060 (8-agent theater), N047 (already shipped), N071–N080 (speculative).
- **External anomaly:** `marko_intel.py` (260 lines, N089 "Missed Money Estimator")
  appeared in the working tree mid-turn from outside this agent. Committed since
  it compiles + imports clean, but **it is currently unused dead code** —
  no route, no template, no test references it. Decide next turn whether to
  wire it in (`/lead/<id>/intel` route + Call First augment) or remove it.
