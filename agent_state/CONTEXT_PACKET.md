# Context packet — tight (under 8k tokens)

## Stack
Python 3.x · Flask via dashboard.py · Werkzeug · Playwright (chromium) ·
storage.py (JSON local + Upstash KV in prod via STORAGE_BACKEND env).
Optional: Ollama at localhost:11434 (opt-in via MARKO_CHAT_USE_OLLAMA=1).
Optional: weasyprint installed but unusable on this Windows box
(GTK runtime missing); Playwright/Chromium covers PDF render.
No paid APIs. No Docker. No DB rewrites. No vector store.

## Live URLs (operator)
- Lead Intelligence: marko-teal.vercel.app/ (existing dashboard)
- Public intake: quote.bookermove.com → /quote (302)
- Operator cockpit: /cockpit?token=<ADMIN_TOKEN>
- Money mode: /money?token=<ADMIN_TOKEN>
- Review queue: /review?token=<ADMIN_TOKEN>
- Conversion analytics: /admin/conversions?token=<ADMIN_TOKEN>
- Leak engine dashboard: /leaks?token=<ADMIN_TOKEN>
- Chat closer API: POST /api/marko/chat?token=<ADMIN_TOKEN>
- PDF audit: GET /leaks/run/<id>/<biz>/audit.pdf?token=...
- TalkBot inbound: POST /api/talkbot/inbound (X-Talkbot-Token gated)

## Live-send posture (must remain off until first YES)
MARKO_QUOTE_LIVE_SEND, MARKO_OUTREACH_LIVE, MARKO_MOVER_ALLOWLIST = unset.

## Engine modules (leak-engine product)
- marko_leak_engine.py — Playwright scanner + leak detector + scorer
  + outreach + Loom + commercial generator. CLI:
  `--city --niche [--targets]`.
- marko_leak_batch.py — multi-job runner, isolated per-job folders.
- niche_config.py — 7 niches with aliases + weight overrides + commercial themes.
- marko_leak_dashboard.py — read-only helper for /leaks routes.
- marko_chat.py — grounded chat builders + optional Ollama reframer.
- marko_pdf.py — server-rendered PDF audit (weasyprint→Playwright fallback).

## Output convention
`leak_reports/<run_id>/<biz_slug>/{report.json, report.md,
screenshot_desktop.png, screenshot_mobile.png, audit.pdf}` plus
`leak_reports/<run_id>/run_summary.json`. N004/N005/N006/**N007**
verifier evidence in `leak_reports/_n00X_evidence/`.

## Verifiers (run any individually; all `python _truth/n_*.py`)
- n_leak_engine_verify.py (N002)
- n_dashboard_demo_verify.py (N004)
- n_chat_closer_verify.py (N005)
- n_pdf_audit_verify.py (N006)
- **visible_truth_watcher.py (N007 — also a continuous watcher)**
- n_cockpit_verify.py
- n_conversion_tracking_verify.py
- n_money_queue_verify.py
- n_marko_money_engine_verify.py
- n_marko_demand_verify.py
- n_public_intake_redirect_verify.py

## N007 watcher quick reference
- One-shot: `python _truth/visible_truth_watcher.py`
- Headed: add `--visible`
- Continuous loop: add `--watch [--interval N]`
- Skip PDF (faster loops): add `--no-pdf`
- Status log: `_truth/visible_truth_watcher_status.log`
  (JSON-per-line, append-only)
- Failure screenshots: `leak_reports/_n007_evidence/<ts>_<slug>.png`
- 11 routes checked; PDF check verifies report.json hash unchanged
  before/after

## Discovery sources known
- leads.json filter (movers + dog_groomer rows real)
- operator-supplied URL list (`--targets` CLI flag)

## What's intentionally NOT built
- CRM, auth rewrite, Stripe, multi-tenant, queue workers, Docker, K8s
- LangChain, vector DB, "AI swarm"
- Auto-send / bulk outreach
- Web-discovery sources (deferred behind --targets)

## Last 3 PASSes
- **N007**: Visible Truth Watcher (11 routes, PDF data-mutation
  guard, status log, --watch/--visible/--no-pdf flags; 1 file added)
- N006: Server-rendered audit.pdf + Download button (10.41s)
- N005: Chat closer panel + grounded answers + Ollama-off fallback (3.04s)
