# TRUTH_RESULT_N.md

N: N-MARKO-RUNTIME-STATE-CLEANUP-AND-SMOKE-GREEN

Date: 2026-05-13

## Final Status

PASS

## Commands

| Command | Exit Code | Result |
| --- | ---: | --- |
| `python smoke_test.py` | 0 | 268/268 passed. |
| `python -m py_compile main.py dashboard.py commands.py scraper.py cli.py` | 0 | Compile OK. |
| `python playwright_smoke.py` | 0 | 39/39 passed. |
| `git diff -- leads.json marko_log.json` | 0 | No runtime JSON diff. |
| `git hash-object leads.json` vs `git rev-parse HEAD:leads.json` | 0 | Hashes match. |
| `git hash-object marko_log.json` vs `git rev-parse HEAD:marko_log.json` | 0 | Hashes match. |

## Runtime Backup

- `_truth/runtime_backups/N_MARKO_RUNTIME_STATE_CLEANUP_20260513-184717/leads.dirty.json`
- `_truth/runtime_backups/N_MARKO_RUNTIME_STATE_CLEANUP_20260513-184717/marko_log.dirty.json`

## Remaining Dirty Tree

Tracked intentional changes:

- `.gitignore`
- `CURRENT_N.md`
- `S1_LOCKED_N.md`
- `_truth/n273_verify.py`
- `_truth/n274_verify.py`
- `_truth/n275a_verify.py`
- `_truth/n275b_verify.py`
- `_truth/n275c_verify.py`
- `smoke_test.py`

Untracked intentional files from bridge/proof work:

- `A1_OUTPUT_N.md`
- `NEXT_N.md`
- `TRUTH_RESULT_N.md`
- `_truth/drive.py`
- `_truth/money_truth.py`
- `docs/bookermove-lead-bridge-contract.md`

## Cost

- tokens: not exposed by local runner; estimated 18k-24k for this cleanup N.
- runtime: about 25 minutes.
- cost_$: 0.00 external spend; no paid APIs, no deploy, no email/SMS sent.
