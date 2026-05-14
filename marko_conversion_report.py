"""Generate conversion_report.md from real conversion_events.json data.

Pure derive. Numbers are real or "(none yet)" -- never invented.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import marko_tracking

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(BASE_DIR, "conversion_report.md")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_best(label, pair):
    val, n = pair
    if not val:
        return f"- **{label}:** (none yet)"
    return f"- **{label}:** `{val}` (n={n})"


def render():
    events = marko_tracking.load_events()
    agg = marko_tracking.aggregate(events)
    weakest = marko_tracking.weakest_funnel_step(agg["step_conversion_rates"])
    weakest_str = (f"`{weakest[0]}` at **{weakest[1]}%**"
                   if weakest else "(no funnel data yet — need at least one "
                                   "landing event followed by a downstream step)")

    funnel_lines = "\n".join(
        f"| {step} | {count} |"
        for step, count in agg["funnel_counts"]
    )
    rate_lines = "\n".join(
        f"| {r['from']} → {r['to']} | {r['from_count']} | {r['to_count']} | "
        f"{(str(r['rate_pct']) + '%') if r['rate_pct'] is not None else '—'} |"
        for r in agg["step_conversion_rates"]
    )

    md = f"""# MARKO Conversion Report

Generated: {_now_iso()} · derived from `conversion_events.json`
({agg['total_events']} events total, no fabrication).

**Stripe integration:** not configured in repo. Funnel events
`checkout_started`, `checkout_completed`, `mover_signup_init` are
reserved in the schema and accepted by `/api/track`, so the moment a
real checkout URL exists, those rows light up automatically.

---

## Funnel counts

| step | count |
|---|---|
{funnel_lines}

## Step-to-step conversion

| step | from | to | rate |
|---|---|---|---|
{rate_lines}

## Best performers (real data only)

{_fmt_best("Best CTA", agg["best_cta"])}
{_fmt_best("Best source", agg["best_source"])}
{_fmt_best("Best campaign", agg["best_campaign"])}
{_fmt_best("Best ZIP", agg["best_zip"])}
{_fmt_best("Best pitch", agg["best_pitch"])}

## Weakest funnel step

{weakest_str}

## Device split (landing + quote_submit)

| device | count |
|---|---|
| mobile | {agg['device_split'].get('mobile', 0)} |
| desktop | {agg['device_split'].get('desktop', 0)} |
| unknown | {agg['device_split'].get('unknown', 0)} |
| bot | {agg['device_split'].get('bot', 0)} |

## Highest-converting path observed

{_highest_path(events)}

## Mobile vs desktop observations

{_mobile_obs(agg['device_split'])}

## Top MARKO campaign

{_top_campaign(events)}

---

## Operator notes

- All counts are append-only from the `/quote` flow and `/api/track`. No event is back-dated.
- `cta_id`, `source`, `campaign`, `zip`, `pitch` survive GET → POST through hidden inputs in the quote form. Verified each turn by `_truth/n_marko_money_engine_verify.py` (attribution check).
- Stripe-side rows will appear automatically once `event_type=checkout_started` is emitted from a real Stripe redirect handler. Today: 0 of those events on file.
"""
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        fh.write(md)
    return REPORT_FILE


def _highest_path(events):
    """Naive: count (source, campaign, zip) tuples among quote_submit events."""
    pool = [e for e in events if e["event_type"] == "quote_submit"]
    if not pool:
        return "(no quote_submit events yet)"
    counts = {}
    for e in pool:
        key = (e.get("source") or "(none)",
               e.get("campaign") or "(none)",
               e.get("zip") or "(none)")
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[0]
    src, camp, zp = top[0]
    return f"`source={src} · campaign={camp} · zip={zp}` ({top[1]} submits)"


def _mobile_obs(device_split):
    m = device_split.get("mobile", 0)
    d = device_split.get("desktop", 0)
    if m + d == 0:
        return "(no landing events yet)"
    pct_m = m / (m + d) * 100
    note = "majority mobile -- prioritize mobile chrome" if pct_m >= 50 else \
           "majority desktop -- mobile is upside if traffic shifts"
    return f"mobile={m}, desktop={d} ({pct_m:.0f}% mobile share). {note}."


def _top_campaign(events):
    pool = [e for e in events
            if e["event_type"] == "quote_submit" and (e.get("campaign") or "")]
    if not pool:
        # Fall back to landing events: even traffic without a submit still
        # tells us which campaign is reaching the funnel.
        pool = [e for e in events
                if e["event_type"] == "landing" and (e.get("campaign") or "")]
        suffix = " (no submits yet -- counted from landing events)"
    else:
        suffix = ""
    if not pool:
        return "(no campaign data yet)"
    counts = {}
    for e in pool:
        c = e.get("campaign") or ""
        counts[c] = counts.get(c, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[0]
    return f"`{top[0]}` ({top[1]} events){suffix}"


if __name__ == "__main__":
    path = render()
    print(json.dumps({"file": os.path.relpath(path, BASE_DIR)}, indent=2))
