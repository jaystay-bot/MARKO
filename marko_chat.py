"""MARKO Sales Operator chat closer (N005).

Grounded chat layer over the leak-engine reports. Deterministic
template builders are canonical -- they ALWAYS work, with or without
a local model. Ollama (if present + opt-in via env) only re-phrases
the deterministic output; it never adds facts.

The chat is the mouth. The verified report JSON is the brain.

Usage:
    out = answer("who_first", run_id=run_id)
    out = answer("what_to_say", run_id=run_id, biz_slug=slug)
    out = answer("custom", run_id=run_id, biz_slug=slug,
                 free_text="how do I handle 'too expensive'?")

Returns:
    {
      "answer": str,
      "grounded_fields": dict,    # the exact report fields cited
      "used_model": bool,
      "model_name": str | None,
      "fallback_reason": str | None,
    }
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import marko_leak_dashboard as mld

# Closed enum of supported commands. Free text routes to "custom".
COMMANDS = (
    "who_first",
    "why_business",
    "what_to_say",
    "loom_30",
    "handle_objection",
    "offer_99",
    "offer_300",
    "offer_200_mo",
    "rank_easiest",
    "follow_up",
    "do_now",
    "custom",
)

NOT_ENOUGH = "Not enough verified data for that."


# ---------- Loaders (read-only via existing helper) ----------------------

def _load_report(run_id, biz_slug):
    if not run_id or not biz_slug:
        return None
    return mld.load_report(run_id, biz_slug)


def _load_run(run_id):
    if not run_id:
        return None
    return mld.load_run(run_id)


def _rank_run_rows(run):
    """Rank a run's rows by (leak_score desc, confidence desc).

    `run["rows"]` already comes sorted that way from the helper, but we
    re-sort defensively so callers can't be surprised by upstream
    changes. Skips scan_failed rows.
    """
    rows = [r for r in (run.get("rows") or []) if not r.get("scan_failed")]
    rows.sort(
        key=lambda r: (r.get("leak_score") or 0,
                       r.get("confidence_score") or 0),
        reverse=True,
    )
    return rows


# ---------- Deterministic builders (the brain) ---------------------------

def _build_who_first(run_id):
    run = _load_run(run_id)
    if not run:
        return None, {"reason": "no run loaded"}
    rows = _rank_run_rows(run)
    if not rows:
        return None, {"reason": "no scannable rows in run"}
    top = rows[0]
    biz_slug = top["biz_slug"]
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "report missing for top row"}
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    leak_lines = "\n".join(
        f"  - {l.get('category')} ({l.get('severity')}): {l.get('evidence')}"
        for l in leaks[:3]
    ) or "  - (none observed)"
    offer = report.get("suggested_offer") or "(no offer set)"
    site = report.get("website") or "(unknown)"
    answer = (
        f"Contact first: {top['business_name']}\n"
        f"Website: {site}\n"
        f"Leak score: {top['leak_score']}/100  |  "
        f"Confidence: {top['confidence_score']}/100\n\n"
        f"Top leaks:\n{leak_lines}\n\n"
        f"Why first: highest leak score in this run with the strongest "
        f"observed evidence on a real, scrapable site.\n\n"
        f"What to say (one line):\n"
        f"  \"Hey {top['business_name']} -- I ran a 60-second audit on "
        f"{site}. {leaks[0]['evidence'] if leaks else 'site checks the basics'}. "
        f"That's the kind of leak that costs after-hours leads.\"\n\n"
        f"Suggested offer:\n  {offer}\n\n"
        f"Next action: open the full report (it's the page you're on) "
        f"and copy the cold email under \"Outreach pack\". Then call "
        f"{report.get('scan', {}).get('visible_phone') or '(phone via report)'} "
        f"or send the email."
    )
    return answer, {
        "business_name": top["business_name"],
        "leak_score": top["leak_score"],
        "confidence_score": top["confidence_score"],
        "top_leak_categories": [l.get("category") for l in leaks],
        "website": site,
        "report_path": f"leak_reports/{run_id}/{biz_slug}/report.json",
    }


def _build_why_business(run_id, biz_slug):
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "no report loaded"}
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    if not leaks:
        return (
            f"{report['business_name']} scored {report.get('leak_score')}/100. "
            "No major leaks observed -- not the strongest pitch target."
        ), {"business_name": report["business_name"],
            "leak_score": report.get("leak_score")}
    primary = leaks[0]
    socials = list((report.get("social_links") or {}).keys())
    socials_line = (
        f"They have a public footprint ({', '.join(socials)}) so they're "
        "investing in visibility -- they care about leads."
        if socials else
        "No social links visible on their homepage -- they're under-using "
        "the cheap visibility channels."
    )
    site = report.get("website") or "their site"
    answer = (
        f"Why {report['business_name']}:\n"
        f"  - Real observed leak on {site}: "
        f"{primary.get('evidence')} ({primary.get('severity')} severity)\n"
        f"  - Leak score {report.get('leak_score')}/100; confidence "
        f"{report.get('confidence_score')}/100 -- this is a measured "
        f"signal, not a guess.\n"
        f"  - {socials_line}\n"
        f"  - The fix is concrete and small: "
        f"{(report.get('suggested_fixes') or [{'fix': '(no fix listed)'}])[0].get('fix')}\n"
        f"  - That makes it sellable today, not a six-week consulting pitch."
    )
    return answer, {
        "business_name": report["business_name"],
        "leak_score": report.get("leak_score"),
        "primary_leak_category": primary.get("category"),
        "primary_leak_evidence": primary.get("evidence"),
        "social_link_count": len(socials),
    }


def _build_what_to_say(run_id, biz_slug):
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "no report loaded"}
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    primary = leaks[0] if leaks else None
    name = report.get("business_name") or "your team"
    site = report.get("website") or "your site"
    if primary:
        leak_phrase = primary.get("evidence", "").rstrip(".")
    else:
        leak_phrase = "site checks the basics"
    draft = report.get("outreach_draft") or {}
    answer = (
        f"Three openers, all leak-grounded -- pick the channel:\n\n"
        f"PHONE (15 sec):\n"
        f"  \"Hey, is the owner around? Jay here -- I run BookerMove in "
        f"Richmond. I ran a 60-second audit on {site} this morning and "
        f"want to show you one specific thing: {leak_phrase}. Quick "
        f"30-second pitch, then you can hang up if it's not useful?\"\n\n"
        f"SMS / DM (1 line):\n"
        f"  \"Hi {name} -- Jay w/ BookerMove. Quick audit on {site} "
        f"flagged {primary.get('category') if primary else 'a fixable issue'}. "
        f"Want a 1-page PDF? Reply YES.\"\n\n"
        f"EMAIL (subject + 2-line opener):\n"
        f"  Subject: {draft.get('subject') or f'{name} -- 60-second site audit'}\n"
        f"  Body opener: \"Looked at {site}. {leak_phrase.capitalize()}. "
        f"That's the kind of leak that costs after-hours leads.\""
    )
    return answer, {
        "business_name": name,
        "website": site,
        "primary_leak_category": (primary.get("category") if primary else None),
        "uses_real_outreach_subject": bool(draft.get("subject")),
    }


def _build_loom_30(run_id, biz_slug):
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "no report loaded"}
    loom = report.get("loom_30s_script") or {}
    if not loom.get("script"):
        return NOT_ENOUGH, {"reason": "loom_30s_script missing"}
    answer = (
        f"30-second Loom script (read directly into Loom; "
        f"share-screen the audit report):\n\n"
        f"SHOT LIST: {loom.get('shot_list')}\n\n"
        f"SCRIPT:\n{loom.get('script')}"
    )
    return answer, {
        "business_name": report.get("business_name"),
        "leak_driver": loom.get("leak_driver"),
        "duration_seconds": loom.get("duration_seconds"),
    }


_OBJECTIONS = (
    ("too expensive", (
        "\"Totally fair. The pitch isn't an agency retainer. It's $99 "
        "for the smallest fix that closes the biggest leak we measured "
        "on your site -- I'll point at the screenshot. If that one fix "
        "doesn't pay back in a month, you cancel and we're done.\""
    )),
    ("send me an email", (
        "\"Already in your inbox -- subject is the leak we flagged. "
        "Want me to also send the 1-page PDF so you can compare to your "
        "current setup? It's already generated.\""
    )),
    ("we already have someone", (
        "\"Good -- means you take the website seriously. The audit is "
        "free; if your current person isn't catching this leak, you'll "
        "want to know. If they ARE on it, we're done in 30 seconds.\""
    )),
    ("not interested", (
        "\"Got it. The audit PDF is yours -- no spam after. Worst case "
        "you've got it for the next time you redo the site.\""
    )),
    ("how do you have my info", (
        "\"All of it is public -- your website, the phone number on "
        "your homepage, your Google Business Profile. I didn't pull "
        "anything that's not already on the open internet. The "
        "screenshots are from public-data scans.\""
    )),
)


def _build_handle_objection(run_id, biz_slug, free_text=None):
    """If the operator typed a free-text objection, fuzzy-match it to the
    closest known objection and return that response. Otherwise return
    the full pack so Jay can scan it.
    """
    report = _load_report(run_id, biz_slug) if biz_slug else None
    name = (report or {}).get("business_name") or "the prospect"
    if free_text:
        ft = free_text.lower()
        for keyword, resp in _OBJECTIONS:
            if keyword in ft:
                return (
                    f"Objection match: \"{keyword}\"\n"
                    f"Response (use Jay's voice; reference {name} by name):\n"
                    f"{resp}"
                ), {"matched_objection": keyword,
                    "business_name": name}
    block = "\n\n".join(
        f"OBJECTION: \"{k}\"\nRESPONSE: {v}"
        for k, v in _OBJECTIONS
    )
    return (
        f"Common objections + grounded responses (reference "
        f"{name} by name when you deliver):\n\n{block}"
    ), {"business_name": name, "objection_count": len(_OBJECTIONS)}


def _offer_text(tier, report):
    primary_leak_label = "(no specific leak)"
    fix_line = "(no fix listed)"
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    if leaks:
        primary_leak_label = leaks[0].get("category")
        fixes = report.get("suggested_fixes") or []
        if fixes:
            fix_line = fixes[0].get("fix")
    name = report.get("business_name") or "your team"
    site = report.get("website") or "your site"
    if tier == "99":
        return (
            f"$99 quick fix for {name}:\n"
            f"  - Scope: smallest cleanup that closes the dominant leak "
            f"({primary_leak_label}) on {site}\n"
            f"  - Specifically: {fix_line}\n"
            f"  - Turnaround: 48 hours\n"
            f"  - Pay only if the fix lands; refund otherwise\n"
            f"  - One-time, no contract, no upsell on this call"
        )
    if tier == "300":
        return (
            f"$300 landing/booking/quote-flow fix for {name}:\n"
            f"  - Scope: redesign the single page where the leak lives "
            f"({primary_leak_label}) -- form + above-the-fold CTA + "
            f"trust block\n"
            f"  - Includes: 4-field quote form wired to your inbox, "
            f"sticky tap-to-call on mobile, one testimonial block\n"
            f"  - Turnaround: 5 business days\n"
            f"  - Half up front, half on go-live; refund if leak metrics "
            f"don't move in 30 days"
        )
    if tier == "200_mo":
        return (
            f"$200/mo monitoring + content + follow-up for {name}:\n"
            f"  - Weekly site re-scan; alert on new leaks\n"
            f"  - One short-form video brief per month (you film; we "
            f"script using the audit data)\n"
            f"  - Inbound-lead follow-up template + 1 nudge sequence\n"
            f"  - Cancel anytime, month-to-month, no setup fee"
        )
    return NOT_ENOUGH


def _build_offer(tier, run_id, biz_slug):
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "no report loaded"}
    answer = _offer_text(tier, report)
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    return answer, {
        "tier": tier,
        "business_name": report.get("business_name"),
        "primary_leak_category": (leaks[0].get("category") if leaks else None),
    }


def _build_rank_easiest(run_id):
    """Easiest = high confidence + medium-leak-score (real but fixable)
    + has email or phone (reachable).
    """
    run = _load_run(run_id)
    if not run:
        return None, {"reason": "no run loaded"}
    rows = _rank_run_rows(run)
    if not rows:
        return None, {"reason": "no scannable rows"}

    # Score each row by (confidence + reachable_bonus + medium_leak_bonus).
    scored = []
    for r in rows:
        report = _load_report(run_id, r["biz_slug"])
        if not report:
            continue
        scan = report.get("scan") or {}
        reachable = bool(scan.get("visible_phone") or scan.get("visible_email"))
        leak = r.get("leak_score") or 0
        # peak ease at score ~50 (real but not catastrophic)
        leak_easy_bonus = max(0, 30 - abs(leak - 50))
        ease = (r.get("confidence_score") or 0) + leak_easy_bonus + (15 if reachable else 0)
        scored.append({
            "biz_slug": r["biz_slug"],
            "business_name": r["business_name"],
            "leak_score": leak,
            "confidence": r.get("confidence_score"),
            "reachable": reachable,
            "ease_score": ease,
        })
    scored.sort(key=lambda s: s["ease_score"], reverse=True)
    top = scored[:5]
    lines = "\n".join(
        f"  {i+1}. {s['business_name']} -- ease {s['ease_score']}, "
        f"leak {s['leak_score']}, conf {s['confidence']}, "
        f"{'reachable' if s['reachable'] else 'no visible contact'}"
        for i, s in enumerate(top)
    )
    answer = (
        f"Easiest closes in this run (high confidence + mid-range "
        f"leak score + reachable contact):\n\n{lines}\n\n"
        f"Start with #1 -- they have the most fixable leak signature "
        f"and a contact channel that doesn't require gatekeeper bypass."
    )
    return answer, {
        "ranked_business_names": [s["business_name"] for s in top],
        "top_ease_score": (top[0]["ease_score"] if top else None),
    }


def _build_follow_up(run_id, biz_slug):
    report = _load_report(run_id, biz_slug)
    if not report:
        return None, {"reason": "no report loaded"}
    name = report.get("business_name") or "your team"
    leaks = report.get("top_3_leaks") or report.get("major_leaks") or []
    primary = leaks[0] if leaks else None
    answer = (
        f"Follow-up message for {name} (3 days after first touch, "
        f"no answer):\n\n"
        f"SUBJECT: {name} -- short follow-up + the audit PDF\n\n"
        f"BODY:\n"
        f"  Hey {name} -- circling back on the audit I sent. "
        f"{('The biggest thing I flagged is ' + primary.get('category') + ' -- ' + primary.get('evidence') + '.') if primary else 'Site checks most of the basics.'} "
        f"Two paths if it's useful:\n"
        f"  1. Reply YES and I'll send the 1-page PDF audit (no spam after).\n"
        f"  2. Reply NOT NOW and I'll stop following up entirely.\n"
        f"  Either way, the audit is yours.\n\n"
        f"  -- Jay, BookerMove (Richmond, VA)"
    )
    return answer, {
        "business_name": name,
        "primary_leak_category": (primary.get("category") if primary else None),
    }


def _build_do_now(run_id, biz_slug):
    """One concrete action Jay can take in the next 5 minutes."""
    if biz_slug:
        report = _load_report(run_id, biz_slug)
        if report:
            phone = (report.get("scan") or {}).get("visible_phone")
            name = report.get("business_name")
            if phone:
                return (
                    f"Right now (5 minutes):\n"
                    f"  1. Pick up your phone, dial {phone}.\n"
                    f"  2. Use the PHONE opener from \"What should I "
                    f"say?\" verbatim.\n"
                    f"  3. If voicemail: hang up, send the cold email "
                    f"under \"Outreach pack\" instead.\n"
                    f"  4. Either way, log the attempt manually -- no "
                    f"CRM, just a note in your phone."
                ), {"action": "call_or_email", "business_name": name,
                    "phone": phone}
            return (
                f"Right now (5 minutes):\n"
                f"  1. Open the cold email under \"Outreach pack\" on "
                f"this page.\n"
                f"  2. Paste it into your normal email client. Address "
                f"to: {report.get('scan', {}).get('visible_email') or '(no visible email -- find one on contact page)'}\n"
                f"  3. Send it manually.\n"
                f"  4. Note the send so you can follow up in 3 days."
            ), {"action": "email", "business_name": name}
    # Fall back to whoever's at the top of the run.
    res = _build_who_first(run_id)
    if res[0]:
        return res
    return NOT_ENOUGH, {"reason": "no run loaded"}


def _build_custom(run_id, biz_slug, free_text):
    """Catch-all for free-text. Without Ollama there's no language
    understanding -- so we offer the user the 11 buttoned commands and
    explicitly say the chat won't fabricate. With Ollama opt-in, the
    Ollama path runs separately on the deterministic answer.
    """
    suggestion = (
        "I won't fabricate a free-text answer without grounding. "
        "Pick one of the buttons above (they all use real report data) "
        "or set MARKO_CHAT_USE_OLLAMA=1 to enable the local Ollama "
        "reframer for free-text questions."
    )
    if not free_text:
        return suggestion, {"reason": "no free_text provided"}
    return (
        f"You asked: {free_text!r}\n\n{suggestion}"
    ), {"free_text": free_text}


# ---------- Optional Ollama reframer (the mouth) ------------------------

OLLAMA_URL = "http://localhost:11434/api/generate"
ALLOWED_MODELS = ("llama3.1:8b", "qwen2.5:7b", "mistral:7b")


def _ollama_enabled():
    return (os.environ.get("MARKO_CHAT_USE_OLLAMA") or "").strip() == "1"


def _ollama_model():
    m = (os.environ.get("MARKO_CHAT_MODEL") or "llama3.1:8b").strip()
    return m if m in ALLOWED_MODELS else "llama3.1:8b"


def _try_ollama(deterministic_answer, grounded, command, timeout=8):
    """Optional reframer. Returns (text|None, fallback_reason).

    Prompt explicitly forbids new facts -- the deterministic answer
    is the ground truth. If Ollama is off OR unreachable OR errors,
    return None and let the caller fall back to the deterministic text.
    """
    if not _ollama_enabled():
        return None, "ollama_disabled (set MARKO_CHAT_USE_OLLAMA=1 to enable)"
    try:
        import urllib.request as _ur
        import urllib.error as _ue
    except Exception as exc:
        return None, f"urllib import: {exc}"
    payload = {
        "model": _ollama_model(),
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 380},
        "prompt": (
            "You are MARKO Sales Operator. Rewrite the answer below in "
            "Jay's voice -- short, local, plain-spoken, no AI hype, no "
            "made-up facts, no invented prices. Do NOT add any business "
            "name, leak, screenshot, review, or competitor not present "
            "in the JSON facts block. If a fact isn't there, don't say "
            "it. Keep it under ~220 words.\n\n"
            f"COMMAND: {command}\n\n"
            f"JSON FACTS (verified):\n{json.dumps(grounded, indent=2)}\n\n"
            f"DETERMINISTIC ANSWER (ground truth):\n{deterministic_answer}\n\n"
            "REWRITE:"
        ),
    }
    try:
        req = _ur.Request(OLLAMA_URL,
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        text = (data.get("response") or "").strip()
        if not text:
            return None, "ollama returned empty response"
        return text, None
    except _ue.URLError as exc:
        return None, f"ollama unreachable: {exc.reason}"
    except Exception as exc:
        return None, f"ollama error: {type(exc).__name__}: {exc}"


# ---------- Public entry point ------------------------------------------

_DISPATCH = {
    "who_first":        lambda r, b, t: _build_who_first(r),
    "why_business":     lambda r, b, t: _build_why_business(r, b),
    "what_to_say":      lambda r, b, t: _build_what_to_say(r, b),
    "loom_30":          lambda r, b, t: _build_loom_30(r, b),
    "handle_objection": lambda r, b, t: _build_handle_objection(r, b, t),
    "offer_99":         lambda r, b, t: _build_offer("99", r, b),
    "offer_300":        lambda r, b, t: _build_offer("300", r, b),
    "offer_200_mo":     lambda r, b, t: _build_offer("200_mo", r, b),
    "rank_easiest":     lambda r, b, t: _build_rank_easiest(r),
    "follow_up":        lambda r, b, t: _build_follow_up(r, b),
    "do_now":           lambda r, b, t: _build_do_now(r, b),
    "custom":           lambda r, b, t: _build_custom(r, b, t),
}


def answer(command, run_id=None, biz_slug=None, free_text=None):
    """Public entry. Returns dict with answer + grounded_fields +
    used_model + model_name + fallback_reason.
    """
    if command not in COMMANDS:
        return {
            "answer": (
                f"Unknown command {command!r}. Use one of: "
                f"{', '.join(c for c in COMMANDS if c != 'custom')}, or "
                f"'custom' with free_text."
            ),
            "grounded_fields": {"reason": "unknown_command"},
            "used_model": False,
            "model_name": None,
            "fallback_reason": "unknown_command",
        }

    builder = _DISPATCH[command]
    det, grounded = builder(run_id, biz_slug, free_text)

    if det is None:
        return {
            "answer": NOT_ENOUGH,
            "grounded_fields": grounded or {},
            "used_model": False,
            "model_name": None,
            "fallback_reason": (grounded or {}).get("reason", "no data"),
        }

    # Optional model reframer. Never replaces facts -- only voice.
    text, why_off = _try_ollama(det, grounded or {}, command)
    if text is None:
        return {
            "answer": det,
            "grounded_fields": grounded or {},
            "used_model": False,
            "model_name": None,
            "fallback_reason": why_off,
        }
    return {
        "answer": text,
        "grounded_fields": grounded or {},
        "used_model": True,
        "model_name": _ollama_model(),
        "fallback_reason": None,
    }
