# Next N (written only after PASS)

## What now works (Z bridge)
- `python _truth/visible_truth_watcher.py` runs the full 11-route
  visibility check in ~5 seconds, exit 0/1
- `--visible` opens a real Chromium window so Jay sees the checks
  flash through the operator UI
- `--watch` keeps it looping; `--watch --visible` is the "browser
  stays open while I code" mode the contract called for
- Append-only status log captures every run; failure screenshots
  drop into `leak_reports/_n007_evidence/` only when something breaks
- PDF data-mutation guard proves the audit.pdf path doesn't write
  back into report.json (sha256 pre/post identical this run)
- Existing routes (/quote, /__diag, /cockpit, /money, /admin/*,
  /review, /leaks, /leaks/run/.../audit.pdf, /api/marko/chat) all
  green and untouched

## What Jay can do
- Open one terminal: `python _truth/visible_truth_watcher.py --watch
  --visible --interval 10`. A Chromium window opens, every 10
  seconds it sweeps the operator UI in front of you. Failures pop
  a red banner in stdout (">>> FAILURE STATE <<<").
- Open a second terminal and code; the watcher catches regressions
  in real time.
- Tail the status log: `Get-Content _truth/visible_truth_watcher_status.log
  -Wait` (PowerShell) for a live JSON feed.

## What remains manual
- Sending the email / making the call (auto-send still forbidden)
- Web-discovery for niches not in `leads.json`
- WON / DEAD lead tiers in /cockpit
- GTK install for weasyprint preference (Playwright PDF works)

## Exact next N recommendation

### N008-Local-Niche-Seed-Pack (RECOMMENDED first)
Still the single highest-impact small task: 3 of 4 demo presets
render disabled with "No verified scan yet." Adding 5–10 real
public business URLs per missing niche (barbers, nail salons, med
spas, roofers, plumbers) into `leads.json` flips them all to
enabled. Pure data, ~30 min/niche; engine handles every downstream
step. The watcher built in N007 will instantly start exercising
them.

### N008-One-Click-Outreach-Copy-Pack
Persist chat-layer outputs as `outreach.md` per business. Largely
already exists in `marko_chat`; just write to disk so they're
available without the dashboard open.

### N008-Watcher-Telemetry-Rollup
Read `_truth/visible_truth_watcher_status.log` and surface a tiny
"watcher health" tile on `/cockpit`: last run timestamp, ok/fail,
recent failure summary. Operator joy; closes the loop between the
watcher and the cockpit Jay already opens.

### Other parked candidates
- N008-Local-Leaderboard
- N008-Demo-Video-Recording-Flow (bigger build; defer)
- N008-BookerMove-Tie-In (gated on first paid customer)
- N008-Install-GTK-for-weasyprint (one system change; not pressing)

## What stays parked (forbidden until further notice)
- CRM, auth rewrite, Stripe, multi-tenant, queue workers
- Vector DB, RAG platform, LangChain, agent swarm
- Auto-send / auto-call / auto-SMS
- Docker / K8s / scaling theater
