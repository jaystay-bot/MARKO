# CURRENT_N.md

**Turn:** N-MARKO-RUNTIME-STATE-CLEANUP-AND-SMOKE-GREEN
**Date:** 2026-05-13
**Status:** cleanup baseline verified, no deploy

## Honest scope this turn

- Back up dirty runtime JSON state before cleanup.
- Restore `leads.json` and `marko_log.json` to `HEAD` when confirmed as verifier/runtime drift.
- Fix only smoke-test harness drift caused by Resend credential gating, KV token validation, business-hours gating, and per-domain throttling.
- Run smoke, compile, and Playwright verification.

## Explicit non-goals

- No product UI changes.
- No BookerMove export generation.
- No scraper expansion.
- No database migration.
- No push.
- No deploy.

## Verify gate

- `python smoke_test.py`
- `python -m py_compile main.py dashboard.py commands.py scraper.py cli.py`
- `python playwright_smoke.py`
- `git status --short --untracked-files=all`
