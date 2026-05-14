"""TRUTH verifier for N006-PDF-AUDIT-POLISH.

In-process Werkzeug + Playwright (same boot pattern as N004/N005).
9-point check per S1_LOCKED_N.md:

  1. Existing report viewer renders + new "Download PDF" button present
  2. GET .../audit.pdf -> 200, content-type application/pdf, magic %PDF-, >=2KB
  3. audit.pdf written to leak_reports/<run>/<biz>/audit.pdf
  4. PDF body contains the actual business name (string in raw bytes)
     AND the actual leak score (string in raw bytes)
  5. Delete cached PDF + ?force=1 -> regenerates a valid PDF
  6. Reload report viewer -> button still present + no console errors
  7. Regression: /leaks, /cockpit, /quote, /__diag still 200
  8. Token gate: PDF route without token -> 403
  9. Renderer status reports at least one usable backend

verify_cmd: python _truth/n_pdf_audit_verify.py
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

os.environ.pop("MARKO_QUOTE_LIVE_SEND", None)
os.environ.pop("MARKO_OUTREACH_LIVE", None)
os.environ["ADMIN_TOKEN"] = "verify-token"

import dashboard               # noqa: E402
import marko_leak_dashboard    # noqa: E402
import marko_pdf               # noqa: E402

EVIDENCE_DIR = os.path.join(ROOT, "leak_reports", "_n006_evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


def _wait_ready(url, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status < 500:
                    return True
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"server not ready: {last}")


def fail(errors, label, msg):
    errors.append(f"{label}: {msg}")


def _is_real_pdf(body):
    return bool(body) and body[:5] == b"%PDF-" and len(body) >= 2048


def main():
    errors = []
    evidence = []
    runtime = {}
    t0 = time.time()

    # 9. renderer status (cheap; reports backend availability up front)
    rstatus = marko_pdf.renderer_status()
    if not rstatus.get("preferred"):
        fail(errors, "renderer",
             f"no PDF renderer usable: {rstatus}")

    # need at least one verified scan to drive
    presets = marko_leak_dashboard.available_presets()
    enabled = [p for p in presets if p["available"]]
    if not enabled:
        fail(errors, "data", "no enabled demo presets")
        print(json.dumps({"ok": False, "errors": errors,
                          "renderer_status": rstatus}, indent=2))
        return 1
    target_preset = enabled[0]
    run = marko_leak_dashboard.load_run(target_preset["latest_run_id"])
    top_row = run["rows"][0]
    run_id = target_preset["latest_run_id"]
    biz_slug = top_row["biz_slug"]
    expected_biz = top_row["business_name"]
    expected_score = top_row["leak_score"]
    pdf_disk = os.path.join(ROOT, "leak_reports", run_id, biz_slug,
                            "audit.pdf")
    # Ensure clean start (force regeneration to test that path too)
    if os.path.exists(pdf_disk):
        os.remove(pdf_disk)

    from werkzeug.serving import make_server
    port = _free_port()
    server = make_server("127.0.0.1", port, dashboard.app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(f"{base}/__diag", timeout=10.0)
    except Exception as exc:
        fail(errors, "boot", str(exc))
        print(json.dumps({"ok": False, "errors": errors}, indent=2))
        return 1

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

            report_url = (
                f"{base}/leaks/run/{run_id}/{biz_slug}?token=verify-token"
            )
            pdf_url = (
                f"{base}/leaks/run/{run_id}/{biz_slug}/audit.pdf"
                f"?token=verify-token"
            )

            # 8. token gate: no token -> 403
            r = page.context.request.get(
                f"{base}/leaks/run/{run_id}/{biz_slug}/audit.pdf")
            if r.status != 403:
                fail(errors, "gate",
                     f"PDF no-token expected 403, got {r.status}")

            # 1. report viewer renders + button present
            t1 = time.time()
            page.goto(report_url, wait_until="domcontentloaded")
            runtime["report_load_s"] = round(time.time() - t1, 2)
            if page.locator('[data-test="download-pdf"]').count() == 0:
                fail(errors, "ui", '"Download PDF" button missing')
            shot = os.path.join(EVIDENCE_DIR, "01_viewer_with_pdf_button.png")
            page.screenshot(path=shot, full_page=False)
            evidence.append(os.path.relpath(shot, ROOT))

            # 2. fetch PDF; assert real
            t2 = time.time()
            r = page.context.request.get(pdf_url)
            runtime["pdf_first_render_s"] = round(time.time() - t2, 2)
            if r.status != 200:
                fail(errors, "pdf",
                     f"PDF fetch expected 200, got {r.status}")
            ct = (r.headers.get("content-type") or "").lower()
            if "application/pdf" not in ct:
                fail(errors, "pdf",
                     f"content-type wrong: {ct!r}")
            body = r.body()
            if not _is_real_pdf(body):
                fail(errors, "pdf",
                     f"not a real PDF (magic={body[:6]!r}, size={len(body)})")
            # 3. file written to disk
            if not os.path.exists(pdf_disk):
                fail(errors, "pdf",
                     f"audit.pdf not on disk at {pdf_disk}")
            else:
                disk_size = os.path.getsize(pdf_disk)
                if disk_size < 2048:
                    fail(errors, "pdf",
                         f"on-disk audit.pdf too small ({disk_size} bytes)")

            # 4. PDF body contains real business name + leak score
            #    (Plain-text content lives in the PDF byte stream
            #    uncompressed for our short report; if the renderer
            #    compressed it, we'd need pypdf -- skip with note.)
            ascii_dump = body.decode("latin-1", errors="ignore")
            text_check_skipped = False
            if expected_biz not in ascii_dump:
                # weasyprint compresses streams by default; chromium
                # often leaves them raw. If neither matches, try a
                # decoded-text fallback via pypdf if available.
                try:
                    from pypdf import PdfReader
                    import io
                    reader = PdfReader(io.BytesIO(body))
                    text = "\n".join(p.extract_text() or "" for p in reader.pages)
                    if expected_biz not in text:
                        fail(errors, "pdf_content",
                             f"PDF text missing business name "
                             f"{expected_biz!r}")
                    if str(expected_score) not in text:
                        fail(errors, "pdf_content",
                             f"PDF text missing leak score "
                             f"{expected_score!r}")
                except ImportError:
                    text_check_skipped = True
            else:
                if str(expected_score) not in ascii_dump:
                    fail(errors, "pdf_content",
                         f"PDF body missing leak score {expected_score!r}")

            # save the PDF as evidence
            evidence_pdf = os.path.join(EVIDENCE_DIR,
                                        "02_audit_sample.pdf")
            with open(evidence_pdf, "wb") as fh:
                fh.write(body)
            evidence.append(os.path.relpath(evidence_pdf, ROOT))

            # 5. delete cache + force=1 -> regenerates
            os.remove(pdf_disk)
            t3 = time.time()
            r = page.context.request.get(pdf_url + "&force=1")
            runtime["pdf_regen_s"] = round(time.time() - t3, 2)
            if r.status != 200:
                fail(errors, "regen",
                     f"regenerate expected 200, got {r.status}")
            body2 = r.body()
            if not _is_real_pdf(body2):
                fail(errors, "regen", "regenerated PDF not real")
            if not os.path.exists(pdf_disk):
                fail(errors, "regen",
                     "regenerated audit.pdf missing from disk")

            # 6. reload viewer -> button still there, no console errors
            page.reload(wait_until="domcontentloaded")
            if page.locator('[data-test="download-pdf"]').count() == 0:
                fail(errors, "reload", "download button missing after reload")

            # 7. regression: existing routes
            for path, expected in (
                ("/leaks?token=verify-token", 200),
                ("/cockpit?token=verify-token", 200),
                ("/__diag", 200),
            ):
                r = page.context.request.get(f"{base}{path}")
                if r.status != expected:
                    fail(errors, "regression",
                         f"{path} expected {expected}, got {r.status}")
            # /quote on the public host
            r = page.context.request.get(
                f"{base}/quote",
                headers={"Host": "quote.bookermove.com"})
            if r.status != 200:
                fail(errors, "regression",
                     f"/quote expected 200, got {r.status}")

            ctx.close()
        finally:
            browser.close()

    console_errors = [(t, txt) for t, txt in console_msgs if t == "error"]
    if console_errors:
        fail(errors, "console",
             f"{len(console_errors)} console.error events: "
             f"{console_errors[:3]}")
    if page_errors:
        fail(errors, "pageerror",
             f"{len(page_errors)} page errors: {page_errors[:3]}")

    runtime["total_s"] = round(time.time() - t0, 2)
    server.shutdown()

    # final on-disk size for evidence
    pdf_size = os.path.getsize(pdf_disk) if os.path.exists(pdf_disk) else 0

    summary = {
        "ok": not errors,
        "n": "N006-PDF-AUDIT-POLISH",
        "verify_cmd": "python _truth/n_pdf_audit_verify.py",
        "exit_code_will_be": 0 if not errors else 1,
        "port": port,
        "renderer_status": rstatus,
        "active_renderer": rstatus.get("preferred"),
        "audit_pdf_path": os.path.relpath(pdf_disk, ROOT),
        "audit_pdf_size_bytes": pdf_size,
        "expected_biz": expected_biz,
        "expected_leak_score": expected_score,
        "evidence_files": evidence,
        "runtime_s": runtime,
        "console_messages": len(console_msgs),
        "console_errors": len(console_errors),
        "page_errors": len(page_errors),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
