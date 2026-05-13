# CONTEXT_PACKET.md — project state snapshot

## Stack
Flask + JSON storage + Vercel (GitHub auto-deploy). Single-page operator dashboard.

## Live URLs
- Primary: https://marko-teal.vercel.app
- Branch alias: https://marko-git-main-jaystay-bots-projects.vercel.app
- Repo: github.com/jaystay-bot/MARKO

## Modules
- `main.py` — Vercel WSGI entry. `app = Flask(__name__); app = dashboard.app` (static-detector + rebind pattern).
- `cli.py` — CLI surface (`python cli.py <command>`).
- `dashboard.py` — 18 Flask routes serving `templates/index.html`.
- `commands.py` — business logic: campaigns, leads, send, retry, scoring, call_queue.
- `scraper.py` — DDG search + same-domain subpage walk (`/contact`, `/about`, `/services`).

## Data files
- `campaigns.json`, `leads.json`, `marko_log.json`, `config.json`, `templates.json`
- **Writes do not persist on Vercel** (serverless). Banner warns. Local mode for real work.

## Tests
- `smoke_test.py` — 42 unit tests, uses `tempfile.TemporaryDirectory`, no network.
- `playwright_smoke.py` — 21 headless Chromium tests; auto-cleans `PWSmoke-*` campaigns.

## Domain constants
- Daily cap: 50 sends/day. Batch cap: 10. Retry cap: 3. Cooldown: 60min.
- Score signals: email, phone, both_contacts, website, owner, contact_page, local, niche.
- Thresholds: HOT ≥ 70, GOOD ≥ 40, WEAK < 40.
- Call queue: phone required, excludes FAILED/ARCHIVED/CALLED, sort by score desc → has-email → created_at.
- Statuses: NEW, CONTACTED (=SENT), RETRY, FAILED, REPLIED, ARCHIVED, CALLED.

## Working flows (do not break)
Call First → cold-call cheat sheet → Mark CALLED. Scrape with tier+niche chips. Retry Queue reset.
Template preview no-auto-send. Per-campaign CSV export. Leads filter (search/status/niche/contact/score).
