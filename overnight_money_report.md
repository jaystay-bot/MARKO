# MARKO Overnight Money Report

Generated: 2026-05-14 · build pass: N-MARKO-ONE-CLICK-MONEY-QUEUE
Source: `money_queue.json` (real public mover data + observed leaks)

**Policy this run:** `auto_send: false`, `default_send_mode: dry_run`,
`MARKO_OUTREACH_LIVE` not set. **No outreach has been sent.**

---

## Top 5 movers to contact

| # | Business | Email | Leak | Score |
|---|---|---|---|---|
| 1 | Moxie Movers | booking@moxiemovers.com | owner_overload | 115 |
| 2 | Mitchell's Movers | mitchmovers@gmail.com | owner_overload | 85 |
| 3 | Mr Moving LLC | mr.movingllc@gmail.com | owner_overload | 75 |
| 4 | Mighty Moves | movers@mightymoves.us | owner_overload | 60 |
| 5 | Rent The Help | info@rentthehelp.com | owner_overload | 50 |

All five are call_today. All five share the same dominant leak — owner-operated
+ no automated capture surface — which is exactly the pain BookerMove sells
against. The drafts in `money_queue.json` open on that leak by name; nothing is
generic.

## Strongest leak discovered

**`owner_overload`** — looks owner-operated (gmail or 804 local) and the
public site has no automated capture surface, so after-hours and
between-job callers go to voicemail.

6 of 7 ranked drafts use this leak as the pitch driver. It's the most
direct money-loss story (every missed call = lost booking) and the one
BookerMove most cleanly replaces.

## Best-performing pitch angle (per draft)

> "I'm guessing you take most of the booking calls yourself. Looked at {website} — there's no automated way for a customer to leave their move details when you're on a job, so those calls go to voicemail."

Followed by the offer ("ONE moving lead from your service area free as a
test, $20-$50 per lead after if it's a real fit") and the public capture
URL with `mover_hint=<their mover_id>` for attribution.

## Highest-confidence buyer

**Moxie Movers** (rank 1, score 115, confidence band: high, close-prob
band: 20-35%).

Why: hits every score component except mobile-checked — owner_operated,
weak_capture, stale_site, missed_call_risk, reachable_email,
covers_hot_zip (Chesterfield/Midlothian 23112). Single strongest target
in the whole queue.

## ZIP with strongest opportunity density

**23112 Chesterfield (Midlothian corridor)** — `moved_in` signal, HIGH
confidence (Chesterfield County permits + USPS/HUD vacancy). Three
top-5 movers cover this ZIP (Mitchell's, Mr Moving, the demand-routing
preview matches Mitchell's first). One YES from any of them turns this
ZIP into a live revenue line.

## Estimated revenue potential

From `overnight_money_report.json.estimated_revenue_band`:
**$77 – $602/week** assuming 5 call_today movers at 5–25% close on the
$20–$50 first-paid-lead test, plus current overnight inbound at 10–30%
close. Wide band on purpose — we have no real conversion history yet.

## Best capture URL

```
https://quote.bookermove.com/quote?source=marko&campaign=richmond_movers&mover_hint=<MOVER_ID>
```

Each money_queue row carries its mover-specific URL pre-baked. The
`mover_hint` survives the hidden-input round-trip into `lead.notes`
(verified in this turn's TRUTH).

## Rate-limit safety (documented; not enforced server-side yet)

| Setting | Value |
|---|---|
| max_sends_per_day | 10 |
| min_minutes_between_sends | 5 |
| duplicate_window_days | 30 |
| domain_warming_note | First 7 days on a fresh sending domain: cap at 5/day. Reply-rate matters more than volume. After 7 clean days, raise to 10/day. |

The `/review/<id>/send` route already requires `MARKO_OUTREACH_LIVE=1`
AND (`MARKO_SMOKE_REDIRECT_TO` OR `?confirm_real=1`) for any live send
— that's the per-call belt. The day cap above is operator discipline,
not yet a server enforcement; that's a small follow-up if needed.

## Verification

- `money_queue.json`: 7 drafts, all `send_status: draft_only`
- 0 broken template variables (no `{business}`, `{website}`, `{capture_url}` literals leaked)
- All emails reference a real observed leak (no hallucinated evidence)
- `/review` requires `ADMIN_TOKEN`; `/review/<id>/approve|skip|retry_later` mutates status only; `/review/<id>/send` defaults to dry_run
- `/quote` mobile chrome intact (verified via existing `_truth/n_marko_money_engine_verify.py`)
- `outreach_log.json` empty until first send fires

## Next step (Jay, this requires you)

1. Open `https://<your-host>/review?token=<ADMIN_TOKEN>` on your phone or desktop.
2. Read draft #1 (Moxie Movers).
3. Click **approve** if it reads right, **edit** if you want to tweak the wording, **skip** if it's wrong, **retry_later** if you'd rather come back to it.
4. To send the test, click **send**. With current env (`MARKO_OUTREACH_LIVE` unset), this is a **dry-run** — it returns the rendered email and logs to `outreach_log.json` but never opens a socket. Safe to click.
5. To actually send the live test:
   - Set `MARKO_SMOKE_REDIRECT_TO` to your own inbox (recommended — real send pipeline, but the email goes to you, not Moxie). Then click send.
   - OR set `MARKO_OUTREACH_LIVE=1` and pass `?confirm_real=1` on the send URL (this is the only path that actually emails Moxie — irreversible).
