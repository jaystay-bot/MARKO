"""MARKO market-brain layer (N262).

Pure, deterministic intelligence that sits on top of the existing
score_lead / pain_points_from_html / estimate_missed_money stack and
answers the operator's real question:

    "Out of every lead I have, who do I call FIRST and WHY?"

Read-only. No file writes. No network. No mutation of inputs.
make_money_today() reads leads.json via commands.call_queue (which is
already the read path used by the dashboard) -- everything else is a
pure function over a single lead dict.

Isolated from active lanes: this module is new; nothing else imports
it. Dashboard wiring is intentionally deferred to a later N#.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import commands
import marko_intel


# ---------- Tunables (deterministic, no env / no config file) ----------

# Niches where missing an intake call ~= immediately losing the job.
# Substring-matched against lead.niche (matches the convention used in
# marko_intel._niche_lookup).
HIGH_URGENCY_NICHES = (
    "mover", "moving",
    "roofer", "roofing",
    "hvac",
    "plumber", "plumbing",
    "towing",
    "locksmith",
    "garage door",
)

# Best-angle catalogue. Ordered by pitch power -- first match wins.
# Each entry: (predicate, angle_key, hook).
# Predicates receive (lead, pain_set) where pain_set is a lowercased set.
_ANGLE_CATALOG = [
    (lambda l, p: any("site error" in t for t in p) or "empty page" in p,
     "OPERATIONAL_ALARM",
     "your site is broken right now -- visitors bounce before they see anything"),
    (lambda l, p: "no online booking" in p and _is_high_urgency(l.get("niche")),
     "AFTER_HOURS_LEAK",
     "no way to book or quote online, and your niche runs on after-hours calls"),
    (lambda l, p: "no contact form" in p and bool(l.get("phone")),
     "PHONE_ONLY_BOTTLENECK",
     "phone-only intake -- anyone landing at 11pm just bounces"),
    (lambda l, p: "no SSL" in p,
     "TRUST_EMBARRASSMENT",
     "Chrome is flagging your site as 'not secure' -- that kills conversions"),
    (lambda l, p: "weak mobile" in p,
     "MOBILE_BOUNCE",
     "site isn't really set up for phones -- that's where most local searches come from"),
    (lambda l, p: "no online booking" in p,
     "INTAKE_FRICTION",
     "no online booking or quote path -- forces every prospect to call"),
    (lambda l, p: any("copyright" in t for t in p),
     "STALE_PRESENCE",
     "site looks stale -- usually means the owner is busy operating, not marketing"),
    (lambda l, p: "no social presence" in p,
     "DISCOVERABILITY",
     "no social footprint -- you're invisible to a chunk of local search"),
]

# Action playbook per fastest_close_path verdict.
_ACTION_PLAYBOOK = {
    "CALL FIRST": {
        "action": "Dial the number on the call queue today",
        "by_when": "today, business hours",
        "reason_default": "phone present + high-urgency niche + strong score",
    },
    "EMAIL FIRST": {
        "action": "Send personalized intro email referencing the strongest pain tag",
        "by_when": "today, before EOD",
        "reason_default": "email + named owner + multiple weakness signals",
    },
    "SEND MOCKUP FIRST": {
        "action": "Generate a one-screen mockup, attach to a short email",
        "by_when": "within 24h",
        "reason_default": "visual pain (mobile / booking) + email channel available",
    },
    "FOLLOW-UP LATER": {
        "action": "Park in follow-up queue, retry in 7-14 days",
        "by_when": "+7 days",
        "reason_default": "weak signal set or recently contacted -- don't burn the lead",
    },
}

# Closability bands.
CLOSABILITY_HOT = 0.70   # call today
CLOSABILITY_WARM = 0.55  # in the daily top 5 if not enough HOT
CLOSABILITY_COLD = 0.30  # below: park

# Follow-up cooldown: don't re-pitch within this window.
FOLLOWUP_COOLDOWN_HOURS = 48


# ---------- Helpers ----------

def _pain_list(lead):
    p = lead.get("pain_points") or []
    if isinstance(p, str):
        p = [p]
    return list(p)


def _is_high_urgency(niche):
    if not niche:
        return False
    n = str(niche).lower()
    return any(key in n for key in HIGH_URGENCY_NICHES)


def _ensure_score(lead):
    """Return the lead's score, computing it via commands.score_lead if absent."""
    s = lead.get("_score")
    if s is not None:
        return s, lead.get("_label")
    out = commands.score_lead(lead)
    return out["score"], out["label"]


# ---------- §5: Closability score ----------

def closability_score(lead):
    """Single 0.0-1.0 number for 'how likely is this lead to close fast?'.

    Weighting (per the N261 spec):
      0.4 * normalized lead score
      0.3 * pain-signal density (capped at 3 tags)
      0.2 * niche urgency
      0.1 * named owner
    """
    score, _ = _ensure_score(lead)
    pain = _pain_list(lead)
    urgency = _is_high_urgency(lead.get("niche"))
    owner = bool(lead.get("owner"))

    raw = (
        0.4 * (score / 100.0)
        + 0.3 * min(len(pain) / 3.0, 1.0)
        + 0.2 * (1.0 if urgency else 0.0)
        + 0.1 * (1.0 if owner else 0.0)
    )
    return round(max(0.0, min(1.0, raw)), 3)


# ---------- §7: Opportunity engine (pure per-lead) ----------

def opportunity_size(lead):
    """Wrap marko_intel.estimate_missed_money with a horizon tag.

    Output: {low, high, confidence, horizon}.
    Horizon is always '30d' (the underlying ranges are monthly). Exists
    as an explicit field so callers can later mix in 90d projections
    without changing the shape.
    """
    base = marko_intel.estimate_missed_money(lead)
    return {
        "low": base.get("low"),
        "high": base.get("high"),
        "confidence": base.get("confidence", "low"),
        "horizon": "30d",
        "note": base.get("note", ""),
    }


def best_angle(lead):
    """Pick the single strongest pitch angle for this lead.

    Returns {angle, hook, evidence}. evidence is the pain-tag list that
    triggered the choice -- the operator can sanity-check before pitching.
    """
    pain = _pain_list(lead)
    pain_set = set(pain)
    for predicate, angle, hook in _ANGLE_CATALOG:
        try:
            if predicate(lead, pain_set):
                return {"angle": angle, "hook": hook, "evidence": pain}
        except Exception:
            continue
    # Fallback: niche-only generic angle.
    niche = (lead.get("niche") or "").strip()
    if niche:
        return {
            "angle": "GENERIC_NICHE",
            "hook": f"local {niche} -- standard intake-improvement angle",
            "evidence": [],
        }
    return {
        "angle": "UNKNOWN",
        "hook": "no strong pain signal detected -- needs enrichment first",
        "evidence": [],
    }


def fastest_close_path(lead):
    """Return one of: CALL FIRST | EMAIL FIRST | SEND MOCKUP FIRST | FOLLOW-UP LATER.

    Decision order (most specific first):
      1) Weak signal set or already worked -> FOLLOW-UP LATER
      2) Phone + high-urgency niche + strong score -> CALL FIRST
      3) Email + >=2 pain tags + named owner -> EMAIL FIRST
      4) Visual pain (mobile/booking) + email -> SEND MOCKUP FIRST
      5) Phone fallback -> CALL FIRST
      6) Email fallback -> EMAIL FIRST
      7) Otherwise -> FOLLOW-UP LATER
    """
    score, label = _ensure_score(lead)
    status = (lead.get("status") or "NEW").upper()
    pain = _pain_list(lead)
    has_phone = bool(lead.get("phone"))
    has_email = bool(lead.get("email"))
    has_owner = bool(lead.get("owner"))
    urgent = _is_high_urgency(lead.get("niche"))

    # 1) Park weak / closed leads.
    if label in ("LOW", "DEAD"):
        return "FOLLOW-UP LATER"
    if status in commands.CALL_QUEUE_EXCLUDE:
        return "FOLLOW-UP LATER"
    # CONTACTED but recent: don't re-burn.
    if status == "CONTACTED":
        last = lead.get("last_attempt_at")
        if last:
            try:
                age = datetime.now() - datetime.fromisoformat(last)
                if age < timedelta(hours=FOLLOWUP_COOLDOWN_HOURS):
                    return "FOLLOW-UP LATER"
            except (TypeError, ValueError):
                pass

    # 2) CALL FIRST: phone, urgency, strong score.
    if has_phone and urgent and score >= 70:
        return "CALL FIRST"

    # 3) EMAIL FIRST: well-targeted, owner known.
    if has_email and len(pain) >= 2 and has_owner:
        return "EMAIL FIRST"

    # 4) SEND MOCKUP FIRST: visual pain + reachable by email.
    if has_email and (
        "weak mobile" in pain or "no online booking" in pain
        or "empty page" in pain or "no SSL" in pain
    ):
        return "SEND MOCKUP FIRST"

    # 5-6) Fallbacks based on available channel.
    if has_phone:
        return "CALL FIRST"
    if has_email:
        return "EMAIL FIRST"

    # 7) No channel available.
    return "FOLLOW-UP LATER"


def recommended_first_action(lead):
    """Translate close-path + best-angle into a concrete next action.

    Returns {action, by_when, reason}. Pure; no side effects.
    """
    path = fastest_close_path(lead)
    angle = best_angle(lead)
    play = _ACTION_PLAYBOOK[path]

    # Reason wires the close-path with the angle hook so the operator
    # sees WHY this call/email/mockup, not just WHAT.
    reason = play["reason_default"]
    if angle["angle"] not in ("UNKNOWN", "GENERIC_NICHE"):
        reason = f"{angle['angle'].lower().replace('_', ' ')}: {angle['hook']}"

    return {
        "path": path,
        "action": play["action"],
        "by_when": play["by_when"],
        "reason": reason,
    }


# ---------- §8: Make Money Today ----------

def _why_now(lead, pain, closability):
    """Short, operator-readable reason this lead is on today's list."""
    if any("site error" in t for t in pain) or "empty page" in pain:
        return "empty/broken page -- emergency pitch"
    if _is_high_urgency(lead.get("niche")) and lead.get("phone") \
            and "no online booking" in pain:
        return "after-hours leak + phone-only intake"
    if "weak mobile" in pain and _is_high_urgency(lead.get("niche")):
        return "high-ticket niche with weak mobile"
    if "no online booking" in pain and "weak mobile" in pain:
        return "double intake leak (mobile + booking)"
    if any("copyright" in t for t in pain) and lead.get("owner"):
        return "stale site + owner known"
    if lead.get("status") == "CALLBACK":
        return "callback overdue"
    if closability >= CLOSABILITY_HOT:
        return "strong signal set across the board"
    return "in warm band -- worth a touch"


def _recently_contacted(lead, hours=FOLLOWUP_COOLDOWN_HOURS):
    if (lead.get("status") or "").upper() != "CONTACTED":
        return False
    last = lead.get("last_attempt_at")
    if not last:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(last)) \
            < timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def make_money_today(limit=5, queue_source=None):
    """Top-N leads to work right now, with reasons.

    Reads from commands.call_queue (existing read path) by default. Pass
    queue_source for tests or for an already-filtered queue.

    Returns:
        {
          "as_of": ISO timestamp,
          "limit": int,
          "considered": int,    # how many leads went through the filter
          "top": [
            {id, name, phone, email, niche, city,
             closability, money_range, why_now,
             path, action, hook}
          ],
          "note": str | None,   # set when fewer than `limit` qualify
        }
    """
    if queue_source is None:
        queue = commands.call_queue(limit=50)
    else:
        queue = list(queue_source)

    scored = []
    for lead in queue:
        if _recently_contacted(lead):
            continue
        c = closability_score(lead)
        if c < CLOSABILITY_WARM:
            continue
        pain = _pain_list(lead)
        money = opportunity_size(lead)
        path_info = recommended_first_action(lead)
        angle = best_angle(lead)

        # Boosts (additive, capped so closability stays interpretable).
        boost = 0.0
        if money["confidence"] == "high":
            boost += 0.05
        if path_info["path"] == "CALL FIRST":
            boost += 0.02

        scored.append({
            "id": lead.get("id"),
            "name": lead.get("name"),
            "phone": lead.get("phone"),
            "email": lead.get("email"),
            "niche": lead.get("niche"),
            "city": lead.get("city"),
            "closability": round(min(1.0, c + boost), 3),
            "money_low": money["low"],
            "money_high": money["high"],
            "money_confidence": money["confidence"],
            "money_range": _format_money_range(money),
            "why_now": _why_now(lead, pain, c),
            "path": path_info["path"],
            "action": path_info["action"],
            "hook": angle["hook"],
        })

    scored.sort(
        key=lambda r: (
            r["closability"],
            r["money_high"] or 0,
            1 if r["path"] == "CALL FIRST" else 0,
        ),
        reverse=True,
    )

    top = scored[:limit]
    note = None
    if len(top) < limit:
        note = (f"only {len(top)} leads above warm threshold "
                f"({CLOSABILITY_WARM:.2f}) -- consider enriching pain_points "
                f"or scraping new leads")

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "limit": limit,
        "considered": len(queue),
        "top": top,
        "note": note,
    }


def _format_money_range(money):
    if money["low"] is None or money["high"] is None:
        return None
    if money["low"] == 0 and money["high"] == 0:
        return "$0/mo (no detected leak)"
    return f"${money['low']:,}-${money['high']:,}/mo ({money['confidence']})"


# ---------- N261: Mockup niche normalization ----------
#
# Real lead.niche values are messy ("dog groomer", "med spa", "movers"...).
# Mockup filenames live in templates/mockup/<slug>_<variant>.html with slugs
# like "groomers", "med_spas", "movers". This map is the one place that
# bridges the two so the dashboard + route never need to guess.

_NICHE_TO_MOCKUP_SLUG = {
    "movers": "movers", "mover": "movers", "moving": "movers",
    "roofers": "roofers", "roofer": "roofers", "roofing": "roofers",
    "med spas": "med_spas", "med spa": "med_spas", "medspa": "med_spas",
    "dog groomers": "groomers", "dog groomer": "groomers",
    "groomers": "groomers", "groomer": "groomers", "grooming": "groomers",
    "hair stylists": "salons", "hair stylist": "salons",
    "salon": "salons", "salons": "salons", "hair": "salons",
    "auto shops": "auto_shops", "auto shop": "auto_shops", "auto": "auto_shops",
    "detailing": "detailers", "detailer": "detailers",
    "detailers": "detailers", "detail": "detailers",
    "towing": "towing", "tow": "towing",
    "plumbers": "plumbers", "plumber": "plumbers", "plumbing": "plumbers",
    "hvac": "hvac",
}


def niche_to_mockup_slug(niche):
    """Map a free-text niche to a mockup filename slug, or None.

    Conservative -- only returns a slug when the niche substring-matches
    a known key. Never invents a slug.
    """
    if not niche:
        return None
    nlow = str(niche).strip().lower()
    # Exact match first.
    if nlow in _NICHE_TO_MOCKUP_SLUG:
        return _NICHE_TO_MOCKUP_SLUG[nlow]
    # Substring fallback (handles "Dog Grooming, Pet Food..." etc).
    for key, slug in _NICHE_TO_MOCKUP_SLUG.items():
        if key in nlow:
            return slug
    return None


def best_mockup_variant(niche):
    """Pick booking vs emergency variant for a niche.

    High-urgency niches (movers/roofers/plumbers/HVAC/towing) get the
    emergency variant -- they win deals when a prospect is bleeding right
    now. Slower-paced niches get the booking variant.
    """
    return "emergency" if _is_high_urgency(niche) else "booking"


# ---------- §4 scope: single read-only function for the dashboard lane ----------
#
# The dashboard lane may eventually want to render today's call list.
# This is the only public read entrypoint they should bind to -- it
# returns plain JSON-able dicts and never mutates state. Wire-up is
# intentionally deferred (this lane does not edit dashboard.py).

def today_brief_json(limit=5):
    """Read-only dashboard contract. Same shape as make_money_today()."""
    return make_money_today(limit=limit)


# ---------- Self-tests ----------

if __name__ == "__main__":
    # Synthetic leads exercise every code path without touching real data.
    failures = []

    def expect(name, ok, detail=""):
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("marko_brain self-tests")

    # 1) closability_score: perfect lead approaches 1.0.
    perfect = {
        "name": "Acme Movers", "email": "a@a.com", "phone": "555-1",
        "website": "https://a.com/", "owner": "Pat Smith", "niche": "movers",
        "city": "Richmond", "state": "VA", "campaign_id": "C001",
        "contact_type": "both", "source": "scrape",
        "pain_points": ["no online booking", "weak mobile", "no contact form"],
    }
    c_perfect = closability_score(perfect)
    expect("perfect lead closability >= 0.85", c_perfect >= 0.85,
           f"got {c_perfect}")

    # 2) closability_score: minimal lead near zero.
    minimal = {"name": "x"}
    c_min = closability_score(minimal)
    expect("minimal lead closability < 0.10", c_min < 0.10, f"got {c_min}")

    # 3) fastest_close_path: high-urgency phone lead -> CALL FIRST.
    call_case = dict(perfect)
    p1 = fastest_close_path(call_case)
    expect("urgent + phone + high score -> CALL FIRST",
           p1 == "CALL FIRST", f"got {p1!r}")

    # 4) fastest_close_path: email + owner + multi-pain on low-urgency niche -> EMAIL FIRST.
    email_case = {
        "name": "Spa", "email": "s@s.com", "owner": "Jane Doe",
        "niche": "med spa", "pain_points": ["weak mobile", "no contact form"],
        "status": "NEW",
    }
    p2 = fastest_close_path(email_case)
    expect("email + owner + multi-pain (low-urgency) -> EMAIL FIRST",
           p2 == "EMAIL FIRST", f"got {p2!r}")

    # 5) fastest_close_path: visual pain + email, no owner -> SEND MOCKUP FIRST.
    # Needs enough signal density to clear the LOW-tier parking floor.
    mockup_case = {
        "name": "Shop", "email": "s@s.com",
        "website": "https://shop.example/", "niche": "detailing",
        "city": "Richmond", "state": "VA", "campaign_id": "C001",
        "pain_points": ["weak mobile"], "status": "NEW",
    }
    p3 = fastest_close_path(mockup_case)
    expect("visual pain + email + no owner -> SEND MOCKUP FIRST",
           p3 == "SEND MOCKUP FIRST", f"got {p3!r}")

    # 6) fastest_close_path: weak signal set -> FOLLOW-UP LATER.
    weak_case = {"name": "x", "email": "x@x.com", "status": "NEW"}
    p4 = fastest_close_path(weak_case)
    expect("weak lead -> FOLLOW-UP LATER", p4 == "FOLLOW-UP LATER",
           f"got {p4!r}")

    # 7) best_angle: emergency angle wins when site is broken.
    angle = best_angle({"niche": "movers", "pain_points": ["empty page"]})
    expect("empty page -> OPERATIONAL_ALARM",
           angle["angle"] == "OPERATIONAL_ALARM", f"got {angle}")

    # 8) recommended_first_action: returns all three keys + reason references angle.
    rec = recommended_first_action(perfect)
    expect("recommended_first_action has path/action/by_when/reason",
           all(k in rec for k in ("path", "action", "by_when", "reason"))
           and len(rec["reason"]) > 5, f"got {rec}")

    # 9) opportunity_size: known niche + signals -> non-zero range.
    op = opportunity_size(perfect)
    expect("known niche + 3 pain points -> non-zero opp range",
           op["low"] and op["high"] and op["high"] > op["low"]
           and op["horizon"] == "30d", f"got {op}")

    # 10) make_money_today: handles an empty queue gracefully.
    empty_brief = make_money_today(limit=5, queue_source=[])
    expect("make_money_today on empty queue returns empty top + note",
           empty_brief["top"] == [] and empty_brief["note"]
           and empty_brief["considered"] == 0,
           f"got {empty_brief}")

    # 11) make_money_today: synthetic queue ranks higher closability first.
    # L_MID needs a 3rd pain tag to clear the WARM closability floor (0.55).
    mid_lead = dict(email_case,
                    pain_points=["weak mobile", "no contact form", "no SSL"])
    queue = [
        # Inject _score directly so we don't depend on score_lead internals here.
        dict(perfect, id="L_HOT", _score=85, _label="HOT"),
        dict(weak_case, id="L_WEAK", _score=15, _label="DEAD"),
        dict(mid_lead, id="L_MID", _score=55, _label="GOOD"),
    ]
    brief = make_money_today(limit=3, queue_source=queue)
    ids = [r["id"] for r in brief["top"]]
    expect("brief excludes DEAD lead", "L_WEAK" not in ids, f"got {ids}")
    expect("brief ranks HOT before MID",
           ids.index("L_HOT") < ids.index("L_MID") if "L_HOT" in ids and
           "L_MID" in ids else False, f"got {ids}")

    print(f"\n{len(failures)} failure(s)")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        raise SystemExit(1)

    # Quick demo over real leads.json so the operator can sanity-check the
    # output shape. No assertions on real data (it changes).
    print("\n=== demo: make_money_today over real leads.json ===")
    try:
        live = make_money_today(limit=5)
        print(f"as_of: {live['as_of']}  considered: {live['considered']}  "
              f"top: {len(live['top'])}")
        if live["note"]:
            print(f"note: {live['note']}")
        for row in live["top"]:
            money = row["money_range"] or "n/a"
            print(f"  [{row['closability']:.2f}] {row['name']!r}  "
                  f"({row['niche']}) -> {row['path']}")
            print(f"     phone={row['phone'] or '-'}  money={money}")
            print(f"     why : {row['why_now']}")
            print(f"     hook: {row['hook']}")
    except Exception as exc:
        print(f"(demo skipped: {exc})")
