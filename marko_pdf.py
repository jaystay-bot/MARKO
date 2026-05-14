"""MARKO server-side PDF audit (N006).

Renders the same audit data as the on-screen report viewer into a
1-page `audit.pdf` next to `report.json`. Real PDF only -- no
placeholder, no stub, no fake.

Renderer order (declared in S1_LOCKED_N.md):
  1. weasyprint -- preferred per contract; requires GTK runtime
  2. Playwright/Chromium -- already installed; same engine as the
     existing browser "Print to PDF" path

The route caches the result on disk; subsequent requests stream the
file. `force=True` rebuilds even if the cache is fresh. If both
renderers fail at the OS layer, raises `PdfRenderError` so the route
returns 503 and a customer never sees a fake button silently
succeed.
"""
from __future__ import annotations

import base64
import os
import sys
from typing import Optional

from flask import render_template

import marko_leak_dashboard as mld

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = mld.REPORTS_DIR


class PdfRenderError(RuntimeError):
    """Both renderers failed. Surface in a 503; never show fake output."""


def _audit_path(run_id: str, biz_slug: str) -> str:
    return os.path.join(REPORTS_DIR, run_id, biz_slug, "audit.pdf")


def _png_to_data_uri(path: Optional[str]) -> Optional[str]:
    """Inline a screenshot as a data URI so the PDF renderer doesn't
    need URL fetch capability or absolute paths.
    """
    if not path:
        return None
    abspath = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    if not os.path.exists(abspath):
        return None
    try:
        with open(abspath, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _build_html(report: dict, run_id: str, biz_slug: str) -> str:
    """Render audit_pdf.html with the report payload + base64 screenshots."""
    sc = report.get("screenshots") or {}
    desktop_uri = _png_to_data_uri(sc.get("desktop"))
    mobile_uri = _png_to_data_uri(sc.get("mobile"))
    # render_template requires an active Flask app context. The Flask
    # route caller already has one; the verifier creates one explicitly.
    return render_template(
        "audit_pdf.html",
        report=report,
        run_id=run_id,
        biz_slug=biz_slug,
        desktop_data_uri=desktop_uri,
        mobile_data_uri=mobile_uri,
    )


def _try_weasyprint(html: str) -> Optional[bytes]:
    """First-choice renderer per contract. Returns PDF bytes or None
    if the runtime isn't usable on this machine.

    Most failures here are "GTK not installed" on Windows. Caught
    broadly because weasyprint surfaces them as several different
    exception types depending on platform.
    """
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        return None
    try:
        return HTML(string=html, base_url=BASE_DIR).write_pdf()
    except Exception:
        # Examples: OSError on Windows (GTK), font lookup failures, etc.
        # Fall back rather than die.
        return None


def _try_playwright(html: str) -> Optional[bytes]:
    """Fallback renderer using already-installed Chromium. Same engine
    as the existing browser "Print to PDF" so output is consistent.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context()
                page = ctx.new_page()
                page.set_content(html, wait_until="domcontentloaded")
                pdf_bytes = page.pdf(
                    format="Letter",
                    margin={"top": "0.6in", "bottom": "0.6in",
                            "left": "0.6in", "right": "0.6in"},
                    print_background=True,
                )
                ctx.close()
                return pdf_bytes
            finally:
                browser.close()
    except Exception:
        return None


def render_audit_pdf(run_id: str, biz_slug: str,
                     force: bool = False) -> str:
    """Render (or return cached) audit.pdf. Returns absolute path.

    Raises:
        FileNotFoundError -- report.json missing (caller decides 404)
        PdfRenderError    -- every renderer failed; route should 503
    """
    out_path = _audit_path(run_id, biz_slug)
    if not force and os.path.exists(out_path) and os.path.getsize(out_path) >= 2048:
        # Cache hit AND sanity-sized; serve from disk
        return out_path

    report = mld.load_report(run_id, biz_slug)
    if not report:
        raise FileNotFoundError(
            f"report.json missing for {run_id}/{biz_slug}")

    html = _build_html(report, run_id, biz_slug)

    pdf_bytes = _try_weasyprint(html)
    renderer = "weasyprint"
    if not pdf_bytes:
        pdf_bytes = _try_playwright(html)
        renderer = "playwright"
    if not pdf_bytes:
        raise PdfRenderError(
            "no PDF renderer available: weasyprint failed (likely GTK "
            "missing) AND Playwright/Chromium failed. Install one of "
            "the two to enable PDF generation."
        )
    if not pdf_bytes.startswith(b"%PDF-"):
        # Defensive: never write a non-PDF to disk under audit.pdf
        raise PdfRenderError(
            f"renderer {renderer!r} returned non-PDF bytes; refusing to "
            "write fake output to disk"
        )
    if len(pdf_bytes) < 2048:
        raise PdfRenderError(
            f"renderer {renderer!r} returned suspiciously small PDF "
            f"({len(pdf_bytes)} bytes); refusing to write")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(pdf_bytes)
    os.replace(tmp, out_path)
    return out_path


def renderer_status() -> dict:
    """Operator probe: which renderers are usable right now?"""
    weasy_ok = False
    weasy_err = None
    try:
        from weasyprint import HTML  # type: ignore
        try:
            HTML(string="<p>x</p>").write_pdf()
            weasy_ok = True
        except Exception as exc:
            weasy_err = f"{type(exc).__name__}: {str(exc)[:160]}"
    except Exception as exc:
        weasy_err = f"import: {type(exc).__name__}: {str(exc)[:160]}"

    pw_ok = False
    pw_err = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(headless=True)
                b.close()
                pw_ok = True
            except Exception as exc:
                pw_err = f"{type(exc).__name__}: {str(exc)[:160]}"
    except Exception as exc:
        pw_err = f"import: {type(exc).__name__}: {str(exc)[:160]}"

    return {
        "weasyprint_ok": weasy_ok,
        "weasyprint_error": weasy_err,
        "playwright_ok": pw_ok,
        "playwright_error": pw_err,
        "preferred": "weasyprint" if weasy_ok else (
            "playwright" if pw_ok else None),
    }
