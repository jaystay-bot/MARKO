# S1 Locked — N-MARKO-VISUAL-REBUILD-V1

## Mode
ONE SHOT · UI/UX ONLY · NO BACKEND CHANGES · NO ENGINE CHANGES ·
MOBILE FIRST · DEPLOY SAFE · VISUAL HIERARCHY PRIORITY

## Honest scope decomposition (declared up front)
The contract says "complete frontend rebuild" + "Linear/Stripe polish."
The 1863-line `templates/index.html` posts to 30+ existing form
routes. A full pixel-perfect rebuild of every internal widget is a
multi-day job, not a one-shot turn. This N ships the **visible IA
reframe + mobile-first nav + new TODAY hero + reorganized 4-tab
layout + copy refresh**. Per-widget pixel polish, animation, and
deep CSS overhaul of every legacy form is **explicitly deferred** to
follow-up Ns (V2, V3). The escape hatch (`/legacy`) keeps every
existing form a single tap away during transition.

## Allowed files
- `dashboard.py` (route changes only -- relabel index → today; add 4
  new routes: /, /pipeline, /campaigns, /settings, /legacy)
- `templates/_marko_shell.html` (new -- shared shell with header +
  bottom-tab nav + dark Linear-grade base CSS)
- `templates/today.html` (new -- TODAY hero + 5 queue cards)
- `templates/pipeline.html` (new -- mobile-first leads list)
- `templates/campaigns.html` (new -- presets + simplified campaign cards)
- `templates/settings.html` (new -- compliance + SMTP + retry +
  diagnostics consolidated, progressive disclosure)
- `agent_state/*` per contract

## Forbidden files / surfaces
EVERY backend module: routing.py, email_client.py, storage.py,
commands.py, marko_*, niche_config, scraper, marko_brain,
marko_compliance, marko_intel, marko_sequence. ZERO edits to
existing `templates/index.html` (it stays as `/legacy` exactly as-is).
ZERO edits to other existing templates (mode_call.html, leak_report.html,
cockpit.html, leaks.html, audit_pdf.html, quote.html, etc.). No env
vars. No vercel.json changes. No new Python deps.

## Forbidden work
- editing any backend module
- editing any existing template
- changing any existing route's behavior (except `/` which is
  explicitly redirected per contract)
- removing any existing functionality (escape hatch is `/legacy`)
- touching auth (no token gating added; existing posture preserved)
- adding paid APIs / Stripe / vector DB / LangChain / animations
- pixel-polishing every legacy widget (deferred)

## Deliverables
1. New mobile-first home screen at `GET /` ("Today's Money Opportunities")
   - Header: brand + bottom-tab nav (Today / Pipeline / Campaigns / Settings)
   - **TODAY'S BEST LEAD hero card** (single most actionable lead from
     existing `commands.call_queue()`):
     - business name (large)
     - dominant pain/leak (one line)
     - estimated value range (from existing `_offer` data)
     - confidence band (HOT/GOOD/WEAK)
     - **one massive CALL button** (`tel:` link)
     - secondary EMAIL button (`mailto:` or `/lead/<id>/email/cold`)
   - Below hero: 5 queue cards in priority order:
     1. Call First (top 5)
     2. Follow-Ups Due (sequence step 4 or 5 due)
     3. Safe Email Queue (pending sends)
     4. Revenue At Risk (no-touch in N days, HOT score)
     5. New Hot Leads (newly scraped HOT)
   - "Open full controls →" link to `/legacy` at the bottom
2. PIPELINE screen at `GET /pipeline`
   - mobile-first leads table (cards on mobile, condensed table on desktop)
   - status color pills (NEW / CONTACTED / INTERESTED / BOOKED / DEAD)
   - per-row: name, niche, score, last action, primary action button
   - filter: hot/warm/cool, status, niche
3. CAMPAIGNS screen at `GET /campaigns`
   - existing presets as cards (4-up grid)
   - active campaigns as cards
   - "Find Leads" launcher (delegates to existing `/scrape`)
   - export buttons (delegate to existing `/export/*` routes)
4. SETTINGS screen at `GET /settings`
   - Compliance config (delegates to `/config/compliance`)
   - Diagnostics (links to `/__diag`, watcher status log)
   - SMTP / live-send env status (read-only, from existing env_status())
   - Retry / activity (collapsed by default)
   - "Open full operator dashboard" link to `/legacy`
5. `GET /legacy` -- existing `templates/index.html` rendered exactly
   as-is (zero functional regression, every form still works)

## Visual direction
Dark mode, Linear/Stripe-grade restraint:
- bg #0a0c0e, panels #141a21, hairline borders #232c36
- accent green #00d68f (one CTA per screen), money amber #ffd24c
  for revenue, hot red #ff5c5c reserved for HOT-only emphasis
- system font stack (no font preload; fast first paint)
- 16px base, 1.5 line-height, generous spacing (12px / 18px / 28px rhythm)
- bottom-tab nav 60px tall, safe-area padding for notched iPhones
- single-column on mobile, 2-up grid on tablet, 3-up on desktop
- ZERO animations, gradients, glows, or "futuristic" gimmicks

## Copy reframe
- Header: "MARKO -- Today's Money Opportunities" (was "Lead Intelligence")
- Brand subtitle: "What to do right now to close money."
- Hero label: "Today's best lead"
- Tab labels: Today · Pipeline · Campaigns · Settings
- Action labels: "Call now" / "Email" / "Mark contacted" / "Skip" /
  "Follow up later"

## verify_cmd
```
python _truth/visible_truth_watcher.py
```
Plus:
- visit `/`, `/pipeline`, `/campaigns`, `/settings`, `/legacy` -- all 200
- visit each from mobile UA (375x812) -- all render without horizontal scroll
- existing operator routes (/cockpit, /money, /leaks, /quote, /__diag)
  still 200 (regression)
- legacy form posts still resolve (the route targets are untouched;
  watcher catches any 5xx from incidental data flow)

## Hard stops
- ANY existing route returns non-200 after the change → FAIL
- new screens overflow horizontally on 375px mobile → FAIL
- console errors during render → FAIL
- the watcher's 11 existing checks regress → FAIL

## parallel_safe
false (binds a TCP port via watcher)

## Cost / time
Local-only. Build: ~45 min of template work (5 new templates +
route changes). Verifier runtime: ~15s (existing watcher + new
mobile-viewport checks). Net new deps: zero.

## Locked.
