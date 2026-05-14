# MARKO MORNING MONEY REPORT

Generated: 2026-05-14 overnight build · no automated outreach fired
Source data: `leads.json` (9 real public mover businesses, niche=movers),
`movers.json` (buyer-registry coverage), `hot_zips.json` (derived from
public county permits, USPS/HUD vacancy, ACS, VCU calendar).

Policy this run: `auto_send: false`, `sms_sent: false`,
`live_email_sent: false`, `data_sources_public_only: true`.
**No mover was contacted. `MARKO_QUOTE_LIVE_SEND` and `MARKO_MOVER_ALLOWLIST`
are unchanged.**

---

## Status: HOLD

Contract asked for **25 real mover targets**. The "existing public mover
data" the contract restricts me to is `leads.json` filtered to
`niche=movers` — that slice has **9 unique businesses**. I refused to
fabricate the missing 16.

The 9 we do have are real, ranked, and ready to call this morning.
What's missing is **input data, not engine work** — the queue generator
will absorb new mover entries the moment they're added. Closing the gap
to 25 is a one-shot public-data sweep; the exact recipe is in "Blockers"
below.

Everything else passed:
- 9 ranked real targets, full outreach copy each (phone / email / SMS-text-do-not-send)
- 3 demand-side campaign angles (hot ZIPs from public signals)
- Capture URL verified: `/quote?source=marko&campaign=richmond_movers` → 200, public form, no operator UI leak
- Public-intake redirect verifier: 9/9 cases pass

---

## Top 5 to call first

Phone first — all five are Richmond-area local 804 numbers, owner-operated
signals, weak online lead-capture. Email is the fallback if no answer.

| # | Business | Phone | Email | Website | Why first |
|---|---|---|---|---|---|
| 1 | Moxie Movers | 804-928-1111 | booking@moxiemovers.com | moxiemovers.com | Stale site (copyright 2018) + no online booking + no contact form + has email — strongest weakness composite |
| 2 | Mr Moving LLC | 804-986-9480 | mr.movingllc@gmail.com | mrmovingllc.com | Gmail = owner-operated, very stale site (copyright 2011), no contact form |
| 3 | Mitchell's Movers VA | 804-920-0646 | mitchmovers@gmail.com | mitchellsmoversva.com | Gmail owner-operated, no online booking, covers Chesterfield + Midlothian (overlaps a HOT ZIP) |
| 4 | Mighty Moves | (804) 215-6330 | movers@mightymoves.us | mightymoves.us | Local 804, vanity domain (small op), no contact form |
| 5 | Rent The Help | (804) 249-9024 | info@rentthehelp.com | rentthehelp.com | Local 804, no contact form, single-city Richmond coverage |

Remaining four in the queue (All My Sons, MiniMoves, College Hunks,
HireAHelper) are chains/aggregators — call only after the top 5 are
exhausted; HireAHelper is a competitor aggregator and ranks last (-50).

---

## Top 3 ZIPs to push (demand side)

From `hot_zips.json` (public permit + vacancy + calendar signals):

| ZIP | City | Signal | Confidence |
|---|---|---|---|
| 23112 | Chesterfield (Midlothian corridor) | `moved_in` (Chesterfield permits + USPS/HUD vacancy) | high |
| 23233 | Henrico (Short Pump) | `nearby_homeowner` (Henrico permits portal) | medium |
| 23220 | Richmond (VCU campus area) | `nearby_homeowner` — seasonal August move-in surge | low (but cheap pre-position) |

---

## Best phone script (use verbatim for the top 5)

> "Hi, is the owner around? My name's Jay — I run BookerMove, a Richmond-area moving-lead service. I want to send you one moving lead free as a test. No setup, no contract, no signup. If you book the job, great — next lead's $20 to $50, you pay only when one closes. Want me to send the next inbound quote in your ZIP your way?"

## Best email (substitute `{{business}}`)

```
Subject: One free moving lead for {{business}}

Hi {{business}} team,

I run BookerMove — I route inbound moving quotes from customers in
Richmond, Chesterfield, and Henrico to local movers like you.

I'd like to send you ONE moving lead free, no contract, no setup fee.
If it's a real fit, future leads are $20-$50 each, pay-as-you-go.
You cancel anytime.

If that sounds good, reply YES and I'll send the next inbound quote
in your service area straight to this email.

Public intake page: https://quote.bookermove.com/quote

— Jay
```

## Best text-style message (DO NOT SEND)

> Hi {{business}} — Jay w/ BookerMove (Richmond moving leads). Want one free moving lead, no contract? If it converts, $20-$50 per lead after. Reply YES and I'll send the next one. quote.bookermove.com/quote

This text is generated and stored under `outreach_message.sms_text_do_not_send`
in every queue record. Per the contract, no SMS is sent — Jay sends by
hand if he chooses, after a YES.

---

## Best capture URL

```
https://quote.bookermove.com/quote?source=marko&campaign=richmond_movers
```

Verification (test client, this run): `200, 6189 bytes, public form
markers present, operator markers absent`.

The `source` and `campaign` query params don't currently persist into
the lead record (out of scope per S1 — "preserved if supported").
They live in URL analytics only. Attribution-into-lead is a 4-line
follow-up when you want it.

---

## Next 30-minute action plan for Jay

1. **Read this file.** (3 min)
2. **Check `missed_money.json` and your `MARKO_OWNER_NOTIFY_TO` inbox** for any overnight inbound. (2 min)
3. **Dial Moxie Movers, 804-928-1111.** Use the phone script verbatim. (5 min)
4. **If YES from Moxie:** add `M003` to `MARKO_MOVER_ALLOWLIST` in Vercel env, set `MARKO_QUOTE_LIVE_SEND=1`, redeploy. That's the minimum two-env-var unlock to start live mover delivery — only after a real YES. (5 min)
5. **Post the capture URL** in 1 RVA Facebook neighborhood group + 1 Craigslist Richmond `services > skilled trades` post. Zero spend, real demand probe. (5 min)
6. **Dial Mr Moving LLC, 804-986-9480** while waiting on responses. (5 min)
7. **Open `https://quote.bookermove.com/__diag`** to confirm `resolved_host: "quote.bookermove.com"` and `is_public_intake_host: true`. (1 min)

Anything beyond minute 30 depends on call outcomes — keep working down
the call list (#3 Mitchell's, #4 Mighty Moves, #5 Rent The Help).

---

## Blockers

- **9 of 25 mover targets.** Closing the gap = one Google Maps sweep for the four uncovered submarkets. Suggested queries (each returns ~5–10 listings; pull name + phone + email + website + city, append to `leads.json` with `niche: "movers"`):
  - "movers Petersburg VA"
  - "movers Hopewell VA"
  - "movers Mechanicsville VA"
  - "movers Glen Allen VA"
  - "movers Ashland VA"
  - "movers Colonial Heights VA"

  Bonus: Petersburg and Hopewell are the two ZIPs currently surfacing as `no_match` in `routing_ready.json`, so the same sweep also closes a real demand-side coverage gap. Re-run `python marko_overnight.py` after appending — the queue regenerates automatically.

- **`source`/`campaign` URL params not persisted into lead.** Acceptable per S1. When you want it: hidden inputs in `templates/quote.html` + prefix into `lead["notes"]` in `routing.build_lead`. ~4 lines.

- **`MARKO_QUOTE_LIVE_SEND` stays off until a real mover says YES.** This is the safety belt that prevents an overnight "did MARKO email a real business?" panic. Confirmed: nothing went out.

---

## Verification proof

- `overnight_money_queue.json` written: 9 targets, schema 1.0.0, `policy.auto_send: false`, every record has `business_name`, `phone`, `email`, `website`, `city`, `weakness_signal`, `why_they_might_buy`, `recommended_offer`, `outreach_message{phone_script,email,sms_text_do_not_send}`, `call_priority`, `score`, `capture_url`
- `/quote?source=marko&campaign=richmond_movers` test-client → 200, 6189 bytes, `public_markers_missing: []`, `operator_markers_leaked: []`
- `quote.bookermove.com/` → 302 → `/quote` (still passing 9/9 in `_truth/n_public_intake_redirect_verify.py`)
- `movers.json` untouched — 7 entries, no buyer added or modified
- No git ops performed
