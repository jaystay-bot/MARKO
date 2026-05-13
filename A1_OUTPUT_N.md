# A1_OUTPUT_N.md

N: N-MARKO-RUNTIME-STATE-CLEANUP-AND-SMOKE-GREEN

Date: 2026-05-13

## Runtime JSON Cleanup

Dirty runtime files inspected:

- `leads.json`: contained verifier/webhook state (`email_status`, refreshed owner) not intended for deploy.
- `marko_log.json`: contained verifier webhook events and owner-refresh log entries not intended for deploy.

Backup created before restore:

- `_truth/runtime_backups/N_MARKO_RUNTIME_STATE_CLEANUP_20260513-184717/leads.dirty.json`
- `_truth/runtime_backups/N_MARKO_RUNTIME_STATE_CLEANUP_20260513-184717/marko_log.dirty.json`

Cleanup applied:

- Restored `leads.json` to `HEAD`.
- Restored `marko_log.json` to `HEAD`.
- Added `_truth/runtime_backups/` to `.gitignore`.

## Smoke Fix

Changed only `smoke_test.py` test harness expectations/mocks:

- Replaced legacy SMTP-password mock with `RESEND_API_KEY` + from-address mock.
- Updated mocked `commands.send_email` return tuples to the current 4-value contract.
- Used validator-compatible fake KV token.
- Mocked business-hours gate for send-path tests so tests do not depend on wall-clock time.
- Gave the batch-cap fixture unique domains so it tests hard batch size, not per-domain throttling.

## Verification

- `python smoke_test.py`: PASS, 268/268.
- `python -m py_compile main.py dashboard.py commands.py scraper.py cli.py`: PASS.
- `python playwright_smoke.py`: PASS, 39/39.

## Not Done

- No product UI change.
- No BookerMove export.
- No push.
- No deploy.
