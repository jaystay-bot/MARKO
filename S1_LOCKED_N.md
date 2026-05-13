# S1_LOCKED_N.md - Runtime cleanup scope lock

## Active N

N-MARKO-RUNTIME-STATE-CLEANUP-AND-SMOKE-GREEN

## Allowed files

- `.gitignore`
- `CURRENT_N.md`
- `S1_LOCKED_N.md`
- `smoke_test.py`
- Existing `_truth/*.py` verifier hardening already present from previous safe-sync work
- `A1_OUTPUT_N.md`
- `TRUTH_RESULT_N.md`
- `NEXT_N.md`

## Runtime State Rule

- `leads.json` and `marko_log.json` must not remain dirty.
- Before restoring runtime JSON, preserve dirty copies under ignored `_truth/runtime_backups/`.
- Do not delete local runtime data without backup.

## Forbidden

- No product UI edits.
- No route changes.
- No scraper expansion.
- No database migration.
- No dependency changes.
- No BookerMove export generation.
- No push.
- No deploy.

## Verify command

```text
git status --short --untracked-files=all
python smoke_test.py
python -m py_compile main.py dashboard.py commands.py scraper.py cli.py
python playwright_smoke.py
```

## PASS rule

PASS only if:

- `python smoke_test.py` exits 0.
- `python playwright_smoke.py` exits 0.
- `leads.json` and `marko_log.json` match `HEAD`.
- Dirty tree contains only intentional code/control/doc changes.
