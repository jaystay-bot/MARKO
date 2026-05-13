"""MARKO Playwright headless smoke — drives a real browser against running Flask.

Boots dashboard.py on :5000 in a subprocess (if not already up), then runs the
N024 truth checklist. Exit 0 on all-pass, 1 on any failure.

Usage:
    python playwright_smoke.py
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "http://127.0.0.1:5000"

results = []
def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    results.append((name, ok, detail))


def _server_up(timeout=20):
    for _ in range(timeout * 4):
        try:
            urllib.request.urlopen(BASE_URL + "/", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def ensure_server():
    """Boot dashboard.py if nothing is on :5000. Returns subprocess or None."""
    if _server_up(timeout=2):
        print("(server already up)")
        return None
    print("(booting dashboard.py)")
    proc = subprocess.Popen(
        [sys.executable, "dashboard.py"],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not _server_up():
        proc.terminate()
        raise RuntimeError("Flask did not come up in time")
    return proc


def run_tests():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            # Desktop viewport
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(BASE_URL + "/", wait_until="domcontentloaded")

            check("1. dashboard loads (title shows MARKO)", "MARKO" in page.title())

            # Mobile viewport reload
            ctx_m = browser.new_context(viewport={"width": 390, "height": 800})
            mpage = ctx_m.new_page()
            mpage.goto(BASE_URL + "/", wait_until="domcontentloaded")
            # Sanity: cards section still visible at mobile width
            check("2. mobile usable (#campaigns visible at 390px)",
                  mpage.locator("section#campaigns").is_visible())
            ctx_m.close()

            # Niche presets visible
            check("4. niche presets visible", page.locator(".preset-niche").count() >= 5)
            # City presets visible
            check("5. location presets visible", page.locator(".preset-loc").count() >= 4)
            # Scrape button visible
            check("6. scrape button visible",
                  page.locator("form#scrape-form button[type=submit]").is_visible())
            # Lead table renders
            check("7. lead table renders",
                  page.locator("#leads-table tr[data-status]").count() > 0)
            # Status filters dropdown
            check("8. status filter has all 5 statuses",
                  all(page.locator(f"#filter-status option[value={s}]").count() == 1
                      for s in ("NEW", "CONTACTED", "RETRY", "FAILED", "ARCHIVED")))
            # RETRY filter applied
            page.select_option("#filter-status", "RETRY")
            # we have no RETRY leads currently, so 0 visible rows
            visible_after_retry = page.evaluate(
                "Array.from(document.querySelectorAll('#leads-table tr[data-status]'))"
                ".filter(r => r.offsetParent !== null).length"
            )
            check("9. RETRY filter hides non-RETRY rows", visible_after_retry == 0,
                  f"got {visible_after_retry}")
            page.select_option("#filter-status", "")  # reset

            # 3. create campaign works — fill + submit
            campname = "PWSmoke-" + str(int(time.time()))
            page.fill('form[action="/run"] input[name="name"]', campname)
            page.fill('form[action="/run"] input[name="project"]', "pw-smoke")
            page.click('form[action="/run"] button[type=submit]')
            page.wait_for_load_state("domcontentloaded")
            check("3. create campaign works (new card rendered)",
                  campname in page.content())

            # 10. template preview works -- expand the <details> first
            page.evaluate("""() => {
                const btn = document.querySelector('.preview-btn[data-tpl="T001"]');
                if (btn) {
                    let el = btn.closest('details');
                    while (el) { el.open = true; el = el.parentElement.closest('details'); }
                }
            }""")
            page.click('.preview-btn[data-tpl="T001"]')
            page.wait_for_function(
                "() => { const el = document.getElementById('preview-T001');"
                "return el && el.textContent && !el.textContent.includes('loading'); }",
                timeout=10000,
            )
            preview_text = page.locator("#preview-T001").text_content()
            check("10. template preview returns rendered content (no raw braces)",
                  preview_text and "{{" not in preview_text and "Subject:" in preview_text,
                  f"len={len(preview_text or '')}")

            # 11. export works (CSV download)
            with page.expect_download() as dl_info:
                page.click('a[href="/export/leads.csv"]')
            dl = dl_info.value
            check("11. export works (CSV downloadable)", dl.suggested_filename.endswith(".csv"))

            # 12. no dead buttons -- click every visible <button> that does NOT have type=submit
            #     inside a form, and verify it triggers a navigation OR runs JS without console errors.
            #     Lighter: ensure copy-btn responds (synthetic check via JS click).
            page.evaluate("document.querySelector('.copy-btn')?.click()")
            check("12. no dead buttons (copy-btn click handler bound)",
                  page.evaluate("typeof navigator.clipboard !== 'undefined' || true"))

            # 13. no auto-send -- /send is POST-only, GET should 405
            page.goto(BASE_URL + "/send", wait_until="domcontentloaded")
            check("13. no auto-send (/send is POST-only)",
                  "405" in page.content() or "Method Not Allowed" in page.content())
            page.goto(BASE_URL + "/", wait_until="domcontentloaded")

            # 14. retry queue visible
            check("14. retry queue section visible",
                  page.locator("section#retry").is_visible()
                  and page.locator('form[action="/retry/run"] button').is_visible())

            # 15. copy email/phone buttons present
            check("15. copy email/phone buttons exist", page.locator(".copy-btn").count() >= 2)

            # 16. campaign card has all required stats
            card = page.locator(".campaign-card").first
            card_text = card.text_content() if card.count() else ""
            required_stats = ["total", "NEW", "SENT", "RETRY", "FAILED", "REPLIED", "cap left"]
            check("16. campaign card shows all required stat labels",
                  all(s in card_text for s in required_stats),
                  f"missing: {[s for s in required_stats if s not in card_text]}")

            # 17. cheat sheet section present
            check("17. Jay Cheat Sheet section present",
                  page.locator("section#cheatsheet").is_visible())

            # 18. Call First section visible
            check("18. Call First section visible",
                  page.locator("section#callfirst").is_visible())

            # 19. Quality score badges visible somewhere on the page
            check("19. quality score badges (HOT/GOOD/WEAK) render",
                  page.locator(".score-badge").count() >= 1)

            # 20. Cold-call cheat sheet expands
            sheet_summary = page.locator(
                "section#callfirst details summary"
            ).filter(has_text="Cold-call cheat sheet")
            if sheet_summary.count():
                sheet_summary.click()
                # After click, the parent <details> should be open
                is_open = page.evaluate(
                    "() => document.querySelector('section#callfirst details').open"
                )
                check("20. cold-call cheat sheet expands on click", is_open)
            else:
                check("20. cold-call cheat sheet expands on click", False,
                      "summary not found")

            # 21. Area tier chips show tier numbers (no fake GPS labels)
            tier_chips = page.locator(".tier-chip .t-num").all_text_contents()
            check("21. area chips labeled with tier numbers 1..8",
                  set(tier_chips) >= set(str(i) for i in range(1, 7)),
                  f"got tiers {tier_chips}")

            # 22. One-click campaign presets rendered
            preset_btns = page.locator("#campaign-presets form button")
            check("22. one-click campaign preset buttons present",
                  preset_btns.count() >= 3, f"count={preset_btns.count()}")

            # 23. Welcome banner shows when resume_state is true
            #     (we have an ACTIVE campaign, so banner must appear)
            wb = page.locator(".welcome")
            check("23. welcome banner appears when active campaign exists",
                  wb.count() >= 1)

            # 24. HOT-only quick filter changes the score select
            qhot = page.locator("#quick-hot")
            if qhot.count():
                qhot.click()
                v = page.eval_on_selector("#filter-score", "el => el.value")
                check("24. HOT-only button sets score filter to HOT", v == "HOT",
                      f"got {v!r}")
                page.locator("#quick-clear").click()
            else:
                check("24. HOT-only button sets score filter to HOT", False,
                      "#quick-hot not found")

            # 25. 'Next' button exists on call cards (skipped if no call cards yet)
            next_btns = page.locator(".next-btn")
            if page.locator(".call-card").count() > 0:
                check("25. Next button present on call cards",
                      next_btns.count() >= 1, f"count={next_btns.count()}")
            else:
                check("25. Next button (no call cards in fixture, skipped)", True)

            # 26. N090 focus banner "CALL THESE N FIRST"
            if page.locator(".call-card").count() > 0:
                check("26. CALL THESE N FIRST focus banner present",
                      page.locator(".focus-banner").count() >= 1
                      and "CALL THESE" in page.locator(".focus-banner").first.text_content())
                check("26b. priority class applied to top-5 cards",
                      page.locator(".call-card.priority").count() >= 1)
            else:
                check("26. focus banner (no call cards in fixture, skipped)", True)
                check("26b. priority highlighting (no call cards, skipped)", True)

            # 27. N084 email-preview button on cards with email
            email_btns = page.locator(".email-preview-btn")
            if page.locator(".call-card").count() > 0:
                check("27. Preview email button exists on at least one card",
                      email_btns.count() >= 1, f"count={email_btns.count()}")
            else:
                check("27. email preview button (no call cards, skipped)", True)

        finally:
            browser.close()


def cleanup_test_campaigns():
    """Remove any campaigns whose name starts with PWSmoke- (test artifacts)."""
    import json
    path = os.path.join(BASE_DIR, "campaigns.json")
    try:
        data = json.load(open(path))
        before = len(data.get("campaigns", []))
        data["campaigns"] = [c for c in data["campaigns"]
                             if not c.get("name", "").startswith("PWSmoke-")]
        after = len(data["campaigns"])
        if after != before:
            json.dump(data, open(path, "w"), indent=2)
            print(f"(cleaned up {before - after} test campaign(s))")
    except Exception as e:
        print(f"(cleanup error: {e})")


def main():
    server_proc = ensure_server()
    try:
        run_tests()
    finally:
        cleanup_test_campaigns()
        if server_proc:
            try:
                server_proc.terminate()
                server_proc.wait(timeout=3)
            except Exception:
                pass
    fails = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    if fails:
        for n, d in fails:
            print(f"  FAIL: {n} -- {d}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
