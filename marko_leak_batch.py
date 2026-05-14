"""MARKO leak-engine batch runner (N002).

Run multiple (city, niche, [targets]) jobs in one shot. Each job
writes to its own timestamped folder under leak_reports/, so two jobs
never overwrite each other.

Usage:
  python marko_leak_batch.py \\
      --job "Richmond|movers" \\
      --job "Richmond|pet groomers" \\
      --max-targets 2

  python marko_leak_batch.py --jobs jobs.json

jobs.json format:
  {
    "jobs": [
      {"city": "Richmond", "niche": "movers"},
      {"city": "Richmond", "niche": "pet groomers", "max_targets": 5},
      {"city": "Richmond", "niche": "barbers",
       "targets": ["https://example-shop.com/"]}
    ]
  }

Writes a top-level batch_summary.json under leak_reports/<batch_id>/
listing every job + its run_summary path. The per-job run folders
remain in their own timestamped paths so they're individually
addressable from the rest of MARKO.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import marko_leak_engine as mle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _now_compact():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_inline_jobs(raw_jobs):
    """Parse --job 'city|niche' tokens into job dicts."""
    out = []
    for raw in raw_jobs or []:
        parts = [p.strip() for p in raw.split("|", 2)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise SystemExit(
                f"--job must be 'city|niche' (optionally |url1,url2): {raw!r}"
            )
        job = {"city": parts[0], "niche": parts[1]}
        if len(parts) >= 3 and parts[2]:
            job["targets"] = [t.strip() for t in parts[2].split(",") if t.strip()]
        out.append(job)
    return out


def load_jobs_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    jobs = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(jobs, list):
        raise SystemExit(f"jobs file {path!r} must have a 'jobs' list")
    return jobs


def run_batch(jobs, batch_id=None, default_max_targets=None):
    if batch_id is None:
        batch_id = "batch_" + _now_compact()
    batch_dir = os.path.join(mle.REPORTS_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    started = _now_iso()
    results = []
    for i, job in enumerate(jobs, start=1):
        city = job.get("city") or ""
        niche = job.get("niche") or ""
        targets = job.get("targets") or None
        max_targets = job.get("max_targets") or default_max_targets

        # Each job gets its own run_id under leak_reports/.
        run_id = (
            f"{mle._slug(city)}_{mle._slug(niche)}_{batch_id}_job{i}"
        )
        try:
            summary = mle.run(city, niche, max_targets=max_targets,
                              run_id=run_id, targets=targets)
            results.append({
                "ok": True,
                "job_index": i,
                "city": city,
                "niche": niche,
                "discovery_source": ("operator_targets" if targets
                                     else "leads.json"),
                "discovered": summary["discovered"],
                "scanned": summary["scanned"],
                "scan_failures": summary["scan_failures"],
                "run_dir": os.path.relpath(
                    os.path.join(mle.REPORTS_DIR, run_id), BASE_DIR
                ),
            })
        except Exception as exc:
            results.append({
                "ok": False,
                "job_index": i,
                "city": city,
                "niche": niche,
                "error": f"{type(exc).__name__}: {exc}",
            })

    payload = {
        "batch_id": batch_id,
        "started_at": started,
        "finished_at": _now_iso(),
        "job_count": len(jobs),
        "ok_count": sum(1 for r in results if r["ok"]),
        "fail_count": sum(1 for r in results if not r["ok"]),
        "jobs": results,
    }
    with open(os.path.join(batch_dir, "batch_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return payload


def main():
    parser = argparse.ArgumentParser(
        description="MARKO leak engine batch runner")
    parser.add_argument("--job", action="append", default=[],
                        help=("city|niche or city|niche|url1,url2 -- can "
                              "be passed multiple times"))
    parser.add_argument("--jobs", default=None,
                        help="path to a JSON file with 'jobs' list")
    parser.add_argument("--max-targets", type=int, default=None,
                        help="default max-targets cap per job (jobs may override)")
    parser.add_argument("--batch-id", default=None,
                        help="override batch folder name (default: batch_<ts>)")
    args = parser.parse_args()

    jobs = []
    if args.jobs:
        jobs.extend(load_jobs_file(args.jobs))
    if args.job:
        jobs.extend(parse_inline_jobs(args.job))
    if not jobs:
        parser.error("must provide at least one --job or --jobs <file>")

    payload = run_batch(jobs, batch_id=args.batch_id,
                        default_max_targets=args.max_targets)
    print(json.dumps(payload, indent=2))
    return 0 if payload["fail_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
