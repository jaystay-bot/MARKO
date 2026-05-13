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
