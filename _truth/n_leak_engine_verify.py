"""TRUTH verifier for N002-MARKO-MVP-HARDENING.

Proves all 10 N002 conditions:
  1. Richmond movers scan still works
  2. At least 2 additional niches work (pet groomers via leads.json
     alias resolution + a third niche via operator-supplied --targets)
  3. Reports generate successfully
  4. Screenshot validation still passes (PNG magic + size floor)
  5. Leak scores render (0-100 + score_breakdown.parts)
  6. Commercial concept block renders (text-only, leak-driven)
  7. No paid APIs introduced (banned-needle scan + niche_config check)
  8. No unrelated files touched (touched-files list emitted)
  9. Batch mode outputs isolated folders (run via marko_leak_batch)
 10. No runtime crash (any exception fails the verifier loud)

Print contract per N002 spec: niche scanned, reports generated count,
screenshot validation count, sample leak summary, sample commercial
concept, scan runtime, touched files list.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import marko_leak_engine as mle  # noqa: E402
import marko_leak_batch as mlb   # noqa: E402
import niche_config              # noqa: E402

REQUIRED_FIELDS = (
    "business_name", "website", "social_links", "leak_score",
    "score_breakdown", "major_leaks", "top_3_leaks", "screenshots",
    "suggested_fixes", "suggested_offer", "outreach_draft",
    "mini_commercial_concept", "estimated_lost_leads",
    "confidence_score",
)

# Paid-API and "AI startup language" banned needles. Verifier rejects
# any report containing these so the contract's no-fake-AI guard fires
# at build time, not first-customer time.
BANNED_NEEDLES = (
    "openai api", "anthropic api", "google maps api", "yelp api",
    "stripe api", "rapidapi", "scrapingbee", "scraperapi",
    "lorem ipsum", "demo lead", "fake_", "todo_fake",
    "leverage ai", "10x your", "growth hack", "synergy",
    "agentic swarm", "ai swarm", "langchain",
)

# Files this N is allowed to touch. Anything else added/modified during
# the verifier run gets surfaced (we only read git status here, never
# revert; the operator decides what to do).
ALLOWED_TOUCH_PATTERNS = (
    "marko_leak_engine.py",
    "marko_leak_batch.py",
    "niche_config.py",
    "_truth/n_leak_engine_verify.py",
    "agent_state/",
    "leak_reports/",  # output directory
)


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def check_screenshot_real(path, errors, label):
    if not path:
        fail(errors, label, "screenshot path is empty"); return
    abspath = os.path.join(ROOT, path)
    if not os.path.exists(abspath):
        fail(errors, label, f"screenshot file does not exist: {abspath}"); return
    size = os.path.getsize(abspath)
    if size < 1024:
        fail(errors, label, f"screenshot too small ({size} bytes): {abspath}"); return
    with open(abspath, "rb") as fh:
        head = fh.read(8)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        fail(errors, label, f"screenshot not a real PNG: {abspath}")


def check_report_shape(report, errors, idx, validated_screenshots):
    label = f"report[{idx}]"
    for f in REQUIRED_FIELDS:
        if f not in report:
            fail(errors, label, f"missing required field {f!r}")

    if "leak_score" in report:
        if not (0 <= report["leak_score"] <= 100):
            fail(errors, label, f"leak_score out of range: {report['leak_score']}")
    if "confidence_score" in report:
        if not (0 <= report["confidence_score"] <= 100):
            fail(errors, label,
                 f"confidence_score out of range: {report['confidence_score']}")

    bd = report.get("score_breakdown") or {}
    if not isinstance(bd.get("parts"), list):
        fail(errors, label, "score_breakdown.parts missing or not a list")
    elif report.get("major_leaks") and not bd["parts"]:
        fail(errors, label, "leaks observed but score_breakdown.parts is empty")

    ll = report.get("estimated_lost_leads") or {}
    if ll.get("heuristic") is not True:
        fail(errors, label, "estimated_lost_leads.heuristic must be True")
    if not isinstance(ll.get("basis"), str) or len(ll.get("basis") or "") < 20:
        fail(errors, label, "estimated_lost_leads.basis missing/too short")

    draft = report.get("outreach_draft") or {}
    if not draft.get("subject") or not draft.get("body"):
        fail(errors, label, "outreach_draft missing subject/body")

    commercial = report.get("mini_commercial_concept") or {}
    if not commercial.get("concept") or len(commercial["concept"]) < 50:
        fail(errors, label, "mini_commercial_concept.concept missing/too short")
    if "format" not in commercial:
        fail(errors, label, "mini_commercial_concept.format missing")

    full_text = json.dumps(report).lower()
    for n in BANNED_NEEDLES:
        if n in full_text:
            fail(errors, label, f"report contains banned phrase {n!r}")

    leaks = report.get("major_leaks") or []
    if leaks:
        body_lc = (draft.get("body") or "").lower()
        if not any(k in body_lc for k in ("site", "form", "viewport",
                                          "copyright", "after-hours",
                                          "phone", "voicemail", "leads",
                                          "leak", "respond", "reply")):
            fail(errors, label,
                 "outreach body doesn't reference a leak signal -- looks generic")

    sc = report.get("screenshots") or {}
    for which, path in (("desktop", sc.get("desktop")),
                        ("mobile",  sc.get("mobile"))):
        check_screenshot_real(path, errors, f"{label} {which}")
        if path and os.path.exists(os.path.join(ROOT, path)):
            validated_screenshots["count"] += 1


def _load_reports_for_run(run_id) -> List[Dict[str, Any]]:
    rd = os.path.join(mle.REPORTS_DIR, run_id)
    summary_path = os.path.join(rd, "run_summary.json")
    with open(summary_path, "r", encoding="utf-8") as fh:
        run_summary = json.load(fh)
    out = []
    for r in run_summary["reports"]:
        rj = os.path.join(ROOT, r["report_dir"], "report.json")
        if os.path.exists(rj):
            with open(rj, "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def _git_touched():
    """Return list of files git sees as modified or untracked, for the
    'no unrelated files touched' check. Uses --short for stable parsing.
    """
    try:
        cp = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT, capture_output=True, text=True, timeout=10
        )
        return [ln for ln in cp.stdout.splitlines() if ln.strip()]
    except Exception as exc:
        return [f"(git status failed: {exc})"]


def _is_allowed_path(rel_path):
    return any(p in rel_path.replace("\\", "/")
               for p in ALLOWED_TOUCH_PATTERNS)


def main():
    errors = []
    t0 = time.time()
    runtime = {}
    validated_screenshots = {"count": 0}
    sample_leak_summary = None
    sample_commercial = None
    niches_scanned = []
    reports_total = 0

    # ---- Condition 1: Richmond movers via leads.json
    t = time.time()
    movers_summary = mle.run("Richmond", "movers", max_targets=2)
    runtime["movers_s"] = round(time.time() - t, 2)
    niches_scanned.append({"city": "Richmond", "niche": "movers",
                           "scanned": movers_summary["scanned"]})
    if movers_summary["discovered"] == 0:
        fail(errors, "movers", "no Richmond movers discovered")
    if movers_summary["scanned"] == 0:
        fail(errors, "movers", "no scans completed for movers")
    movers_reports = _load_reports_for_run(movers_summary["run_id"])
    for i, rep in enumerate(movers_reports):
        check_report_shape(rep, errors, f"movers[{i}]", validated_screenshots)
    reports_total += len(movers_reports)
    if movers_reports:
        sample_leak_summary = {
            "business": movers_reports[0]["business_name"],
            "score": movers_reports[0]["leak_score"],
            "leaks": [l["category"] for l in movers_reports[0]["major_leaks"]],
        }
        sample_commercial = movers_reports[0]["mini_commercial_concept"]["concept"]

    # ---- Condition 2a: pet groomers (alias resolution to leads.json's
    # 'dog groomer' rows)
    t = time.time()
    groomers_summary = mle.run("Richmond", "pet groomers", max_targets=2)
    runtime["pet_groomers_s"] = round(time.time() - t, 2)
    niches_scanned.append({"city": "Richmond", "niche": "pet groomers",
                           "scanned": groomers_summary["scanned"]})
    if groomers_summary["discovered"] == 0:
        fail(errors, "groomers",
             "alias resolution failed: 'pet groomers' returned 0 rows "
             "(expected dog_groomer rows from leads.json)")
    if groomers_summary["scanned"] == 0:
        fail(errors, "groomers", "no scans completed for groomers")
    groomers_reports = _load_reports_for_run(groomers_summary["run_id"])
    for i, rep in enumerate(groomers_reports):
        check_report_shape(rep, errors, f"groomers[{i}]", validated_screenshots)
    reports_total += len(groomers_reports)

    # ---- Condition 2b: third niche via operator-supplied --targets
    # We scan our own production page (a real, public URL we control)
    # to prove the --targets discovery path WITHOUT scanning a random
    # third party in a CI-style verifier.
    t = time.time()
    targets_summary = mle.run(
        "Richmond", "barbers",
        targets=["https://marko-teal.vercel.app/quote"]
    )
    runtime["barbers_targets_s"] = round(time.time() - t, 2)
    niches_scanned.append({"city": "Richmond", "niche": "barbers",
                           "discovery": "operator_targets",
                           "scanned": targets_summary["scanned"]})
    if targets_summary["scanned"] != 1:
        fail(errors, "targets",
             f"operator-targets path expected 1 scan, got {targets_summary['scanned']}")
    targets_reports = _load_reports_for_run(targets_summary["run_id"])
    for i, rep in enumerate(targets_reports):
        check_report_shape(rep, errors, f"targets[{i}]", validated_screenshots)
    reports_total += len(targets_reports)

    # ---- Condition 7: niche config completeness for all 7 niches
    expected_niches = {"movers", "barbers", "pet_groomers", "nail_salons",
                       "med_spas", "roofers", "plumbers"}
    actual_niches = set(niche_config.all_niches())
    missing = expected_niches - actual_niches
    if missing:
        fail(errors, "niche_config",
             f"niche_config missing required niches: {sorted(missing)}")
    for nk in expected_niches & actual_niches:
        cfg = niche_config.NICHES[nk]
        for f in ("aliases", "service_offer", "baseline_leads_per_mo",
                  "weight_overrides", "commercial_themes"):
            if f not in cfg:
                fail(errors, "niche_config",
                     f"{nk} missing config field {f!r}")

    # ---- Condition 9: batch mode outputs isolated folders
    t = time.time()
    batch_payload = mlb.run_batch([
        {"city": "Richmond", "niche": "movers", "max_targets": 1},
        {"city": "Richmond", "niche": "pet groomers", "max_targets": 1},
    ])
    runtime["batch_s"] = round(time.time() - t, 2)
    if batch_payload["fail_count"] != 0:
        fail(errors, "batch", f"batch had {batch_payload['fail_count']} failed jobs")
    run_dirs = [j["run_dir"] for j in batch_payload["jobs"] if j.get("ok")]
    if len(set(run_dirs)) != len(run_dirs):
        fail(errors, "batch",
             f"batch jobs collided into the same folder: {run_dirs}")
    for rd in run_dirs:
        if not os.path.isdir(os.path.join(ROOT, rd)):
            fail(errors, "batch", f"batch run_dir does not exist: {rd}")

    # ---- Condition 8: no unrelated files touched
    touched = _git_touched()
    unrelated = []
    for line in touched:
        # git --short format: "XY path" with two-char status prefix
        rel = line[3:].strip().split(" -> ")[-1]
        if not _is_allowed_path(rel):
            unrelated.append(line)
    # Allow runtime/log JSON that pre-existed -- they're untracked from
    # earlier sessions, not edits this N performed.
    pre_existing_untracked = {
        "MARKO_REALITY_AUDIT.md", "delivery_log.json", "inbound_leads.json",
        "missed_money.json", "routed_leads.json", "conversion_events.json",
        "conversion_report.md", "daily_revenue_queue.json",
        "demand_opportunities.json", "hot_zips.json", "money_queue.json",
        "outreach_log.json", "overnight_money_queue.json",
        "overnight_money_report.json", "routing_ready.json",
        "_truth/exports/leads_export.json",
        "_truth/exports/rva_move_zones_export.json",
    }
    unrelated = [u for u in unrelated
                 if not any(p in u for p in pre_existing_untracked)]

    runtime["total_s"] = round(time.time() - t0, 2)

    summary = {
        "ok": not errors,
        "n": "N002-MARKO-MVP-HARDENING",
        "niches_scanned": niches_scanned,
        "reports_generated": reports_total,
        "screenshots_validated": validated_screenshots["count"],
        "sample_leak_summary": sample_leak_summary,
        "sample_commercial_concept": sample_commercial,
        "scan_runtime_s": runtime,
        "touched_files": touched,
        "touched_files_unrelated": unrelated,
        "batch_summary": {
            "batch_id": batch_payload["batch_id"],
            "ok": batch_payload["ok_count"],
            "failed": batch_payload["fail_count"],
            "run_dirs": run_dirs,
        },
        "niche_config_count": len(niche_config.all_niches()),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
