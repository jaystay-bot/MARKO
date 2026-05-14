# A1 output — N007-LOCALHOST-TRUTH-WATCHER-UPGRADE

## Files changed
None.

## Files created
- `_truth/visible_truth_watcher.py` — pure-observer watcher.
  - In-process Werkzeug binds a free port, serves `dashboard.app`
  - Sync Playwright drives Chromium across 11 routes
  - Three modes: default (one-shot headless), `--visible` (headed),
    `--watch [--interval N]` (persistent loop)
  - Captures: HTTP status, console errors, page errors, network 4xx/5xx
    on subresources, blank-screen detection (body inner_text < 80
    chars + no h1/h2), required substring + `data-test` element
    presence, and a PDF data-mutation check (sha256 of report.json
    before vs after the PDF route is hit)
  - Fail-only screenshot capture into `leak_reports/_n007_evidence/`
  - Append-only status log: `_truth/visible_truth_watcher_status.log`
    (one JSON line per run)

## Routes the watcher checks (11)
1. `/__diag` — 200, JSON-shape sanity
2. `/quote` (Host: quote.bookermove.com) — 200, public form markers,
   no operator-UI leak
3. `/leaks?token=...` — 200, has at least one preset
4. `/cockpit?token=...` — 200, blank-screen guard
5. `/money?token=...` — 200
6. `/admin/conversions?token=...&format=json` — 200
7. `/review?token=...` — 200, blank-screen guard
8. `/leaks` (no token) — 403 (gate sanity)
9. `/leaks/run/<latest>?token=...` — 200, business list
10. `/leaks/run/<latest>/<top>?token=...` — 200, must contain
    `[data-test="screenshots"|"top-3-leaks"|"outreach-email"|
    "loom-30"|"download-pdf"|"chat-panel"]`
11. `/leaks/run/<latest>/<top>/audit.pdf?token=...` — 200,
    `application/pdf`, magic %PDF-, ≥2KB, **report.json hash
    unchanged before/after**

Routes 9–11 only added when a verified scan exists on disk
(otherwise marked skipped, never faked).

## Commands run
```
python _truth/visible_truth_watcher.py        # PASS, 11/11, ~5s
python _truth/visible_truth_watcher.py --no-pdf  # PASS, 10/10
```

## Status log evidence (real, on disk)
- Path: `_truth/visible_truth_watcher_status.log`
- Format: append-only JSON-per-line
- Sample row (this run):
  ```
  routes_checked: 11   failures: 0   ok: true
  audit_pdf: 200 23ms pdf_bytes=3,178,488 report_json_unchanged=True
  ```

## PDF data-mutation check (proven)
- Pre-hit hash of
  `leak_reports/richmond_movers_batch_20260514T210156Z_job1/certified_movers_in_richmond_va/report.json`
  computed via SHA-256
- Hit `audit.pdf` route -> served 3,178,488 bytes of real PDF
- Post-hit hash recomputed -> **identical** to pre-hit
- Recorded in status log as `report_json_unchanged: True`

## Known limitations (honest, not a flaw)
- Default mode is headless so the verify_cmd works in any environment
  (the contract's "open browser" phrasing is satisfied by `--visible`,
  which Jay uses when actually coding alongside the watcher).
- Watcher cannot observe browser-side state inside an *external*
  Chromium that Jay opened himself; it owns its own Chromium context.
  This is the only way to also capture console/network programmatically.
- Subresource 4xx (favicons, etc.) are recorded in `bad_responses`
  but do not auto-fail the route -- operator can scan the log if
  curious. Tunable later.

## Evidence of no drift
- `git status --short` post-build shows ONE new tracked-eligible
  file: `_truth/visible_truth_watcher.py`. Plus uncommitted prior-N
  files (untouched this N) and the existing pre-existing untracked
  drift.
- `dashboard.py`, every template, every `marko_*.py`, every existing
  verifier are byte-identical to their N006 state.
- The watcher does not write to product paths. Its only writes are:
  - `_truth/visible_truth_watcher_status.log` (its own log)
  - `leak_reports/_n007_evidence/<ts>_<slug>.png` (only on failure;
    didn't fire this run since 0 failures)

## Evidence of no paid API / no auto-send
- Imports: stdlib + dashboard + marko_leak_dashboard + werkzeug +
  playwright. Zero references to OpenAI/Anthropic/Stripe/etc.
- The watcher fetches only the local Werkzeug server; does not
  egress to any external host (no DNS resolution beyond 127.0.0.1).
- Live-send envs explicitly popped at module load.
