"""MARKO Internet Leak Engine — Phase 1 (N001).

Takes (city, niche) and produces, per real local business:
  full-page + mobile screenshot,
  leak score 0-100 with per-component breakdown,
  list of major leaks with public evidence,
  suggested fixes per leak,
  suggested $-band offer,
  outreach draft (subject + body) referencing the actual scan,
  heuristic estimated lost-lead band,
  confidence score 0-100.

Usage:
  python marko_leak_engine.py --city "Richmond" --niche "movers"

Discovery source for Phase 1: leads.json filtered by city + niche
substring (real businesses, public info already on file). Phase 2
adds web sources (DuckDuckGo HTML, OSM Overpass). The discovery
function is the only thing that needs to change to swap sources.

Scope (S1 LOCKED): scanner, leak score, screenshots, report, outreach
draft. No CRM, no auto-send, no paid APIs, no fake data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import storage
import niche_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
REPORTS_DIR = os.path.join(BASE_DIR, "leak_reports")

# ---------- Discovery ----------------------------------------------------

def _slug(s, max_len=60):
    s = (s or "").lower()
    s = re.sub(r"https?://", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:max_len] or "x"


def _load_leads():
    try:
        return storage.read_json(LEADS_FILE).get("leads", [])
    except FileNotFoundError:
        return []


def discover_businesses(city: str, niche: str) -> List[Dict[str, Any]]:
    """Discovery: filter leads.json by city + niche.

    Niche matching is alias-aware via niche_config: 'pet groomer'
    resolves to 'dog_groomer' rows recorded in leads.json, etc.
    Falls back to literal substring match if the niche isn't in the
    config (so a future caller can pass anything without breaking).
    City is a case-insensitive substring match.
    """
    city_lc = (city or "").strip().lower()
    niche_lc = (niche or "").strip().lower()

    canonical_key, cfg = niche_config.niche_or_default(niche_lc)
    aliases = tuple(a.lower() for a in (cfg.get("aliases") or ()))

    def _matches_niche(value):
        v = (value or "").lower()
        if not niche_lc:
            return True
        if not v:
            return False
        if niche_lc in v:
            return True
        return any(a in v or v in a for a in aliases)

    out = []
    for l in _load_leads():
        if not _matches_niche(l.get("niche")):
            continue
        if city_lc and city_lc not in (l.get("city") or "").lower():
            continue
        out.append(l)
    return out


def businesses_from_targets(target_urls):
    """Operator-driven discovery: a list of URLs Jay pasted in.

    No niche resolution -- caller already chose the niche by where this
    list is being used. Each URL becomes a synthetic 'business' record
    the rest of the pipeline can process unchanged.
    """
    out = []
    for raw in (target_urls or []):
        url = (raw or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = url.split("//", 1)[1].split("/", 1)[0]
        out.append({
            "id": "T_" + _slug(host),
            "name": host,
            "business_name": host,
            "city": "",
            "website": url,
            "source": "operator_target_list",
        })
    return out


# ---------- Live scan ----------------------------------------------------

@dataclass
class Scan:
    url: str
    final_url: str
    status_code: Optional[int]
    elapsed_s: float
    has_form: bool = False
    has_input_email: bool = False
    has_input_tel: bool = False
    has_button_book_or_quote: bool = False
    has_viewport_meta: bool = False
    has_mobile_viewport: bool = False
    title: str = ""
    copyright_year: Optional[int] = None
    visible_phone: Optional[str] = None
    visible_email: Optional[str] = None
    social_links: Dict[str, str] = field(default_factory=dict)
    cta_text_samples: List[str] = field(default_factory=list)
    desktop_screenshot: Optional[str] = None
    mobile_screenshot: Optional[str] = None
    has_short_form_video_embed: bool = False
    has_trust_block: bool = False
    has_chat_widget: bool = False
    error: Optional[str] = None


SOCIAL_HOSTS = {
    "facebook":  ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "twitter":   ("twitter.com", "x.com"),
    "linkedin":  ("linkedin.com",),
    "youtube":   ("youtube.com", "youtu.be"),
    "tiktok":    ("tiktok.com",),
    "google":    ("g.page", "maps.app.goo.gl", "business.google.com"),
}

CTA_KEYWORDS = (
    "book", "booking", "schedule", "quote", "estimate", "request",
    "get a quote", "free estimate", "contact us", "call now",
)

VIDEO_EMBED_HOSTS = (
    "youtube.com/embed", "youtu.be/", "player.vimeo.com",
    "tiktok.com/embed", "wistia.net", "loom.com",
)

TRUST_KEYWORDS = (
    "testimonial", "review", "rated", "★", "5-star", "five star",
    "google reviews", "yelp", "bbb", "accredited", "certified",
)

CHAT_WIDGET_HINTS = (
    "intercom", "drift", "tidio", "tawk.to", "tawk_api",
    "fb_customer_chat", "messenger", "crisp.chat", "livechat",
    "hubspot conversations", "olark",
)

COPYRIGHT_RE = re.compile(r"copyright[^0-9]*([12][0-9]{3})", re.I)
PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _classify_social(href):
    if not href:
        return None
    href_lc = href.lower()
    for net, hosts in SOCIAL_HOSTS.items():
        if any(h in href_lc for h in hosts):
            return net
    return None


def scan_website(url: str, out_dir: str, timeout_ms: int = 30000) -> Scan:
    """Real headless Chromium scan. Saves two PNGs (desktop + mobile).

    Returns Scan with all observed signals. Never raises -- failures
    land in `error` so the report can show the failure honestly.
    """
    from playwright.sync_api import sync_playwright

    desktop_path = os.path.join(out_dir, "screenshot_desktop.png")
    mobile_path = os.path.join(out_dir, "screenshot_mobile.png")
    scan = Scan(url=url, final_url=url, status_code=None, elapsed_s=0.0)

    if not url:
        scan.error = "no website url"
        return scan
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
        scan.url = url
        scan.final_url = url

    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                # ---- Desktop pass: full-page screenshot + DOM signals
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36 MARKO-Leak-Audit/1.0"
                    ),
                )
                page = ctx.new_page()
                resp = page.goto(url, timeout=timeout_ms,
                                 wait_until="domcontentloaded")
                scan.status_code = resp.status if resp else None
                scan.final_url = page.url
                page.wait_for_timeout(800)  # let above-the-fold settle
                page.screenshot(path=desktop_path, full_page=True)
                scan.desktop_screenshot = os.path.relpath(desktop_path, BASE_DIR)
                _extract_dom_signals(page, scan)
                ctx.close()

                # ---- Mobile pass: above-the-fold viewport screenshot
                ctx_m = browser.new_context(
                    viewport={"width": 375, "height": 812},
                    device_scale_factor=2.0,
                    is_mobile=True,
                    has_touch=True,
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.0 Mobile/15E148 Safari/604.1 "
                        "MARKO-Leak-Audit/1.0"
                    ),
                )
                page_m = ctx_m.new_page()
                page_m.goto(url, timeout=timeout_ms,
                            wait_until="domcontentloaded")
                page_m.wait_for_timeout(600)
                page_m.screenshot(path=mobile_path, full_page=False)
                scan.mobile_screenshot = os.path.relpath(mobile_path, BASE_DIR)
                # mobile viewport meta detected on this pass too
                vp = page_m.locator('meta[name="viewport"]').count()
                if vp:
                    scan.has_viewport_meta = True
                    content = (page_m.locator('meta[name="viewport"]').first
                               .get_attribute("content") or "").lower()
                    if "width=device-width" in content:
                        scan.has_mobile_viewport = True
                ctx_m.close()
            finally:
                browser.close()
    except Exception as exc:
        scan.error = f"{type(exc).__name__}: {exc}"
    scan.elapsed_s = round(time.time() - t0, 2)
    return scan


def _extract_dom_signals(page, scan):
    """Pull form/CTA/social/contact signals from the rendered DOM."""
    try:
        scan.title = (page.title() or "").strip()
    except Exception:
        pass
    try:
        scan.has_form = page.locator("form").count() > 0
        scan.has_input_email = page.locator('input[type="email"]').count() > 0
        scan.has_input_tel = page.locator('input[type="tel"]').count() > 0
    except Exception:
        pass
    try:
        # CTA candidates: a/button text containing booking-ish keywords
        texts = []
        # cap iterations so a runaway page doesn't burn time
        for sel in ("button", "a"):
            els = page.locator(sel)
            n = min(els.count(), 200)
            for i in range(n):
                try:
                    t = (els.nth(i).inner_text(timeout=500) or "").strip()
                except Exception:
                    continue
                if not t or len(t) > 80:
                    continue
                t_lc = t.lower()
                if any(k in t_lc for k in CTA_KEYWORDS):
                    texts.append(t)
                    if any(k in t_lc for k in ("book", "quote", "estimate", "schedule")):
                        scan.has_button_book_or_quote = True
        # de-dupe but keep order
        seen = set()
        scan.cta_text_samples = [t for t in texts
                                 if not (t in seen or seen.add(t))][:6]
    except Exception:
        pass
    try:
        body = (page.content() or "")
    except Exception:
        body = ""
    # copyright year
    m = COPYRIGHT_RE.search(body)
    if m:
        try:
            scan.copyright_year = int(m.group(1))
        except ValueError:
            pass
    # visible phone/email (first real-looking match)
    pm = PHONE_RE.search(body)
    if pm:
        scan.visible_phone = pm.group(1)
    em = EMAIL_RE.search(body)
    if em:
        # ignore the obvious noise (sentry/google fonts emails embedded in
        # third-party JS payloads)
        addr = em.group(0)
        if not any(addr.endswith(s) for s in (".png", ".jpg", ".gif")):
            scan.visible_email = addr
    # social links
    try:
        anchors = page.locator("a")
        n = min(anchors.count(), 400)
        for i in range(n):
            try:
                href = anchors.nth(i).get_attribute("href", timeout=300)
            except Exception:
                continue
            net = _classify_social(href)
            if net and net not in scan.social_links:
                scan.social_links[net] = href
    except Exception:
        pass
    # short-form video embed signal: any <iframe src=*video-host*>
    body_lc = body.lower()
    if any(h in body_lc for h in VIDEO_EMBED_HOSTS):
        scan.has_short_form_video_embed = True
    # trust block: keyword hit in homepage HTML (loose; surfaced in
    # report so the operator can verify)
    if any(k in body_lc for k in TRUST_KEYWORDS):
        scan.has_trust_block = True
    # chat widget heuristic from third-party script src or container ids
    if any(h in body_lc for h in CHAT_WIDGET_HINTS):
        scan.has_chat_widget = True


# ---------- Leak detection + scoring ------------------------------------

LEAK_DEFS = {
    "no_quote_form": {
        "label": "No quote/contact form",
        "weight": 25,
        "fix": (
            "Add one short quote form on the homepage: name, phone OR "
            "email, ZIP, move date. 4 fields max. Wire it to your "
            "existing inbox -- no CRM needed."
        ),
    },
    "no_booking_path": {
        "label": "No booking or scheduling path",
        "weight": 15,
        "fix": (
            "Add a 'Book a free estimate' button above the fold linking "
            "to the form. Even a Calendly link beats a dead phone number."
        ),
    },
    "weak_cta": {
        "label": "No clear primary call-to-action",
        "weight": 10,
        "fix": (
            "Pick ONE primary action ('Get a free quote' or 'Call now') "
            "and place a button for it above the fold on every page."
        ),
    },
    "stale_copyright": {
        "label": "Stale copyright year in footer",
        "weight": 10,
        "fix": (
            "Update footer copyright to the current year. Visitors read "
            "an old year as 'are they still in business?'"
        ),
    },
    "no_mobile_viewport": {
        "label": "No mobile viewport meta -- iPhone visitors zoom-pinch",
        "weight": 15,
        "fix": (
            "Add `<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\">` to the <head>. One line."
        ),
    },
    "no_social_links": {
        "label": "No public social links",
        "weight": 5,
        "fix": (
            "Add at least your Google Business Profile + one of FB/IG. "
            "Buyers cross-check. Empty social = looks abandoned."
        ),
    },
    "no_visible_contact": {
        "label": "No phone or email above the fold",
        "weight": 15,
        "fix": (
            "Put your phone number in the top-right of the header AND "
            "in the footer. Tap-to-call link on mobile."
        ),
    },
    "missing_trust_signals": {
        "label": "No reviews, ratings, or trust badges visible",
        "weight": 10,
        "fix": (
            "Embed your Google Business reviews widget or a 3-line "
            "testimonial block above the fold."
        ),
    },
    "no_short_form_content": {
        "label": "No short-form video embed (Reels / TikTok / Shorts)",
        "weight": 10,
        "fix": (
            "Embed one 15-30s Reel on the homepage. Doesn't have to be "
            "polished -- one phone-shot before/after beats no video."
        ),
    },
    "outdated_branding": {
        "label": "Site looks unmaintained (stale year + thin signal mix)",
        "weight": 10,
        "fix": (
            "Refresh footer year, swap one stock photo for a real "
            "team/job photo, push one new post or testimonial. The "
            "feel of 'maintained' beats the polish of 'redesigned'."
        ),
    },
    "poor_response_flow": {
        "label": "No fast-response surface (no form, no chat, no tap-to-call)",
        "weight": 15,
        "fix": (
            "Pick exactly one: an embedded chat widget, a sticky "
            "tap-to-call bar on mobile, or a 4-field quote form. Don't "
            "do all three -- pick one and make it loud."
        ),
    },
    "dead_or_missing_socials": {
        "label": "No social links visible (cannot tell if dead vs missing without scraping)",
        "weight": 5,
        "fix": (
            "Add at minimum a Google Business Profile link + one of "
            "FB/IG to the footer. Cross-checking buyers see 'no socials' "
            "and assume abandoned."
        ),
    },
    "scan_failed": {
        "label": "Site failed to load in our scan",
        "weight": 30,
        "fix": (
            "If real visitors hit the same error, every lead is lost. "
            "Check uptime + DNS + SSL. Get a free check at uptimerobot.com."
        ),
    },
}


def detect_leaks(scan: Scan) -> List[Dict[str, Any]]:
    """Real leaks observed from the live scan. No hallucination."""
    out = []

    if scan.error or scan.status_code is None or scan.status_code >= 500:
        out.append({
            "category": "scan_failed",
            "evidence": (
                f"site failed to load: {scan.error or f'HTTP {scan.status_code}'}"
            ),
            "severity": "high",
        })
        # When the scan itself failed there's nothing further to observe.
        return out

    if not scan.has_form and not scan.has_button_book_or_quote:
        out.append({
            "category": "no_quote_form",
            "evidence": "no <form> tag and no booking/quote button found in DOM",
            "severity": "high",
        })
    elif not scan.has_form:
        out.append({
            "category": "no_quote_form",
            "evidence": "no <form> tag in DOM (button-only path missing form fallback)",
            "severity": "medium",
        })

    if not scan.has_button_book_or_quote:
        out.append({
            "category": "no_booking_path",
            "evidence": "no button/link with text 'book', 'quote', 'estimate', or 'schedule'",
            "severity": "medium",
        })

    if not scan.cta_text_samples:
        out.append({
            "category": "weak_cta",
            "evidence": "no CTA-style anchor or button found above the fold",
            "severity": "medium",
        })

    if scan.copyright_year and scan.copyright_year < datetime.now(timezone.utc).year - 1:
        out.append({
            "category": "stale_copyright",
            "evidence": f"footer shows copyright {scan.copyright_year}",
            "severity": "low",
        })

    if not scan.has_mobile_viewport:
        out.append({
            "category": "no_mobile_viewport",
            "evidence": (
                "no <meta name=viewport content=width=device-width> -- "
                "iPhone visitors will zoom-pinch"
            ),
            "severity": "high",
        })

    if not scan.social_links:
        out.append({
            "category": "dead_or_missing_socials",
            "evidence": (
                "no social links visible in homepage DOM (cannot "
                "distinguish dead-account from missing-link without "
                "scraping the social network)"
            ),
            "severity": "low",
        })

    if not scan.visible_phone and not scan.visible_email:
        out.append({
            "category": "no_visible_contact",
            "evidence": "no phone number or email visible in homepage HTML",
            "severity": "high",
        })

    if not scan.has_short_form_video_embed:
        out.append({
            "category": "no_short_form_content",
            "evidence": (
                "no embedded short-form video (no YouTube/Vimeo/TikTok "
                "iframe found in homepage)"
            ),
            "severity": "low",
        })

    if not scan.has_trust_block:
        out.append({
            "category": "missing_trust_signals",
            "evidence": (
                "no review/testimonial/rating keywords detected on "
                "homepage HTML"
            ),
            "severity": "medium",
        })

    # outdated_branding fires when the copyright is meaningfully stale
    # AND the site has thin signal mix (no socials AND no trust block).
    # Both conditions = the site reads abandoned, not just dated.
    cur_year = datetime.now(timezone.utc).year
    if (scan.copyright_year and scan.copyright_year < cur_year - 1
            and not scan.social_links and not scan.has_trust_block):
        out.append({
            "category": "outdated_branding",
            "evidence": (
                f"footer copyright {scan.copyright_year}, no socials, "
                "no review/testimonial signals -- composite reads as "
                "unmaintained"
            ),
            "severity": "medium",
        })

    # poor_response_flow: zero fast-response surfaces of any kind
    if (not scan.has_form
            and not scan.has_chat_widget
            and not scan.has_input_tel
            and not scan.visible_phone):
        out.append({
            "category": "poor_response_flow",
            "evidence": (
                "no form, no chat widget, no tap-to-call link, no "
                "visible phone -- there is no fast path for an inbound "
                "lead to reach the business"
            ),
            "severity": "high",
        })

    # De-duplicate: if both missing_trust_signals (from absent keywords)
    # and outdated_branding fire, keep both -- they pay different weights.
    # If both no_quote_form and poor_response_flow fire, that's also OK;
    # weights are designed so they stack honestly.

    return out


def leak_score(leaks, niche_key=None) -> int:
    """0-100 heuristic. Deterministic: sum of (per-niche-overridable)
    weights of observed leaks, capped at 100.
    """
    return score_breakdown(leaks, niche_key=niche_key)["score"]


def score_breakdown(leaks, niche_key=None):
    """Per-leak contribution + final score. Used by report generator
    so the operator can explain *why* the number landed where it did.
    """
    overrides = {}
    if niche_key and niche_key in niche_config.NICHES:
        overrides = niche_config.NICHES[niche_key].get(
            "weight_overrides") or {}
    parts = []
    total = 0
    for l in leaks:
        cat = l.get("category")
        if cat not in LEAK_DEFS:
            continue
        base = LEAK_DEFS[cat]["weight"]
        applied = overrides.get(cat, base)
        parts.append({
            "category": cat,
            "label": LEAK_DEFS[cat]["label"],
            "severity": l.get("severity"),
            "base_weight": base,
            "applied_weight": applied,
            "niche_override": (cat in overrides),
        })
        total += applied
    return {
        "score": max(0, min(100, total)),
        "raw_total": total,
        "capped_at_100": total > 100,
        "parts": parts,
        "niche_key_applied": niche_key,
    }


def confidence_score(scan: Scan, leaks) -> int:
    """0-100. High when scan succeeded and we observed many ground-truth
    signals; low when scan failed or signals were thin.
    """
    if scan.error or scan.status_code is None:
        return 20
    base = 40
    if scan.has_viewport_meta is not None: base += 10
    if scan.title:                          base += 5
    if scan.cta_text_samples:               base += 5
    if scan.visible_phone or scan.visible_email: base += 10
    if scan.social_links:                   base += 5
    if scan.desktop_screenshot:             base += 10
    if scan.mobile_screenshot:              base += 5
    return max(0, min(100, base))


# ---------- Heuristic lost-lead band -------------------------------------

def estimated_lost_leads_band(score: int, niche: str) -> Dict[str, Any]:
    """Heuristic only -- contract requires this label.

    Ranges are coarse priors per month for a small local-service biz.
    No claim of accuracy; the label and `heuristic: true` flag make this
    explicit so a downstream consumer can never accidentally treat this
    as a forecast.
    """
    # Coarse baselines (leads/month) by niche family
    baseline = 30 if "mover" in (niche or "").lower() else 25
    if score >= 80:
        low, high = int(baseline * 0.30), int(baseline * 0.60)
    elif score >= 60:
        low, high = int(baseline * 0.20), int(baseline * 0.40)
    elif score >= 40:
        low, high = int(baseline * 0.10), int(baseline * 0.25)
    elif score >= 20:
        low, high = int(baseline * 0.05), int(baseline * 0.15)
    else:
        low, high = 0, int(baseline * 0.05)
    return {
        "heuristic": True,
        "leads_per_month_low": low,
        "leads_per_month_high": high,
        "basis": (
            f"baseline {baseline} leads/mo for niche '{niche}'; "
            f"loss share scaled by leak_score {score}/100. "
            "Not a forecast -- coarse prior."
        ),
    }


# ---------- Suggested offer + outreach draft -----------------------------

def suggested_offer(score: int, niche_key=None) -> str:
    """$-band aligned to leak severity, layered with a niche-aware
    canonical offer line. No fake numbers, no faked precision.
    """
    canonical = ""
    if niche_key and niche_key in niche_config.NICHES:
        canonical = niche_config.NICHES[niche_key].get("service_offer") or ""
    if score >= 70:
        sprint = (
            "$300-$500 one-time site-fix sprint: top 3 leaks closed "
            "in 48h (form / mobile viewport / tap-to-call / trust block). "
        )
        return sprint + ("Or " + canonical if canonical else
                         "Or $50/lead pay-as-you-go via BookerMove.")
    if score >= 40:
        sprint = "$150-$300 quick-win pass: top 2 leaks fixed in <48h. "
        return sprint + ("Or " + canonical if canonical else
                         "Or $20-$50/lead via BookerMove.")
    return canonical or (
        "$20-$50 single lead test through BookerMove. Site is mostly "
        "fine -- the gain is volume, not fixes."
    )


# ---------- Mini commercial concept (text-only; no media gen) ------------

def mini_commercial(business, scan, leaks, niche_key) -> Dict[str, Any]:
    """One paragraph, leak-aware, niche-themed video concept.

    Text only. The contract for this N is explicit: no Remotion, no
    rendering, no media pipeline. The output is what Jay (or the
    business owner) would brief a phone-shot 15-second Reel against.
    """
    name = business.get("business_name") or business.get("name") or "the business"
    cfg = niche_config.NICHES.get(niche_key) or niche_config.niche_or_default(niche_key)[1]
    themes = list(cfg.get("commercial_themes") or ())
    if not themes:
        themes = ["clear opening shot", "before/after of the actual service",
                  "one specific call-to-action overlay"]

    # Lead the concept with whichever leak hurts most -- the video
    # should *show* the fix, not just the brand.
    primary = max(leaks, key=lambda l: LEAK_DEFS.get(
        l.get("category"), {"weight": 0})["weight"]) if leaks else None
    leak_hook = ""
    if primary:
        cat = primary.get("category")
        if cat in ("no_visible_contact", "poor_response_flow", "no_quote_form"):
            leak_hook = (
                "Open with a phone vibrating ONCE on a counter, then "
                "auto-replied -- the visual proof that '{name}' answers "
                "even after hours."
            )
        elif cat in ("no_booking_path",):
            leak_hook = (
                "Open with a thumb tapping a single 'Book' button and "
                "a confirmation flashing within 3 seconds."
            )
        elif cat in ("missing_trust_signals", "outdated_branding"):
            leak_hook = (
                "Open with one real customer's quote on screen for 2 "
                "seconds, then cut to the actual person saying it."
            )
        elif cat == "no_short_form_content":
            leak_hook = (
                "Open with a 3-shot before/middle/after grid that "
                "compresses one full job into 4 seconds."
            )
        elif cat == "dead_or_missing_socials":
            leak_hook = (
                "Open with a hand tapping the @-handle on screen, "
                "showing a fresh post timestamp."
            )
    if not leak_hook:
        leak_hook = (
            "Open with one true 5-second moment of the service "
            "happening -- no logo card, no music swell, just the work."
        )

    beats = themes[:4]
    end_card = (
        "Close on a single CTA overlay -- the same one that's missing "
        "from the website today."
    )
    paragraph = (
        f"{leak_hook.format(name=name)} Then in 10-12 seconds, cut "
        f"between: {', '.join(beats)}. {end_card} Vertical 9:16, phone "
        f"camera, no music, no on-screen narration -- one text-overlay "
        f"per beat max."
    )
    return {
        "format": "vertical 9:16, ~15 seconds, phone-shot",
        "leak_driver": (primary.get("category") if primary else None),
        "concept": paragraph,
        "beats": beats,
        "production_notes": (
            "no music required; one text overlay per beat; one CTA at "
            "end; brand logo only in last 1 second"
        ),
    }


# ---------- Loom scripts (text-only; no recording infra) -----------------

def _primary_leak(leaks):
    if not leaks:
        return None
    return max(leaks, key=lambda l: LEAK_DEFS.get(
        l.get("category"), {"weight": 0})["weight"])


def loom_30s_script(business, scan, leaks, niche_key, lost):
    """5-7 sentence script for a 30-second Loom. Hook -> proof ->
    cost -> offer. Designed to be read directly into Loom while the
    operator's screen shows the audit report.
    """
    name = business.get("business_name") or business.get("name") or "your team"
    site = scan.final_url or business.get("website") or "your site"
    primary = _primary_leak(leaks)
    if primary:
        leak_label = LEAK_DEFS[primary["category"]]["label"].lower()
        evidence = primary["evidence"]
    else:
        leak_label = "the basics check out"
        evidence = "your site loaded fine and the form is wired"
    band_low = lost.get("leads_per_month_low", 0)
    band_high = lost.get("leads_per_month_high", 0)
    return {
        "duration_seconds": 30,
        "leak_driver": (primary.get("category") if primary else None),
        "shot_list": (
            "Open Loom, share screen showing the audit report scrolled "
            "to the screenshots section. Camera bubble bottom-right."
        ),
        "script": (
            f"Hey {name} -- Jay here from BookerMove in Richmond. "
            f"I ran a 60-second audit on {site} this morning and want "
            f"to show you one thing. {evidence.capitalize()}. "
            f"That's {leak_label} -- it's the kind of leak that "
            f"costs roughly {band_low} to {band_high} leads a month "
            "for a shop your size, totally heuristic but you'll know "
            "if that feels right. "
            "I'd send you one moving lead from your service area free "
            "as a test, no contract, $20-$50 per lead after if it's a "
            "real fit. Reply to this Loom or text me back -- takes "
            "30 seconds either way."
        ),
    }


def loom_90s_script(business, scan, leaks, niche_key, lost, offer):
    """8-12 sentences. Walks the scan: opens the page, shows the leak,
    shows the suggested fix, ends on the offer. Designed for cold-email
    follow-up after an unanswered Loom 30.
    """
    name = business.get("business_name") or business.get("name") or "your team"
    site = scan.final_url or business.get("website") or "your site"
    primary = _primary_leak(leaks)
    leak_label = (LEAK_DEFS[primary["category"]]["label"]
                  if primary else "no major leak")
    evidence = (primary["evidence"] if primary
                else "site checks the basics")
    fix = (LEAK_DEFS[primary["category"]]["fix"]
           if primary else "keep doing what you're doing")
    band_low = lost.get("leads_per_month_low", 0)
    band_high = lost.get("leads_per_month_high", 0)
    socials_present = bool(scan.social_links)
    socials_line = (
        f"You've got socials on the page -- {', '.join(scan.social_links.keys())} -- "
        "so you're already doing the visibility work."
        if socials_present else
        "I also didn't see any social links on the homepage; that's a "
        "small fix but it changes how new buyers cross-check you."
    )
    return {
        "duration_seconds": 90,
        "leak_driver": (primary.get("category") if primary else None),
        "shot_list": (
            "Loom screen-record. Beat 1: open the homepage live, "
            "scroll to the leak. Beat 2: switch tab to the audit "
            "report's Top 3 leaks block. Beat 3: switch to the "
            "Suggested Fixes section. Beat 4: end on the offer line."
        ),
        "script": (
            f"Hey {name} -- Jay from BookerMove. Quick 90-second "
            f"walkthrough of what I found on {site}. "
            f"Top thing first: {leak_label.lower()}. {evidence.capitalize()}. "
            f"{socials_line} "
            f"Heuristic estimate of what that's costing you -- and I "
            f"want to flag this is a coarse number, not a forecast -- "
            f"is in the {band_low}-to-{band_high} leads-per-month range. "
            f"Here's the fix in one line: {fix} "
            f"My offer: {offer} "
            "If any of that lands, just reply to the email this came "
            "in. I'll send the next inbound moving quote in your "
            "service area straight to whichever address works for you. "
            "Either way, the audit PDF is yours -- no spam after."
        ),
    }


def draft_outreach(business, scan, leaks, score, offer) -> Dict[str, str]:
    """Leak-specific email referencing a real observation from the scan."""
    name = business.get("business_name") or business.get("name") or "your team"
    site = scan.final_url or business.get("website") or "your site"
    # Pick the most expensive observed leak as the pitch driver
    if leaks:
        primary = max(leaks, key=lambda l: LEAK_DEFS.get(
            l["category"], {"weight": 0})["weight"])
    else:
        primary = None

    if primary:
        leak_label = LEAK_DEFS.get(primary["category"], {"label": primary["category"]})["label"]
        body_open = (
            f"Looked at {site}. {primary['evidence'][0].upper()}{primary['evidence'][1:]}. "
            f"That's the kind of leak that costs after-hours leads "
            f"because nothing captures them when you're on a job."
        )
        subject = f"{name} -- {leak_label.lower()} (free 1-page audit)"
    else:
        # No leaks found is rare but possible -- be honest
        body_open = (
            f"Looked at {site}. Site checks the basics, but I run a "
            "moving-lead service in Richmond and I'd rather start with "
            "real demand than tell you to fix nothing."
        )
        subject = f"{name} -- one local moving lead, on me"

    body = "\n\n".join([
        body_open,
        f"Quick offer: {offer}",
        (
            "If a free 1-page audit (this scan with 2 fixes) is useful, "
            "reply YES and I'll send it. No spam after, you keep the PDF."
        ),
        "-- Jay, BookerMove (Richmond, VA)",
    ])
    return {"subject": subject, "body": body}


# ---------- Report generation --------------------------------------------

def _score_band(score):
    if score >= 70: return ("HIGH", "you are leaking customers every single day")
    if score >= 40: return ("MEDIUM", "you are leaving real money on the table")
    if score >= 20: return ("LOW",    "small leaks are still leaks")
    return ("MINIMAL", "site checks the basics; the gain is volume, not fixes")


def _top3(leaks):
    """Top 3 leaks by base weight (severity-aware tiebreak)."""
    sev = {"high": 3, "medium": 2, "low": 1}
    ranked = sorted(
        [l for l in leaks if l.get("category") in LEAK_DEFS],
        key=lambda l: (
            LEAK_DEFS[l["category"]]["weight"],
            sev.get(l.get("severity"), 0),
        ),
        reverse=True,
    )
    return ranked[:3]


def _fmt_md(business, scan, leaks, score, conf, offer, lost, draft, fixes,
            niche_key=None, breakdown=None, commercial=None):
    name = business.get("business_name") or business.get("name") or "(no name)"
    band, band_caption = _score_band(score)
    top3 = _top3(leaks)
    top3_block = "\n".join(
        f"{i+1}. **{LEAK_DEFS[l['category']]['label']}** "
        f"({l['severity']}) — {l['evidence']}"
        for i, l in enumerate(top3)
    ) or "_No major leaks observed -- this site is healthier than most._"
    bullets = "\n".join(
        f"- **{LEAK_DEFS.get(l['category'], {'label': l['category']})['label']}** "
        f"({l['severity']}) — {l['evidence']}"
        for l in leaks
    ) or "_No major leaks observed in this scan._"
    fix_lines = "\n".join(f"- **{cat}:** {fix}" for cat, fix in fixes)
    socials = "\n".join(f"- {net}: {href}" for net, href in scan.social_links.items()) \
              or "_None publicly visible on homepage._"

    breakdown_block = ""
    if breakdown and breakdown.get("parts"):
        rows = "\n".join(
            f"| {p['label']} | {p['severity'] or '—'} | {p['base_weight']} | "
            f"{p['applied_weight']}{' (niche)' if p['niche_override'] else ''} |"
            for p in breakdown["parts"]
        )
        breakdown_block = f"""## Score breakdown ({breakdown['score']}/100, niche='{breakdown.get('niche_key_applied') or 'generic'}')

| Leak | Severity | Base weight | Applied |
|---|---|---|---|
{rows}
{('_Total exceeded 100; capped._' if breakdown.get('capped_at_100') else '')}
"""

    commercial_block = ""
    if commercial:
        beats_list = "\n".join(f"  - {b}" for b in commercial.get("beats", []))
        commercial_block = f"""## Mini commercial concept (text-only, ~15s vertical)

> {commercial['concept']}

- **Format:** {commercial['format']}
- **Leak this video addresses:** {commercial.get('leak_driver') or '(generic)'}
- **Beats:**
{beats_list}
- **Production notes:** {commercial['production_notes']}
"""

    return f"""# {name} — leak audit

> **Leak score: {score}/100 ({band})** — {band_caption}.
> Heuristic estimated loss: **{lost['leads_per_month_low']}–{lost['leads_per_month_high']} leads/month**.

**Generated:** {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
**Scan:** live headless Chromium (MARKO Internet Leak Engine v2 / N002)
**Confidence:** {conf}/100 · **Public-data only:** yes (homepage + meta only)
**Niche:** {niche_key or '(generic)'}

---

## Top 3 leaks to fix first
{top3_block}

## Suggested offer
{offer}

## Mini commercial concept
{commercial_block.strip().split('## Mini commercial concept (text-only, ~15s vertical)')[-1] if commercial_block else '_(none generated)_'}

---

## Business
- **Name:** {name}
- **Website:** {scan.final_url or business.get("website") or "(unknown)"}
- **City:** {business.get("city") or "(unknown)"}
- **Phone (from scan):** {scan.visible_phone or business.get("phone") or "—"}
- **Email (from scan):** {scan.visible_email or business.get("email") or "—"}

## Social links (publicly visible)
{socials}

## Major leaks observed (full list)
{bullets}

## Suggested fixes (by leak)
{fix_lines or "_(no fixes needed)_"}

## Estimated lost leads (heuristic only)
- **{lost['leads_per_month_low']}–{lost['leads_per_month_high']} leads/month**
- _Basis:_ {lost['basis']}
- _Heuristic flag:_ `true` (this is a coarse prior, not a forecast)

{breakdown_block}

## Outreach draft (do not send without Jay's approval)
**Subject:** {draft['subject']}

```
{draft['body']}
```

## Screenshots
- desktop: `{scan.desktop_screenshot or '(scan failed)'}`
- mobile (375x812): `{scan.mobile_screenshot or '(scan failed)'}`

## Raw scan
```json
{json.dumps(asdict(scan), indent=2)}
```
"""


def _build_fixes(leaks):
    out = []
    for l in leaks:
        d = LEAK_DEFS.get(l["category"])
        if d:
            out.append((d["label"], d["fix"]))
    return out


# ---------- End-to-end pipeline ------------------------------------------

def run(city: str, niche: str, max_targets: Optional[int] = None,
        run_id: Optional[str] = None,
        targets: Optional[List[str]] = None) -> Dict[str, Any]:
    """End-to-end pipeline.

    Discovery sources (one is used per call):
      * leads.json filter (default; uses city + niche)
      * operator-supplied URL list (`targets=[...]`); city/niche are
        still recorded on the run for folder naming and reporting.
    """
    if targets:
        businesses = businesses_from_targets(targets)
    else:
        businesses = discover_businesses(city, niche)
    if max_targets:
        businesses = businesses[:max_targets]

    niche_key = niche_config.resolve_niche(niche)

    if run_id is None:
        run_id = (
            f"{_slug(city)}_{_slug(niche)}_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
    run_dir = os.path.join(REPORTS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    summary = {
        "run_id": run_id,
        "city": city,
        "niche_input": niche,
        "niche_key_resolved": niche_key,
        "discovery_source": ("operator_targets" if targets else "leads.json"),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "discovered": len(businesses),
        "scanned": 0,
        "scan_failures": 0,
        "reports": [],
    }

    for biz in businesses:
        biz_name = biz.get("business_name") or biz.get("name") or biz.get("id") or "x"
        biz_slug = _slug(biz_name)
        biz_dir = os.path.join(run_dir, biz_slug)
        os.makedirs(biz_dir, exist_ok=True)
        url = biz.get("website") or ""

        scan = scan_website(url, biz_dir)
        leaks = detect_leaks(scan)
        breakdown = score_breakdown(leaks, niche_key=niche_key)
        score = breakdown["score"]
        conf = confidence_score(scan, leaks)
        offer = suggested_offer(score, niche_key=niche_key)
        lost = estimated_lost_leads_band(score, niche)
        fixes = _build_fixes(leaks)
        draft = draft_outreach(biz, scan, leaks, score, offer)
        commercial = mini_commercial(biz, scan, leaks, niche_key)
        loom30 = loom_30s_script(biz, scan, leaks, niche_key, lost)
        loom90 = loom_90s_script(biz, scan, leaks, niche_key, lost, offer)

        report = {
            "business_name": biz_name,
            "website": scan.final_url or url,
            "social_links": scan.social_links,
            "leak_score": score,
            "score_breakdown": breakdown,
            "major_leaks": leaks,
            "top_3_leaks": _top3(leaks),
            "screenshots": {
                "desktop": scan.desktop_screenshot,
                "mobile": scan.mobile_screenshot,
            },
            "suggested_fixes": [
                {"leak": label, "fix": fix} for label, fix in fixes
            ],
            "suggested_offer": offer,
            "outreach_draft": draft,
            "mini_commercial_concept": commercial,
            "loom_30s_script": loom30,
            "loom_90s_script": loom90,
            "estimated_lost_leads": lost,
            "confidence_score": conf,
            "niche": {
                "input": niche,
                "resolved_key": niche_key,
            },
            "scan": asdict(scan),
        }

        with open(os.path.join(biz_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        with open(os.path.join(biz_dir, "report.md"), "w", encoding="utf-8") as fh:
            fh.write(_fmt_md(biz, scan, leaks, score, conf, offer, lost,
                             draft, fixes, niche_key=niche_key,
                             breakdown=breakdown, commercial=commercial))

        summary["scanned"] += 1
        if scan.error or scan.status_code is None:
            summary["scan_failures"] += 1
        summary["reports"].append({
            "business_name": biz_name,
            "leak_score": score,
            "confidence_score": conf,
            "report_dir": os.path.relpath(biz_dir, BASE_DIR),
            "scan_failed": bool(scan.error),
        })

    with open(os.path.join(run_dir, "run_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="MARKO Internet Leak Engine -- v2 (N002)")
    parser.add_argument("--city", required=True, help="e.g. 'Richmond'")
    parser.add_argument("--niche", required=True,
                        help="e.g. 'movers', 'pet groomers', 'roofers'")
    parser.add_argument("--max-targets", type=int, default=None,
                        help="cap number of businesses scanned (test mode)")
    parser.add_argument("--targets", default=None,
                        help=("comma-separated URL list -- operator-driven "
                              "discovery (use when leads.json has no rows "
                              "for this niche yet)"))
    args = parser.parse_args()
    target_list = None
    if args.targets:
        target_list = [t.strip() for t in args.targets.split(",") if t.strip()]
    summary = run(args.city, args.niche, max_targets=args.max_targets,
                  targets=target_list)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
