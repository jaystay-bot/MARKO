"""Pure-derive helper for the /leaks dashboard (N004).

Reads `leak_reports/<run_id>/` from disk and exposes the small set of
queries the Flask routes need. No mutation, no scrape, no HTTP. If a
report.json was written by the N002 engine before Loom scripts existed,
this layer backfills the Loom blocks lazily so the dashboard never
shows an empty section.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import marko_leak_engine as mle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = mle.REPORTS_DIR

# Demo presets the N004 contract names explicitly. Each is shown on the
# dashboard whether or not a verified scan exists; if no scan exists
# the preset renders disabled with the contract-required copy.
DEMO_PRESETS = (
    {"city": "Richmond",        "niche": "movers"},
    {"city": "Richmond",        "niche": "roofers"},
    {"city": "Colonial Heights", "niche": "groomers"},
    {"city": "Virginia Beach",  "niche": "med spas"},
)


def _safe_load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _is_run_dir(name):
    """Run dirs look like '<city>_<niche>_<YYYYMMDDTHHMMSSZ>' (N001/N002)
    OR '<city>_<niche>_batch_<...>_jobN' (batch). Either way they
    contain a run_summary.json -- that's the real test.
    """
    if name.startswith("_") or name.startswith("."):
        return False
    if name.startswith("batch_"):
        # batch_<ts>/ holds batch_summary.json, not run_summary.json --
        # individual run dirs live alongside it
        return False
    return True


def list_runs() -> List[Dict[str, Any]]:
    """Every directory under leak_reports/ that has a run_summary.json.

    Sorted newest-first by `started_at`.
    """
    if not os.path.isdir(REPORTS_DIR):
        return []
    out = []
    for name in os.listdir(REPORTS_DIR):
        if not _is_run_dir(name):
            continue
        run_dir = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(run_dir):
            continue
        summary_path = os.path.join(run_dir, "run_summary.json")
        summary = _safe_load_json(summary_path)
        if not summary:
            continue
        out.append({
            "run_id": name,
            "city": summary.get("city") or "",
            "niche_input": summary.get("niche_input") or summary.get("niche") or "",
            "niche_key_resolved": summary.get("niche_key_resolved"),
            "discovery_source": summary.get("discovery_source") or "leads.json",
            "started_at": summary.get("started_at") or "",
            "discovered": summary.get("discovered", 0),
            "scanned": summary.get("scanned", 0),
            "scan_failures": summary.get("scan_failures", 0),
            "report_count": len(summary.get("reports") or []),
        })
    out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return out


def latest_run_for(city: str, niche: str) -> Optional[Dict[str, Any]]:
    """Best (newest) run for a (city, niche) pair, alias-aware via the
    engine's existing canonicalization.
    """
    import niche_config
    target_key = niche_config.resolve_niche(niche)
    city_lc = (city or "").strip().lower()
    candidates = []
    for r in list_runs():
        run_city = (r.get("city") or "").strip().lower()
        if city_lc and run_city != city_lc and city_lc not in run_city:
            continue
        rk = r.get("niche_key_resolved") or niche_config.resolve_niche(
            r.get("niche_input") or "")
        if target_key and rk and rk == target_key:
            candidates.append(r)
            continue
        # Fall back to literal substring on the input string.
        if not target_key and (niche or "").lower() in (
                r.get("niche_input") or "").lower():
            candidates.append(r)
    candidates.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return candidates[0] if candidates else None


def available_presets() -> List[Dict[str, Any]]:
    """For each contract preset, attach the latest matching run (if any)
    so the dashboard renders enabled / disabled accordingly.
    """
    out = []
    for p in DEMO_PRESETS:
        latest = latest_run_for(p["city"], p["niche"])
        out.append({
            "city": p["city"],
            "niche": p["niche"],
            "available": bool(latest),
            "latest_run_id": (latest or {}).get("run_id"),
            "report_count": (latest or {}).get("report_count", 0),
            "started_at": (latest or {}).get("started_at"),
            "no_scan_message": (
                None if latest else "No verified scan yet."
            ),
        })
    return out


def load_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Full run summary + per-business report stub list. Heavy report
    bodies are only loaded by `load_report()`.
    """
    run_dir = os.path.join(REPORTS_DIR, run_id)
    summary = _safe_load_json(os.path.join(run_dir, "run_summary.json"))
    if not summary:
        return None
    rows = []
    for rep in (summary.get("reports") or []):
        rd = rep.get("report_dir")
        # report_dir can be relative to repo root; normalize
        biz_dir = os.path.join(BASE_DIR, rd) if rd else None
        biz_slug = (os.path.basename(biz_dir) if biz_dir else "x")
        report = _safe_load_json(os.path.join(biz_dir, "report.json")) \
            if biz_dir else None
        if not report:
            continue
        rows.append({
            "biz_slug": biz_slug,
            "business_name": report.get("business_name"),
            "website": report.get("website"),
            "leak_score": report.get("leak_score"),
            "confidence_score": report.get("confidence_score"),
            "top_leak_label": _top_leak_label(report),
            "suggested_offer": report.get("suggested_offer"),
            "scan_failed": bool((report.get("scan") or {}).get("error")),
        })
    rows.sort(
        key=lambda r: (r.get("leak_score") or 0,
                       r.get("confidence_score") or 0),
        reverse=True,
    )
    return {
        "run_id": run_id,
        "summary": summary,
        "rows": rows,
    }


def _top_leak_label(report):
    top = (report.get("top_3_leaks") or report.get("major_leaks") or [])
    if not top:
        return "(no major leaks)"
    cat = top[0].get("category")
    return mle.LEAK_DEFS.get(cat, {}).get("label", cat or "?")


def _backfill_loom(report):
    """Old reports written before N004 lack loom_*_script blocks.
    Compute on the fly so the dashboard never renders an empty card.
    The on-disk file is NOT mutated -- this is a read-only fallback.
    """
    if "loom_30s_script" in report and "loom_90s_script" in report:
        return report
    # Reconstruct the inputs the engine would have used.
    # We don't have the original `business` dict; build a minimal one
    # from the report fields the generators actually read.
    biz = {
        "business_name": report.get("business_name"),
        "name": report.get("business_name"),
        "website": report.get("website"),
    }
    scan_dict = report.get("scan") or {}
    # mle.Scan dataclass shape -- pass the fields directly via a small
    # adapter to keep loom_*_script's `.attribute` access working.
    class _ScanAdapter:
        pass
    s = _ScanAdapter()
    for k, v in scan_dict.items():
        setattr(s, k, v)
    # Default any field the generators read but the dict lacks.
    for f in ("final_url", "social_links"):
        if not hasattr(s, f):
            setattr(s, f, "" if f == "final_url" else {})
    leaks = report.get("major_leaks") or []
    nk = (report.get("niche") or {}).get("resolved_key")
    lost = report.get("estimated_lost_leads") or {}
    offer = report.get("suggested_offer") or ""
    if "loom_30s_script" not in report:
        report["loom_30s_script"] = mle.loom_30s_script(biz, s, leaks, nk, lost)
    if "loom_90s_script" not in report:
        report["loom_90s_script"] = mle.loom_90s_script(biz, s, leaks, nk, lost, offer)
    return report


def load_report(run_id: str, biz_slug: str) -> Optional[Dict[str, Any]]:
    """Full single-business report. Backfills Loom scripts if absent."""
    biz_dir = os.path.join(REPORTS_DIR, run_id, biz_slug)
    report = _safe_load_json(os.path.join(biz_dir, "report.json"))
    if not report:
        return None
    return _backfill_loom(report)


def screenshot_path(run_id: str, biz_slug: str, which: str) -> Optional[str]:
    """Resolve a screenshot file path; returns None if missing.

    `which` must be 'desktop' or 'mobile' -- guards against path
    traversal via the URL parameter.
    """
    if which not in ("desktop", "mobile"):
        return None
    fname = f"screenshot_{which}.png"
    p = os.path.join(REPORTS_DIR, run_id, biz_slug, fname)
    return p if os.path.exists(p) else None
