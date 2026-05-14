"""Visible Truth Watcher (N007).

Boots the existing Flask app in-process via Werkzeug, drives Chromium
across the operator routes, captures real visual + runtime failures,
and writes a status-log line per run. Pure observer -- never edits
product code, never mutates report data (verified by hashing
report.json before/after the PDF check).

Three modes:
  default               one-shot, headless. Exit 0/1. Contract verify_cmd.
  --visible             one-shot, headed (browser window opens).
  --watch [--interval N]
                        loop forever (Ctrl-C to stop). Combine with
                        --visible to keep the browser open during a
                        coding session.

Other flags:
  --token <TOKEN>       override ADMIN_TOKEN (default: 'verify-token')
  --no-pdf              skip the PDF render check (faster loops)

Usage examples:
  python _truth/visible_truth_watcher.py
  python _truth/visible_truth_watcher.py --visible
  python _truth/visible_truth_watcher.py --watch --visible --interval 15

Status log: _truth/visible_truth_watcher_status.log (append-only,
JSON-per-line). Failure screenshots: leak_reports/_n007_evidence/.

NEVER mutates: report.json, run_summary.json, audit.pdf (only triggers
its lazy generation), or any product code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

# Belt + suspenders for the Flask routes that read these envs.
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
os.environ.pop("MARKO_OUTREACH_LIVE", None)
os.environ.setdefault("ADMIN_TOKEN", "verify-token")

import dashboard               # noqa: E402
import marko_leak_dashboard    # noqa: E402

EVIDENCE_DIR = os.path.join(ROOT, "leak_reports", "_n007_evidence")
STATUS_LOG = os.path.join(HERE, "visible_truth_watcher_status.log")


# ---------------- helpers ----------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(url: str, timeout: float = 10.0) -> Optional[str]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status < 500:
                    return None
        except Exception as exc:
            last = str(exc)
            time.sleep(0.2)
    return last or "unknown"


def _slugify(s: str, max_len: int = 60) -> str:
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    res = "".join(out).strip("_")
    return (res[:max_len] or "x")


def _hash_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_status(line: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATUS_LOG), exist_ok=True)
    with open(STATUS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str) + "\n")


def _ensure_evidence_dir() -> None:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)


# ---------------- target plan -----------------------------------------------

def _plan_routes(token: str) -> List[Dict[str, Any]]:
    """Compose the route list. The leaks-run/report/PDF entries are
    added only if a verified scan exists on disk -- otherwise we mark
    them as 'skipped: no verified scan' rather than fail.
    """
    plan: List[Dict[str, Any]] = [
        {"name": "diag",            "url": "/__diag",
         "expect": 200,             "check_blank": True},
        {"name": "quote_public",    "url": "/quote",
         "expect": 200,             "host": "quote.bookermove.com",
         "must_contain": ["pickup_zip", "Get your moving quote"]},
        {"name": "leaks_index",     "url": f"/leaks?token={token}",
         "expect": 200,             "check_blank": True},
        {"name": "cockpit",         "url": f"/cockpit?token={token}",
         "expect": 200,             "check_blank": True},
        {"name": "money",           "url": f"/money?token={token}",
         "expect": 200},
        {"name": "admin_conv_json", "url": f"/admin/conversions?token={token}&format=json",
         "expect": 200},
        {"name": "review",          "url": f"/review?token={token}",
         "expect": 200,             "check_blank": True},
        {"name": "leaks_no_token",  "url": "/leaks",
         "expect": 403},
    ]

    # Dynamic: report viewer + PDF (only if we have data on disk)
    presets = marko_leak_dashboard.available_presets()
    enabled = [p for p in presets if p["available"]]
    if enabled:
        run = marko_leak_dashboard.load_run(enabled[0]["latest_run_id"])
        if run and run["rows"]:
            top = run["rows"][0]
            run_id = enabled[0]["latest_run_id"]
            plan.append({
                "name": "leaks_run_view",
                "url": f"/leaks/run/{run_id}?token={token}",
                "expect": 200,
                "check_blank": True,
            })
            plan.append({
                "name": "leaks_report_view",
                "url": f"/leaks/run/{run_id}/{top['biz_slug']}?token={token}",
                "expect": 200,
                "check_blank": True,
                "must_contain_data_test": [
                    "screenshots", "top-3-leaks", "outreach-email",
                    "loom-30", "download-pdf", "chat-panel",
                ],
            })
            plan.append({
                "name": "audit_pdf",
                "url": f"/leaks/run/{run_id}/{top['biz_slug']}/audit.pdf?token={token}",
                "expect": 200,
                "is_pdf": True,
                "report_json": os.path.join(
                    ROOT, "leak_reports", run_id, top["biz_slug"], "report.json"),
            })
    return plan


# ---------------- per-route check -------------------------------------------

def _check_route(page, base: str, target: Dict[str, Any]) -> Dict[str, Any]:
    """Run one route. Capture status, console, network, blank, content
    requirements, PDF data-mutation. Returns a result dict. Never raises.
    """
    name = target["name"]
    url = base + target["url"]
    result: Dict[str, Any] = {
        "name": name,
        "url": target["url"],
        "ok": True,
        "issues": [],
        "status": None,
        "elapsed_ms": None,
    }

    # PDF check: hash report.json BEFORE so we can compare AFTER.
    pre_hash = None
    if target.get("is_pdf") and target.get("report_json"):
        pre_hash = _hash_file(target["report_json"])

    headers = {}
    if target.get("host"):
        headers["Host"] = target["host"]

    # Use the page's request context so any required cookies / origin
    # behave like the visible browser. PDF is fetched via request API
    # so we can inspect bytes directly without download dialogs.
    if target.get("is_pdf"):
        t0 = time.time()
        try:
            r = page.context.request.get(url, headers=headers)
        except Exception as exc:
            result["ok"] = False
            result["issues"].append(f"request error: {exc}")
            return result
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        result["status"] = r.status
        if r.status != target.get("expect", 200):
            result["ok"] = False
            result["issues"].append(
                f"status {r.status}, expected {target.get('expect', 200)}")
            return result
        ct = (r.headers.get("content-type") or "").lower()
        if "application/pdf" not in ct:
            result["ok"] = False
            result["issues"].append(f"content-type wrong: {ct!r}")
        body = r.body()
        result["bytes"] = len(body)
        if not body or body[:5] != b"%PDF-":
            result["ok"] = False
            result["issues"].append(f"not a PDF (magic={body[:6]!r})")
        if len(body) < 2048:
            result["ok"] = False
            result["issues"].append(f"PDF too small ({len(body)} bytes)")
        # Data-mutation check: report.json must be byte-identical post-hit.
        if target.get("report_json"):
            post_hash = _hash_file(target["report_json"])
            result["report_json_pre_hash"] = pre_hash
            result["report_json_post_hash"] = post_hash
            if pre_hash and post_hash and pre_hash != post_hash:
                result["ok"] = False
                result["issues"].append(
                    "report.json HASH CHANGED -- PDF render path mutated data!")
        return result

    # Browser-driven check (HTML routes)
    console_msgs: List[Tuple[str, str]] = []
    page_errors: List[str] = []
    bad_responses: List[Tuple[int, str]] = []

    def _on_console(m):
        try:
            console_msgs.append((m.type, m.text))
        except Exception:
            pass

    def _on_pageerror(e):
        page_errors.append(str(e))

    def _on_response(resp):
        try:
            if 400 <= resp.status < 600:
                bad_responses.append((resp.status, resp.url))
        except Exception:
            pass

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("response", _on_response)
    try:
        t0 = time.time()
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=10000)
        except Exception as exc:
            result["ok"] = False
            result["issues"].append(f"navigation error: {exc}")
            return result
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        result["status"] = resp.status if resp else None
        expected = target.get("expect", 200)
        if result["status"] != expected:
            result["ok"] = False
            result["issues"].append(
                f"status {result['status']}, expected {expected}")
        # Stop here for explicit non-200 expectations (e.g. 403 gate)
        if expected != 200:
            return result

        # Blank-screen detector
        if target.get("check_blank"):
            try:
                body_text = page.inner_text("body")
            except Exception:
                body_text = ""
            if len(body_text.strip()) < 80:
                # Allow a bypass if the page has visible h1/h2 anyway
                if (page.locator("h1").count() == 0
                        and page.locator("h2").count() == 0):
                    result["ok"] = False
                    result["issues"].append(
                        f"blank-screen suspected (body inner_text "
                        f"{len(body_text)} chars, no h1/h2)")

        # Required substrings
        for needle in target.get("must_contain", []):
            try:
                content = page.content()
            except Exception:
                content = ""
            if needle not in content:
                result["ok"] = False
                result["issues"].append(f"missing substring: {needle!r}")

        # Required data-test elements
        for slug in target.get("must_contain_data_test", []):
            sel = f'[data-test="{slug}"]'
            if page.locator(sel).count() == 0:
                result["ok"] = False
                result["issues"].append(f"missing element: {sel}")

    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)
        page.remove_listener("response", _on_response)

    errs = [(t, txt) for (t, txt) in console_msgs if t == "error"]
    if errs:
        result["ok"] = False
        result["issues"].append(
            f"{len(errs)} console.error events: {errs[:3]}")
    if page_errors:
        result["ok"] = False
        result["issues"].append(
            f"{len(page_errors)} page errors: {page_errors[:3]}")
    if bad_responses:
        # Filter to only direct-page or critical subresources -- we
        # report ALL but flag only meaningful ones.
        result["bad_responses"] = bad_responses[:5]
        # Don't auto-fail on subresource 4xx (favicons etc.); operator
        # can scan the log if curious.

    if not result["ok"]:
        _ensure_evidence_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(EVIDENCE_DIR, f"{ts}_{_slugify(name)}.png")
        try:
            page.screenshot(path=path, full_page=True)
            result["screenshot"] = os.path.relpath(path, ROOT)
        except Exception as exc:
            result["screenshot_error"] = str(exc)

    return result


# ---------------- run loop --------------------------------------------------

def _run_once(visible: bool, token: str, do_pdf: bool) -> Dict[str, Any]:
    started = _now_iso()
    plan = _plan_routes(token)
    if not do_pdf:
        plan = [t for t in plan if not t.get("is_pdf")]

    # Boot Werkzeug in a background thread
    from werkzeug.serving import make_server
    port = _free_port()
    server = make_server("127.0.0.1", port, dashboard.app)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    boot_err = _wait_ready(f"{base}/__diag", timeout=10.0)
    if boot_err is not None:
        server.shutdown()
        return {
            "started_at": started,
            "ok": False,
            "boot_error": boot_err,
            "base_url": base,
            "results": [],
        }

    results: List[Dict[str, Any]] = []
    pw_err = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=not visible)
            except Exception as exc:
                # Fallback to headless if headed launch failed in this env
                pw_err = f"headed-launch failed: {exc}; falling back headless"
                browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                for target in plan:
                    results.append(_check_route(page, base, target))
                ctx.close()
            finally:
                browser.close()
    except Exception as exc:
        pw_err = str(exc)
    finally:
        server.shutdown()

    finished = _now_iso()
    summary = {
        "started_at": started,
        "finished_at": finished,
        "base_url": base,
        "playwright_note": pw_err,
        "routes_checked": len(results),
        "failures": [r for r in results if not r["ok"]],
        "ok": all(r["ok"] for r in results) and pw_err is None,
        "results": results,
    }
    return summary


def _print_summary(summary: Dict[str, Any]) -> None:
    print(f"\nLocalhost URL: {summary.get('base_url')}")
    print(f"Routes checked: {summary.get('routes_checked')}")
    failures = summary.get("failures") or []
    if failures:
        print(f"FAILURES: {len(failures)}")
        for f in failures:
            print(f"  - {f['name']:<22} status={f.get('status')} "
                  f"issues={f.get('issues')}")
            if f.get("screenshot"):
                print(f"      screenshot: {f['screenshot']}")
    else:
        print("FAILURES: 0")
    if summary.get("playwright_note"):
        print(f"Playwright note: {summary['playwright_note']}")
    print(f"PASS: {summary['ok']}\n")


def _watch(visible: bool, token: str, interval: float, do_pdf: bool) -> int:
    last_ok = None
    iter_n = 0
    print(f"Watcher running. Interval={interval}s. Visible={visible}. Ctrl-C to stop.")
    try:
        while True:
            iter_n += 1
            summary = _run_once(visible=visible, token=token, do_pdf=do_pdf)
            summary["loop_iter"] = iter_n
            _append_status(summary)
            _print_summary(summary)
            if summary["ok"] != last_ok:
                # State change -- print a banner so Jay sees flips fast
                if summary["ok"]:
                    print(">>> RECOVERED <<<")
                else:
                    print(">>> FAILURE STATE <<<")
            last_ok = summary["ok"]
            time.sleep(max(2.0, float(interval)))
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
        return 0


# ---------------- entry ------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MARKO Visible Truth Watcher (N007)")
    parser.add_argument("--visible", action="store_true",
                        help="open a real Chromium window (headed)")
    parser.add_argument("--watch", action="store_true",
                        help="loop forever; combine with --visible")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="seconds between watch loops (default 15)")
    parser.add_argument("--token", default=None,
                        help="ADMIN_TOKEN to use (default 'verify-token')")
    parser.add_argument("--no-pdf", action="store_true",
                        help="skip the PDF render check")
    args = parser.parse_args()

    token = args.token or os.environ.get("ADMIN_TOKEN") or "verify-token"
    os.environ["ADMIN_TOKEN"] = token

    if args.watch:
        return _watch(visible=args.visible, token=token,
                      interval=args.interval, do_pdf=not args.no_pdf)

    summary = _run_once(visible=args.visible, token=token,
                        do_pdf=not args.no_pdf)
    _append_status(summary)
    # Pretty print + JSON tail so the contract's "OUTPUT" section is
    # satisfied (URL, routes, failures, screenshots, PDF check, PASS/FAIL)
    _print_summary(summary)
    print(json.dumps({
        "ok": summary["ok"],
        "n": "N007-LOCALHOST-TRUTH-WATCHER-UPGRADE",
        "verify_cmd": "python _truth/visible_truth_watcher.py",
        "exit_code_will_be": 0 if summary["ok"] else 1,
        "base_url": summary["base_url"],
        "routes_checked": summary["routes_checked"],
        "failures_count": len(summary.get("failures") or []),
        "playwright_note": summary.get("playwright_note"),
        "status_log": os.path.relpath(STATUS_LOG, ROOT),
        "evidence_dir": os.path.relpath(EVIDENCE_DIR, ROOT),
    }, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
