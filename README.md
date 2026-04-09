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
python main.py run <name> <project>
python main.py add_lead <name> <email> <niche>
python main.py send
python main.py log <count> [opens] [replies] [signups]
python main.py analyze
python main.py report
```

## Files

- `main.py` - CLI entry point
- `commands.py` - Command logic
- `dashboard.py` - Flask UI
- `scraper.py` - Lead utilities
- `templates/index.html` - Dashboard template
- `campaigns.json` - Campaign data
- `leads.json` - Lead data
- `config.json` - Settings
- `marko_log.json` - Action log
