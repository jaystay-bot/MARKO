"""Visible Playwright drive of the money flow.

Loads dashboard -> verifies scrape form, leads table, Money Mode card ->
downloads CSV via the UI -> reloads to confirm persistence -> captures
proof screenshots and a summary JSON.

Read-only against leads.json by default; passes --scrape to actually
submit the scrape form (slow, network).
"""
import argparse
import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT  = os.path.dirname(__file__)
URL  = "http://127.0.0.1:5000"


def jdump(d):
    return json.dumps(d, indent=2, default=str)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrape", action="store_true",
                    help="Actually submit the scrape form (network).")
    ap.add_argument("--niche", default="movers")
    ap.add_argument("--city",  default="Richmond")
    ap.add_argument("--state", default="VA")
    ap.add_argument("--max",   default="5")
    ap.add_argument("--headed", action="store_true", default=True)
    args = ap.parse_args()

    proof = {"steps": [], "errors": [], "console": []}
    def step(name, **kw):
        rec = {"name": name, **kw}
        proof["steps"].append(rec)
        print("STEP:", name, {k: v for k, v in kw.items() if k != 'html'})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=350)
        ctx = browser.new_context(accept_downloads=True,
                                  viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda exc: proof["errors"].append(f"pageerror: {exc}"))
        page.on("console",  lambda msg: proof["console"].append(f"{msg.type}: {msg.text}")
                                          if msg.type in ("error","warning") else None)

        # ---- 1. Load dashboard ----
        resp = page.goto(URL, wait_until="domcontentloaded")
        step("load /", status=resp.status if resp else None,
             title=page.title())
        page.screenshot(path=os.path.join(OUT, "01_dashboard.png"), full_page=True)

        # ---- 2. Verify key sections ----
        scrape_form = page.locator("#scrape-form")
        leads_h = page.locator("#leads h2")
        step("scrape form visible", visible=scrape_form.is_visible())
        leads_h_text = leads_h.inner_text() if leads_h.count() else ""
        m = re.search(r"Leads\s*\((\d+)\)", leads_h_text)
        leads_before = int(m.group(1)) if m else None
        step("leads count rendered", count=leads_before, header=leads_h_text)

        # Confirm at least one lead row exposes a phone/email (truth: contact field visible)
        body_text = page.locator("body").inner_text()
        has_phone_in_dom = bool(re.search(r"\(?8\d{2}\)?[-.\s]\d{3}[-.\s]\d{4}", body_text))
        has_at_email     = "@" in body_text and bool(re.search(r"[\w.+-]+@[\w.-]+\.\w{2,}", body_text))
        step("contact fields visible in DOM",
             phone_in_dom=has_phone_in_dom, email_in_dom=has_at_email)

        # ---- 3. Optional: run a real scrape via the UI ----
        if args.scrape:
            page.fill("#scrape-niche", args.niche)
            page.fill("#scrape-city",  args.city)
            page.fill("#scrape-state", args.state)
            page.fill('input[name="max_results"]', args.max)
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=120000):
                page.click('#scrape-form button[type="submit"]')
            step("scrape submitted",
                 message=(page.locator(".message, .flash, .toast, body").first
                          .inner_text()[:200] if page.locator("body").count() else ""))
            page.screenshot(path=os.path.join(OUT, "02_after_scrape.png"),
                            full_page=True)
            leads_h_text2 = page.locator("#leads h2").inner_text()
            m2 = re.search(r"Leads\s*\((\d+)\)", leads_h_text2)
            leads_after_scrape = int(m2.group(1)) if m2 else None
            step("leads count after scrape",
                 before=leads_before, after=leads_after_scrape)

        # ---- 4. Export CSV via the leads-section button ----
        export_link = page.locator('a[href="/export/leads.csv"]').first
        with page.expect_download() as dl_info:
            export_link.click()
        dl = dl_info.value
        csv_path = os.path.join(OUT, "exported_leads.csv")
        dl.save_as(csv_path)
        size = os.path.getsize(csv_path)
        with open(csv_path, "r", encoding="utf-8") as f:
            csv_lines = f.read().splitlines()
        step("csv exported", path=csv_path, bytes=size,
             rows=max(0, len(csv_lines) - 1),
             header=csv_lines[0] if csv_lines else "")

        # Money-truth on the CSV (callable rows = has phone or email)
        callable_rows = 0
        if len(csv_lines) > 1:
            header_cols = csv_lines[0].split(",")
            try:
                i_email = header_cols.index("email")
                i_phone = header_cols.index("phone")
            except ValueError:
                i_email, i_phone = 3, 4
            for row in csv_lines[1:]:
                # naive split is OK; we wrote with csv module so commas in
                # names are quoted -- count only the contact columns
                # Re-parse with csv to be safe:
                import csv as _csv, io as _io
                reader = _csv.reader(_io.StringIO(row))
                cells = next(reader, [])
                if len(cells) > max(i_email, i_phone):
                    if cells[i_email].strip() or cells[i_phone].strip():
                        callable_rows += 1
        step("csv contact-quality", callable_rows=callable_rows,
             total=max(0, len(csv_lines) - 1))

        # ---- 5. Persistence — reload, count should be stable ----
        page.goto(URL, wait_until="domcontentloaded")
        leads_h_text3 = page.locator("#leads h2").inner_text()
        m3 = re.search(r"Leads\s*\((\d+)\)", leads_h_text3)
        leads_after_reload = int(m3.group(1)) if m3 else None
        step("persistence reload",
             before=leads_before, after_reload=leads_after_reload,
             stable=(leads_before == leads_after_reload))
        page.screenshot(path=os.path.join(OUT, "03_after_reload.png"),
                        full_page=True)

        # ---- 6. Regression touch: open the Mobile Call mode for top lead ----
        # Find first lead-id rendered (e.g. L016) and hit /m/lead/<id>
        ids = re.findall(r"\b(L\d{3})\b", page.content())
        top_id = ids[0] if ids else None
        if top_id:
            page.goto(f"{URL}/m/lead/{top_id}", wait_until="domcontentloaded")
            page.screenshot(path=os.path.join(OUT, "04_mobile_call.png"),
                            full_page=True)
            step("mobile-call mode", lead=top_id,
                 title=page.title(),
                 has_tel=("tel:" in page.content()))

        # ---- 7. Regression touch: /recap (operator daily view) ----
        page.goto(f"{URL}/recap", wait_until="domcontentloaded")
        step("recap loaded", title=page.title())
        page.screenshot(path=os.path.join(OUT, "05_recap.png"), full_page=True)

        time.sleep(1.0)
        browser.close()

    with open(os.path.join(OUT, "drive_result.json"), "w", encoding="utf-8") as f:
        f.write(jdump(proof))
    print("\n=== DRIVE PROOF ===")
    print(jdump(proof))


if __name__ == "__main__":
    main()
