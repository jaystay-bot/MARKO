# S1 Locked — N007-LOCALHOST-TRUTH-WATCHER-UPGRADE

## Mode
ATOMIC · LOCALHOST FIRST · VISUAL TRUTH ONLY · NO DEPLOY · NO DRIFT

## Scope
Single new file: a localhost watcher that boots the existing Flask
app in-process, drives Chromium across the operator routes, reports
real visual/runtime failures, captures evidence, and writes a
status log. Observer only -- never touches product code.

## Allowed files
- `_truth/visible_truth_watcher.py` (new) — the watcher itself
- `agent_state/*` per contract

## Forbidden files / surfaces
EVERY other file. Specifically: dashboard.py, templates/*, marko_*,
routing.py, email_client.py, storage.py, niche_config.py, vercel.json,
env files, the report.json under any leak_reports/<run>/<biz>/.

The watcher must be a pure observer: zero edits to product code or
report data. Verifier asserts this via report.json hash before/after.

## Forbidden work
- editing product UI, templates, or any backend route
- redesigning anything
- deploying or pushing
- changing auth (uses existing ADMIN_TOKEN gate)
- mutating report.json or any leak_reports artifact
- adding paid APIs
- claiming PASS without screenshot/log evidence

## Deliverables
1. `_truth/visible_truth_watcher.py` with three modes:
   - default (one-shot, headless): boots, runs checks, writes log,
     exits 0/1. This is the contract's verify_cmd path.
   - `--visible`: headed Chromium window so Jay sees the checks live
   - `--watch [--interval N]`: persistent loop; combine with
     `--visible` to keep the browser open during a coding session
2. Status log appended to
   `_truth/visible_truth_watcher_status.log` (one JSON line per run)
3. Evidence screenshots into
   `leak_reports/_n007_evidence/<timestamp>_<route_slug>.png` only on
   failure (not every route, not every run)

## verify_cmd
```
python _truth/visible_truth_watcher.py
```

## Routes the watcher checks (real, on this app)
1. `GET /__diag` -> 200, JSON shape sane
2. `GET /quote` (Host: quote.bookermove.com) -> 200, public form
   markers present, zero operator-UI leak
3. `GET /leaks?token=verify-token` -> 200, has at least one preset
4. `GET /leaks/run/<latest_run_id>?token=...` -> 200, business list
5. `GET /leaks/run/<id>/<top_biz>?token=...` -> 200, viewer chrome
6. `GET /leaks/run/<id>/<top_biz>/audit.pdf?token=...` -> 200,
   `application/pdf`, magic `%PDF-`, >=2KB
7. `GET /cockpit?token=...` -> 200
8. `GET /money?token=...` -> 200 (JSON)
9. `GET /admin/conversions?token=...&format=json` -> 200
10. `GET /leaks` (no token) -> 403 (gate sanity)

Per-route capture: status, console errors during page load, network
4xx/5xx for subresources, body-length blank check, screenshot saved
on failure.

## PDF data-mutation check
- Hash `report.json` before any PDF action
- Hit the audit.pdf route (which lazy-generates if missing)
- Re-hash `report.json` -- must be IDENTICAL (no write-back bug)
- Pass/fail recorded in the status log

## TRUTH plan
The watcher IS the verifier. Exit 0 = no failures across all 10
checks. Exit 1 = any failure with full detail printed and saved.

A "blank screen" is detected as: status 200 but body inner_text
length < 80 chars and no visible H1/H2 elements.

## Hard stops
- Werkzeug fails to bind a free port -> exit 1, log reason
- Playwright fails to launch Chromium -> exit 1, log reason
- report.json hash changes during the PDF check -> FAIL
- a route returning 5xx that blocks subsequent checks -> log + continue

## parallel_safe
false (binds a TCP port; do not co-run with other Werkzeug verifiers)

## Cost / time
Local-only. One-shot run budget: <30s. Watch mode: indefinite, ~10s
per loop. No new dependencies. No new env vars.
