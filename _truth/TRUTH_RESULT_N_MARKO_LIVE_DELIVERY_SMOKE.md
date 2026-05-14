# TRUTH_RESULT — N-MARKO-LIVE-DELIVERY-SMOKE

N: N-MARKO-LIVE-DELIVERY-SMOKE

Date: 2026-05-14

## Final Status

PASS

Mode: redirect-only · M001 allowlisted · live_redirected.

## Evidence

| Field            | Value                                              |
| ---------------- | -------------------------------------------------- |
| lead_id          | Q-8864ea80                                         |
| message_id       | 7e8961dd-6c85-40e6-a381-4c0c22133981               |
| provider         | resend                                             |
| status           | sent                                               |
| delivery_mode    | live_redirected                                    |
| redirected       | true                                               |
| to_original      | info@allmysons.com                                 |
| to               | supportbookermove@gmail.com                        |
| from             | support@bookermove.com                             |
| dry_run          | false                                              |
| requested_live   | true                                               |
| provider_error   | null                                               |
| block_reasons    | null                                               |
| at               | 2026-05-14T17:03:16Z                               |
| Physical inbox   | confirmed (Inbox, not Spam, formatting readable)   |

## Commands

| Command                                                                          | Result                                       |
| -------------------------------------------------------------------------------- | -------------------------------------------- |
| `python verify_resend_env.py`                                                    | preflight gates surface missing env honestly |
| `python smoke_live_delivery.py` (Jay's terminal, env inline, key not in transcript) | status=sent, delivery_mode=live_redirected   |
| `python _truth/n_inbound_routing_verify.py`                                      | 10/10 green                                  |
| Flask test client `/admin/delivery` + `/admin/delivery_smoke`                    | 401 / 200 / 409 contract holds               |

## What this PASS proves

1. Resend API key wired and works end-to-end from `routing.route_lead`.
2. `support@bookermove.com` is a Resend-verified sender on a domain with deliverable reputation (Gmail Inbox placement on first send).
3. Allowlist gate fires correctly: only `M001` eligible.
4. Redirect gate fires correctly: `to_original` preserved separately from `to`; `redirected=true` on disk.
5. No silent fallback to dry_run — `dry_run=false`, `requested_live=true`, `status=sent`, all consistent.
6. `delivery_log.json` carries full structured evidence per send (provider, message_id, mode, both recipients, sender, timestamp, error/block fields).
7. Spam-safe email format works: plain subject, no AI/marketing copy, no tracker tags — Inbox on first send.

## Standing safeties (preserved, unchanged)

- `MARKO_QUOTE_LIVE_SEND` remains UNSET on Vercel and locally — public `/quote` POSTs still dry_run.
- `MARKO_MOVER_ALLOWLIST` constrained to `M001` only; every other mover is dry_run even if env is flipped.
- `MARKO_SMOKE_REDIRECT_TO` enforced: only `supportbookermove@gmail.com` receives smoke sends.
- No live email has been sent to any real third-party mover.
- No deploy was made as part of this N.
- Vercel `RESEND_API_KEY` remains Sensitive; `vercel env pull` correctly returns empty value for it.

## Artifacts on disk

- `delivery_log.json` — entry `Q-8864ea80` with 16 evidence fields
- `routed_leads.json` — corresponding `status: routed`, `delivery_mode: live_redirected`
- `inbound_leads.json` — corresponding smoke-labeled inbound record
- `movers.json` — 7 real Richmond movers (registry unchanged this N)
- `routing.py` — adds allowlist gate, redirect, structured delivery logging, `smoke_send`
- `dashboard.py` — adds `/admin/delivery` panel + `/admin/delivery_smoke` route
- `smoke_live_delivery.py` — one-shot live runner with strict preflight + hard fail
- `verify_resend_env.py` — env readiness preflight (no send)
- `_truth/n_inbound_routing_verify.py` — 10-assertion verifier including "no fake success" gate
- This file: archived TRUTH result

## Cost

- tokens: not exposed by local runner
- runtime: about 1 hour, including failed paste attempts and key-rotation discussion
- cost_$: 1 real Resend send (one credit). No deploy. No paid integrations added.

## Stop point

Held at user instruction. Do not continue autonomous work. Next N
(N-MARKO-TALKBOT-INTEGRATION) is proposed but NOT started; awaits
explicit Jay approval before execution.
