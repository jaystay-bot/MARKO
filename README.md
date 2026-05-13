# MARKO

Local Python growth engine.

## Setup

```bash
pip install -r requirements.txt
```

## Run Dashboard

```bash
python dashboard.py
```

Open http://127.0.0.1:5000

## CLI Commands

```bash
python cli.py --help
python cli.py run <name> <project>
python cli.py add_lead <name> <email> <niche>
python cli.py send [--dry-run]
python cli.py log <count> [opens] [replies] [signups]
python cli.py analyze
python cli.py report
python cli.py scrape <niche> <city> <state>
```

## Files

- `cli.py` - CLI entry point
- `main.py` - Vercel/WSGI entrypoint (exposes Flask `app`)
- `commands.py` - Command logic
- `dashboard.py` - Flask UI
- `scraper.py` - Lead utilities
- `templates/index.html` - Dashboard template
- `campaigns.json` - Campaign data
- `leads.json` - Lead data
- `config.json` - Settings
- `marko_log.json` - Action log
- `templates.json` - Outreach/campaign/niche presets
- `smoke_test.py` - Local smoke tests (no network)

## Mutation Rules

- CSV export helpers are read-only. They return CSV strings and do not update lead, campaign, or log JSON.
- Intel and compliance helpers are read-only. They may return generated scripts, email previews, blockers, and checklist data, but they do not persist state.
- Status/disposition helpers, retry reset, send, campaign creation, and template creation intentionally mutate JSON and write audit entries where applicable.

## Persistent storage on Vercel

By default MARKO stores everything in local JSON files. That works fine for `python dashboard.py` on your machine, but Vercel's serverless runtime gives each request a fresh ephemeral filesystem — **writes do not persist between cold starts**. The deployed URL shows an amber "Demo mode" banner while this is the case.

To turn the live URL into a real, write-persistent dashboard, wire up Upstash Redis via the Vercel Marketplace (free tier, no credit card):

1. **Provision:** Vercel dashboard → your `marko` project → Storage tab → Create Database → Upstash Redis → Free tier.
2. **Connect:** Vercel injects `KV_REST_API_URL` and `KV_REST_API_TOKEN` into the project's env automatically.
3. **Flip the switch:** add a third env var: `STORAGE_BACKEND=kv` (Production scope).
4. **Redeploy.** The Demo-mode banner disappears; the next scrape, lead-status change, or campaign creation persists across cold starts.

The abstraction layer lives in `storage.py`. Two backends, one API (`read_json` / `write_json`). Keys derive from the file basename — `leads.json` → `marko:leads`, `marko_log.json` → `marko:marko_log`, etc.

**Caveats:**
- Upstash free tier has a ~1MB per-key limit. `marko_log.json` is the growth risk — periodically archive or prune.
- Concurrent writes to the same key race (last writer wins). Fine for one operator; not safe for multi-user.
- To roll back, unset `STORAGE_BACKEND` (or set to `local`) and redeploy. The banner returns; local-file behavior resumes.

You can inspect runtime backend status via `commands.get_storage_info()` or the diagnostic call `storage.backend_info()`.
