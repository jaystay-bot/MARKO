"""MARKO overnight money queue generator (N-MARKO-OVERNIGHT-MONEY-MODE).

Reads ONLY public/legal data already on disk:
  leads.json                  scraped public mover websites (niche=movers)
  movers.json                 buyer registry (real service-area coverage)
  hot_zips.json               derived public-signal demand zones

Writes:
  overnight_money_queue.json  ranked outreach targets with ready copy

No HTTP, no SMS, no live email, no scrape. Pure derive from existing
files. The outreach copy in each record is for Jay to send by hand --
this script never delivers anything.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
MOVERS_FILE = os.path.join(BASE_DIR, "movers.json")
HOT_ZIPS_FILE = os.path.join(BASE_DIR, "hot_zips.json")
QUEUE_FILE = os.path.join(BASE_DIR, "overnight_money_queue.json")

DEFAULT_CAPTURE = (
    "https://quote.bookermove.com/quote?source=marko&campaign=richmond_movers"
)

# Markers of "owner-operated, real local business" -- the buyer profile
# most likely to value a single first lead. Aggregators and chains buy
# leads differently (procurement, RFP) and are deprioritized.
GMAIL_LIKE_DOMAINS = ("gmail.com", "yahoo.com", "hotmail.com", "aol.com")
AGGREGATOR_KEYWORDS = ("hireahelper", "compare", "marketplace", "directory")
CHAIN_KEYWORDS = ("allmysons", "collegehunks", "two men", "minimoves",
                  "atlas", "mayflower", "united van", "north american")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path, default):
    try:
        return storage.read_json(path)
    except FileNotFoundError:
        return default


_GENERIC_NAMES = {
    "movers", "moving", "moving services", "moving company",
    "commercial & residential moving services",
    "richmond movers", "certified movers",
}


def _name_from_domain(website):
    """Pull a readable name from a website domain when the scraped page
    title is too generic. moxiemovers.com -> 'Moxie Movers'.
    """
    if not website:
        return ""
    host = website.lower()
    for prefix in ("https://", "http://", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/", 1)[0].split(".", 1)[0]
    # Best-effort split: camel/multi-word domains aren't directly delimited,
    # so a simple title-case is what we ship.
    return host.replace("-", " ").title()


def _short_name(raw, website=None):
    """Trim noisy scraped page titles to a clean business name guess."""
    if raw:
        # Prefer the "by X" fragment when the title leads with a generic
        # category description: "Commercial & Residential Moving Services
        # in ... by Mitchells Movers" -> "Mitchells Movers".
        if " by " in raw.lower():
            tail = raw.split(" by ", 1)[1].strip().rstrip(".")
            if tail and tail.lower() not in _GENERIC_NAMES:
                return tail
        s = raw
        for sep in ("|", " - ", " – ", " in ", ","):
            if sep in s:
                s = s.split(sep, 1)[0]
        s = s.strip().replace("–", "-")
        if s.lower() not in _GENERIC_NAMES and len(s) >= 5:
            return s
    return _name_from_domain(website) or (raw or "").strip()


def _domain(email):
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower()


def _looks_owner_operated(lead):
    email = (lead.get("email") or "").lower()
    name = (lead.get("name") or "").lower()
    domain = _domain(email)
    phone = (lead.get("phone") or "")
    if any(k in name or k in email for k in CHAIN_KEYWORDS):
        return False
    if any(k in name or k in email for k in AGGREGATOR_KEYWORDS):
        return False
    if domain in GMAIL_LIKE_DOMAINS:
        return True
    # Local-number heuristic: 804 = Richmond MSA. 800/833/888 = chain ops.
    if phone.replace("-", "").replace(" ", "").replace("(", "").replace(")", "").startswith("804"):
        return True
    return False


def _is_aggregator(lead):
    blob = " ".join([
        (lead.get("name") or ""), (lead.get("email") or ""),
        (lead.get("website") or ""),
    ]).lower()
    return any(k in blob for k in AGGREGATOR_KEYWORDS)


def _is_chain(lead):
    blob = " ".join([
        (lead.get("name") or ""), (lead.get("website") or ""),
        (lead.get("email") or ""),
    ]).lower()
    return any(k in blob for k in CHAIN_KEYWORDS)


def _weakness_signal(lead):
    pains = lead.get("pain_points") or []
    bits = []
    for p in pains:
        p_lc = (p or "").lower()
        if "copyright 20" in p_lc:
            bits.append(f"stale website ({p})")
        elif "no online booking" in p_lc:
            bits.append("no online booking")
        elif "no contact form" in p_lc:
            bits.append("no inquiry form on site")
        else:
            bits.append(p)
    if not bits:
        bits.append("no obvious lead-capture surface on public site")
    return "; ".join(bits)


def _why_might_buy(lead, owner_op, chain, aggregator):
    if aggregator:
        return ("aggregator -- buys leads programmatically; small operator "
                "framing won't land. Keep low priority.")
    if chain:
        return ("franchise/chain location -- decisions are gated by corporate "
                "procurement. Worth a phone touch but not the first call.")
    if owner_op:
        return ("owner-operated small local mover -- a single warm lead is "
                "visible revenue today; decision-maker likely answers the "
                "phone directly.")
    return ("local mover with a public phone+email -- decision speed unknown "
            "but no procurement layer visible.")


def _recommended_offer():
    return ("Send one moving lead free. If useful, $20-$50 per lead after that "
            "-- pay only on accepted leads, cancel anytime.")


def _phone_script(business, first_name=None):
    you = first_name or "the owner"
    return (
        f"Hi, is {you} around? "
        f"My name's Jay -- I run BookerMove, a Richmond-area "
        f"moving-lead service. I want to send {business} one moving lead "
        f"free as a test. No setup, no contract, no signup. "
        f"If you book the job, great -- next lead's $20 to $50, you pay "
        f"only when one closes. "
        f"Want me to send the next inbound quote in your ZIP your way?"
    )


def _email_body(business):
    return (
        f"Subject: One free moving lead for {business}\n\n"
        f"Hi {business} team,\n\n"
        f"I run BookerMove -- I route inbound moving quotes from "
        f"customers in Richmond, Chesterfield, and Henrico to local "
        f"movers like you.\n\n"
        f"I'd like to send you ONE moving lead free, no contract, no "
        f"setup fee. If it's a real fit, future leads are $20-$50 each, "
        f"pay-as-you-go. You cancel anytime.\n\n"
        f"If that sounds good, reply YES and I'll send the next inbound "
        f"quote in your service area straight to this email.\n\n"
        f"Public intake page: https://quote.bookermove.com/quote\n\n"
        f"-- Jay\n"
    )


def _sms_text(business):
    return (
        f"Hi {business} -- Jay w/ BookerMove (Richmond moving leads). "
        f"Want one free moving lead, no contract? If it converts, $20-$50 "
        f"per lead after. Reply YES and I'll send the next one. "
        f"quote.bookermove.com/quote"
    )


def _score(lead):
    """Higher = call first. Components mirror the S1 priority list.

    Returns the integer score. For per-component visibility (used by the
    money report), call _score_breakdown(lead) instead.
    """
    return _score_breakdown(lead)["score"]


def _score_breakdown(lead):
    """Same scoring as _score, but returns the per-component breakdown.

    The breakdown lets the operator panel show *why* a mover ranks where
    it does, not just the final number. Components were chosen to match
    the N-SLEEP-MONEY-ENGINE contract:

      weak_capture        no online booking / no contact form (web)
      stale_site          stale copyright = neglected web presence
      owner_operated      gmail-like address or 804 local number
      missed_call_risk    small op + no automation = phone tag pain
      mobile_unknown      site mobile-responsiveness not yet probed
      aggregator/chain    negative weights (procurement-gated buyers)
      reachable           has email / phone

    The mobile component is intentionally a small *unknown penalty* until
    we actually probe each site. Treating "we haven't checked" as zero
    would silently inflate scores -- this surfaces the gap.
    """
    parts = {}
    if _is_aggregator(lead):
        parts["aggregator"] = -50
        return {"score": -50, "parts": parts}

    pains_lc = " ".join((lead.get("pain_points") or [])).lower()
    weak = 0
    if "no online booking" in pains_lc: weak += 25
    if "no contact form" in pains_lc:   weak += 15
    if weak:
        parts["weak_capture"] = weak

    if "copyright 20" in pains_lc:
        parts["stale_site"] = 15

    if _looks_owner_operated(lead):
        parts["owner_operated"] = 25
        # Owner-operated + weak capture = high missed-call likelihood:
        # the owner is on jobs, the phone rings into voicemail, leads
        # bleed. This is exactly the pain BookerMove sells against.
        if weak >= 25:
            parts["missed_call_risk"] = 15

    if _is_chain(lead):
        parts["chain"] = -20

    if (lead.get("email") or ""):
        parts["reachable_email"] = 10
    if (lead.get("phone") or ""):
        parts["reachable_phone"] = 5

    # Mobile responsiveness is unknown for now (no headless probe in V1).
    # Small unknown penalty so the score is honest about what we haven't
    # checked. Becomes a positive signal the moment we add a real probe.
    if not lead.get("mobile_responsive_checked"):
        parts["mobile_unknown_penalty"] = -5

    score = sum(parts.values())
    return {"score": score, "parts": parts}


def _call_priority(score):
    if score >= 50: return "call_today"
    if score >= 20: return "warm"
    return "low"


def _capture_url(mover_id=None):
    base = DEFAULT_CAPTURE
    if mover_id:
        return f"{base}&mover_hint={mover_id}"
    return base


def _movers_by_lead_id():
    movers = _load(MOVERS_FILE, {"movers": []}).get("movers", [])
    return {m.get("source_lead_id"): m for m in movers if m.get("source_lead_id")}


def build_queue():
    leads_doc = _load(LEADS_FILE, {"leads": []})
    leads = [l for l in leads_doc.get("leads", [])
             if (l.get("niche") or "").lower() == "movers"]
    movers_lookup = _movers_by_lead_id()
    hot = _load(HOT_ZIPS_FILE, {"hot_zips": []}).get("hot_zips", [])
    hot_zip_set = {h.get("zip") for h in hot}

    rows = []
    for l in leads:
        owner_op = _looks_owner_operated(l)
        chain = _is_chain(l)
        agg = _is_aggregator(l)
        mover = movers_lookup.get(l.get("id"))
        cities = (mover or {}).get("cities_served") or [l.get("city") or ""]
        zips = (mover or {}).get("zip_codes") or []
        zips_in_hot = [z for z in zips if z in hot_zip_set]
        breakdown = _score_breakdown(l)
        score = breakdown["score"]
        score_parts = dict(breakdown["parts"])
        # Bonus if this mover already covers a hot ZIP -- they're the obvious
        # match for live demand right now.
        if zips_in_hot:
            score += 10
            score_parts["covers_hot_zip"] = 10
        business = _short_name(l.get("name") or "", l.get("website"))
        record = {
            "lead_id": l.get("id"),
            "mover_id": (mover or {}).get("mover_id"),
            "business_name": business,
            "phone": l.get("phone") or "",
            "email": l.get("email") or "",
            "website": l.get("website") or "",
            "city": l.get("city") or "",
            "zip": (zips_in_hot[0] if zips_in_hot
                    else (zips[0] if zips else "")),
            "service_zips_in_hot_demand": zips_in_hot,
            "service_areas": cities,
            "why_they_might_buy": _why_might_buy(l, owner_op, chain, agg),
            "weakness_signal": _weakness_signal(l),
            "lead_angle": (
                "Local-mover demand-capture: customers submit a quote at "
                "quote.bookermove.com and we hand it to one mover at a time. "
                "First lead free -- you only pay for accepted leads after."
            ),
            "recommended_offer": _recommended_offer(),
            "outreach_message": {
                "phone_script": _phone_script(business),
                "email": _email_body(business),
                "sms_text_do_not_send": _sms_text(business),
            },
            "call_priority": _call_priority(score),
            "score": score,
            "score_parts": score_parts,
            "expected_value": "$20-$50 first paid lead test",
            "capture_url": _capture_url((mover or {}).get("mover_id")),
            "operator_notes": {
                "owner_operated_signal": owner_op,
                "chain": chain,
                "aggregator": agg,
                "pain_points_observed": l.get("pain_points") or [],
            },
        }
        rows.append(record)

    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    return rows


def write_queue():
    rows = build_queue()
    payload = {
        "schema_version": "1.0.0",
        "exported_at": _now_iso(),
        "source": "marko",
        "kind": "overnight_money_queue",
        "target_count": len(rows),
        "capture_url_base": DEFAULT_CAPTURE,
        "policy": {
            "auto_send": False,
            "sms_sent": False,
            "live_email_sent": False,
            "data_sources_public_only": True,
        },
        "targets": rows,
    }
    storage.write_json(QUEUE_FILE, payload)
    return payload


if __name__ == "__main__":
    payload = write_queue()
    print(json.dumps({
        "target_count": payload["target_count"],
        "call_today": sum(1 for r in payload["targets"]
                          if r["call_priority"] == "call_today"),
        "warm": sum(1 for r in payload["targets"]
                    if r["call_priority"] == "warm"),
        "low": sum(1 for r in payload["targets"]
                   if r["call_priority"] == "low"),
        "file": os.path.relpath(QUEUE_FILE, BASE_DIR),
    }, indent=2))
