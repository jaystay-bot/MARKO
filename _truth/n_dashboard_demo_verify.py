"""TRUTH verifier for N004-MARKO-DEMO-TRUTH-CLOSE-LOOP-V2.

In-process Werkzeug serves dashboard.app on a free port; sync Playwright
Chromium drives the dashboard end-to-end. No subprocess. No external HTTP.
Captures evidence screenshots into leak_reports/_n004_evidence/.

14-point check per S1_LOCKED_N.md:
  1. /leaks (no token) -> 403
  2. /leaks?token=bad -> 403
  3. /leaks?token=verify-token -> 200 + safety banner + presets render
  4. at least one ENABLED demo preset (real scan on disk) -- click it
  5. business list renders (>=1 row); click first 'open report'
  6. report viewer: biz name, top 3 leaks, outreach, loom 30, loom 90,
     mini commercial, suggested fixes, suggested offer, print-pdf button
  7. screenshot <img> resolves (HTTP 200, PNG magic, >=1KB)
  8. mobile viewport: reload current report URL, key blocks still visible,
     evidence screenshot saved
  9. browser reload (page.reload()) preserves render
 10. zero browser console errors AND zero pageerror events across the run
 11. regression: /quote (Host: quote.bookermove.com) returns 200 + form
 12. regression: /__diag returns 200 + JSON shape
 13. regression: /cockpit?token=verify-token returns 200
 14. no fake-data markers in any rendered page
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

# Belt + suspenders: live-send paths must remain off for the verifier
os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
os.environ.pop("MARKO_OUTREACH_LIVE", None)
os.environ["ADMIN_TOKEN"] = "verify-token"

import dashboard                # noqa: E402
import marko_leak_dashboard     # noqa: E402

EVIDENCE_DIR = os.path.join(ROOT, "leak_reports", "_n004_evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

FAKE_NEEDLES = (
    "lorem ipsum", "demo lead", "fake_", "todo_fake",
    "leverage ai", "10x your", "growth hack", "synergy",
)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_ready(url, timeout=10.0):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status < 500:
                    return True
        except Exception as exc:
            last_exc = exc
            time.sleep(0.2)
    raise RuntimeError(f"server not ready at {url}: {last_exc}")


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def _check_screenshot_response(page, src, label, errors):
    """Fetch the actual screenshot URL via the browser context's request
    API; verify HTTP 200, PNG magic, >=1KB.
    """
    try:
        resp = page.context.request.get(src)
    except Exception as exc:
        fail(errors, label, f"screenshot fetch threw: {exc}")
        return
    if resp.status != 200:
        fail(errors, label, f"screenshot HTTP {resp.status} for {src}")
        return
    body = resp.body()
    if len(body) < 1024:
        fail(errors, label, f"screenshot too small ({len(body)} bytes): {src}")
        return
    if body[:8] != b"\x89PNG\r\n\x1a\n":
        fail(errors, label, f"screenshot not a PNG: {src}")


def _check_no_fake(body_text, errors, label):
    lc = body_text.lower()
    for needle in FAKE_NEEDLES:
        if needle in lc:
            fail(errors, label, f"fake/demo string leaked: {needle!r}")


def main():
    errors = []
    evidence = []
    runtime = {}
    t0 = time.time()

    # ---- Boot Werkzeug in a background thread, in-process
    from werkzeug.serving import make_server
    port = _free_port()
    server = make_server("127.0.0.1", port, dashboard.app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    try:
        _wait_for_ready(f"{base}/__diag", timeout=10.0)
    except Exception as exc:
        fail(errors, "boot", str(exc))
        server.shutdown()
        print(json.dumps({"ok": False, "errors": errors}, indent=2))
        return 1

    # ---- Make sure there is at least one verified scan to demo
    presets = marko_leak_dashboard.available_presets()
    enabled_presets = [p for p in presets if p["available"]]
    if not enabled_presets:
        fail(errors, "data",
             "no enabled demo presets -- need at least one verified "
             "scan on disk (run marko_leak_engine first)")

    # ---- Drive Playwright
    from playwright.sync_api import sync_playwright

    console_msgs = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("console",
                    lambda m: console_msgs.append((m.type, m.text)))
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            # 1) gate: no token -> 403
            r = page.context.request.get(f"{base}/leaks")
            if r.status != 403:
                fail(errors, "gate", f"/leaks no-token: expected 403, got {r.status}")

            # 2) gate: bad token -> 403
            r = page.context.request.get(f"{base}/leaks?token=bad")
            if r.status != 403:
                fail(errors, "gate", f"/leaks bad-token: expected 403, got {r.status}")

            # 3) /leaks?token=verify-token -> 200 + chrome
            t1 = time.time()
            page.goto(f"{base}/leaks?token=verify-token",
                      wait_until="domcontentloaded")
            runtime["leaks_load_s"] = round(time.time() - t1, 2)
            page.wait_for_selector('[data-test="safety-banner"]', timeout=5000)
            page.wait_for_selector('[data-test="presets"]', timeout=5000)
            shot_path = os.path.join(EVIDENCE_DIR, "01_leaks_index_desktop.png")
            page.screenshot(path=shot_path, full_page=True)
            evidence.append(os.path.relpath(shot_path, ROOT))
            body_text = page.inner_text("body")
            _check_no_fake(body_text, errors, "leaks index")
            if "MARKO Internet Leak Engine" not in body_text:
                fail(errors, "leaks", "header missing on /leaks")
            if "Live sending is OFF" not in body_text:
                fail(errors, "leaks",
                     "safety banner missing or doesn't say 'Live sending is OFF'")

            # 4) at least one ENABLED preset; click it
            enabled = page.locator('[data-test="preset"][data-preset-available="1"]')
            if enabled.count() == 0:
                fail(errors, "presets", "no enabled preset rendered on /leaks")
            else:
                href = enabled.first.get_attribute("href")
                t2 = time.time()
                page.goto(base + href, wait_until="domcontentloaded")
                runtime["preset_click_s"] = round(time.time() - t2, 2)
                page.wait_for_selector('[data-test="business-list"]', timeout=5000)
                shot_path = os.path.join(EVIDENCE_DIR, "02_run_view.png")
                page.screenshot(path=shot_path, full_page=True)
                evidence.append(os.path.relpath(shot_path, ROOT))

                # 5) business list, click first open-report
                rows = page.locator('[data-test="biz-row"]')
                if rows.count() < 1:
                    fail(errors, "biz_list",
                         "expected >=1 business row after preset click")
                else:
                    open_link = page.locator('[data-test="open-report"]').first
                    href2 = open_link.get_attribute("href")
                    t3 = time.time()
                    page.goto(base + href2, wait_until="domcontentloaded")
                    runtime["report_open_s"] = round(time.time() - t3, 2)

                    # 6) report viewer: every required block must exist
                    for sel in ('[data-test="biz-name"]',
                                '[data-test="screenshots"]',
                                '[data-test="top-3-leaks"]',
                                '[data-test="why-it-matters"]',
                                '[data-test="suggested-fixes"]',
                                '[data-test="suggested-offer"]',
                                '[data-test="outreach-email"]',
                                '[data-test="loom-30"]',
                                '[data-test="loom-90"]',
                                '[data-test="mini-commercial"]',
                                '[data-test="print-pdf"]'):
                        if page.locator(sel).count() == 0:
                            fail(errors, "report",
                                 f"required block missing: {sel}")

                    body_text = page.inner_text("body")
                    _check_no_fake(body_text, errors, "report viewer")

                    # 7) screenshot <img> resolves (real PNG bytes)
                    for sel, label in (
                        ('[data-test="screenshot-desktop"]', "screenshot desktop"),
                        ('[data-test="screenshot-mobile"]',  "screenshot mobile"),
                    ):
                        loc = page.locator(sel)
                        if loc.count() == 0:
                            fail(errors, "report", f"{label} <img> missing")
                            continue
                        src = loc.first.get_attribute("src")
                        if not src or not src.startswith("/leaks/"):
                            fail(errors, "report",
                                 f"{label} src missing or wrong: {src!r}")
                            continue
                        _check_screenshot_response(
                            page, base + src, label, errors)

                    shot_path = os.path.join(EVIDENCE_DIR, "03_report_desktop.png")
                    page.screenshot(path=shot_path, full_page=True)
                    evidence.append(os.path.relpath(shot_path, ROOT))

                    # 9) reload preserves the rendered report
                    page.reload(wait_until="domcontentloaded")
                    if page.locator('[data-test="loom-30"]').count() == 0:
                        fail(errors, "reload",
                             "loom-30 block disappeared after reload")

                    # 8) mobile viewport: reload + assert key blocks
                    ctx_m = browser.new_context(
                        viewport={"width": 375, "height": 812},
                        device_scale_factor=2.0,
                        is_mobile=True, has_touch=True,
                        user_agent=(
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
                            "Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                            "Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                        ),
                    )
                    page_m = ctx_m.new_page()
                    page_m.on("console",
                              lambda m: console_msgs.append((m.type, m.text)))
                    page_m.on("pageerror", lambda e: page_errors.append(str(e)))
                    page_m.goto(base + href2, wait_until="domcontentloaded")
                    for sel in ('[data-test="biz-name"]',
                                '[data-test="top-3-leaks"]',
                                '[data-test="loom-30"]',
                                '[data-test="print-pdf"]'):
                        if page_m.locator(sel).count() == 0:
                            fail(errors, "mobile",
                                 f"mobile viewport missing: {sel}")
                    shot_path = os.path.join(EVIDENCE_DIR, "04_report_mobile.png")
                    page_m.screenshot(path=shot_path, full_page=False)
                    evidence.append(os.path.relpath(shot_path, ROOT))
                    ctx_m.close()

            # 11) regression: /quote on public host
            r = page.context.request.get(
                f"{base}/quote",
                headers={"Host": "quote.bookermove.com"},
            )
            if r.status != 200:
                fail(errors, "regression",
                     f"/quote returned {r.status}")
            elif "pickup_zip" not in r.text():
                fail(errors, "regression",
                     "/quote body missing pickup_zip form field")

            # 12) /__diag JSON
            r = page.context.request.get(f"{base}/__diag")
            if r.status != 200:
                fail(errors, "regression", f"/__diag returned {r.status}")
            else:
                try:
                    j = r.json()
                    if "backend" not in j or "routing" not in j:
                        fail(errors, "regression",
                             "/__diag missing expected keys")
                except Exception as exc:
                    fail(errors, "regression", f"/__diag json parse: {exc}")

            # 13) /cockpit
            r = page.context.request.get(
                f"{base}/cockpit?token=verify-token")
            if r.status != 200:
                fail(errors, "regression", f"/cockpit returned {r.status}")

            ctx.close()
        finally:
            browser.close()

    # 10) console error sweep across all pages we drove
    console_errors = [(t, txt) for (t, txt) in console_msgs if t == "error"]
    if console_errors:
        fail(errors, "console",
             f"{len(console_errors)} console.error events: "
             f"{console_errors[:3]}")
    if page_errors:
        fail(errors, "pageerror",
             f"{len(page_errors)} page errors: {page_errors[:3]}")

    runtime["total_s"] = round(time.time() - t0, 2)
    server.shutdown()

    summary = {
        "ok": not errors,
        "n": "N004-MARKO-DEMO-TRUTH-CLOSE-LOOP-V2",
        "verify_cmd": "python _truth/n_dashboard_demo_verify.py",
        "exit_code_will_be": 0 if not errors else 1,
        "port": port,
        "presets_total": len(presets),
        "presets_enabled": len(enabled_presets),
        "runtime_s": runtime,
        "evidence_screenshots": evidence,
        "console_messages": len(console_msgs),
        "console_errors": len(console_errors),
        "page_errors": len(page_errors),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
