# S1_LOCKED_N.md — Architect scope lock

## Allowed files (additive only)
- `commands.py`
- `scraper.py` (extend `extract_contact_from_url` return signature)
- `dashboard.py` (new routes + context vars)
- `templates/index.html` (surgical edits; preserve all existing sections)
- `templates.json` (expand campaign_presets with niche+tier)
- `smoke_test.py` (additive tests)
- `playwright_smoke.py` (additive assertions)
- `LESSONS.md`, `SESSION_LOG.md`, `CONTEXT_PACKET.md` (this 8-Hat set)

## Forbidden
- `main.py`, `cli.py`, `vercel.json`, `requirements.txt` — do not touch
- Removing existing routes, sections, columns, or features
- Background workers / long-running threads (Flask sync model)
- New runtime dependencies

## Verify command (every check must PASS)
```
python -m py_compile main.py dashboard.py commands.py scraper.py cli.py
python smoke_test.py            # must be > 42/42 (additive)
python cli.py --help
python -c "from main import app; print(app)"
python -c "from dashboard import app; print(len(app.url_map._rules))"
vercel build --yes
python playwright_smoke.py      # must be > 21/21 (additive)
git push origin main            # triggers Vercel auto-deploy
# live URL feature check post-deploy
```

## Promote rule
- All verifies green → commit → push → poll deploy READY → live-page feature grep
- Any verify fail → STOP, fix, re-run, do not commit
