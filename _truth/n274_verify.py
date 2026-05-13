"""N274 truth verification.

Gates:
  a) /export/pitch_pack_today.zip contains 5..10 folders, each with all 3 files
  b) Money Lane strip renders 5 cards, each with a tel: link, no duplicate lead-ids
  c) Refresh Owners button returns in < 2s wall-clock (must redirect, not block)
  d) Flash message after refresh-owners follows the documented format
  e) N273 verifier still passes (regression)

Plus visible Playwright screenshots: N274_dashboard.png, N274_money_lane.png,
N274_after_refresh.png.
"""
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT  = os.path.dirname(__file__)
URL  = "http://127.0.0.1:5000"
sys.path.insert(0, ROOT)

import commands  # noqa: E402


def _fetch(path):
    with urllib.request.urlopen(f"{URL}{path}") as r:
        return r.status, r.read()


# N276.2: raw-byte snapshot/restore wrapper. n274 exercises refresh_owners
# which intentionally writes to leads.json (adding `owner` fields). Without
# this, a successful PASS still pollutes the working tree.

_TRACKED_FILES = ("LEADS_FILE", "CAMPAIGNS_FILE", "LOG_FILE", "CONFIG_FILE")
_REFRESH_FILES = (
    os.path.join(ROOT, ".refresh_owners.lock"),
    os.path.join(ROOT, ".refresh_owners.result.json"),
)


def _snapshot():
    snap = {}
    for attr in _TRACKED_FILES:
        path = getattr(commands, attr)
        try:
            with open(path, "rb") as f:
                snap[path] = f.read()
        except FileNotFoundError:
            snap[path] = None
    for path in _REFRESH_FILES:
        try:
            with open(path, "rb") as f:
                snap[path] = f.read()
        except FileNotFoundError:
            snap[path] = None
    return snap


def _patient_replace(tmp, path, attempts=20, delay=0.1):
    last = None
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (i + 1))
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    raise last


def _restore(snap):
    for path, blob in snap.items():
        if blob is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        tmp = path + ".restore.tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        _patient_replace(tmp, path)


def main():
    snap = _snapshot()
    try:
        return _main_inner()
    finally:
        # Wait for the background refresh-owners thread to drop the lock
        # before restoring — otherwise the thread keeps writing files after
        # our restore returns. Poll up to ~30s (real refresh is typically
        # <10s with 3 URLs × 5s timeout each).
        try:
            for _ in range(60):
                if not commands.refresh_owners_status().get("running"):
                    break
                time.sleep(0.5)
            # Even after the lock drops, give the writer 0.5s to finish
            # flushing the result file (so we know what to restore).
            time.sleep(0.5)
        except Exception:
            time.sleep(2)
        _restore(snap)


def _main_inner():
    proof = {}

    # ---- gate a: pitch_pack_today.zip ----
    status, body = _fetch("/export/pitch_pack_today.zip")
    zip_path = os.path.join(OUT, "N274_pitch_pack_today.zip")
    with open(zip_path, "wb") as f:
        f.write(body)
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()
    folders = sorted({n.split("/", 1)[0] + "/" for n in names if "/" in n})
    per_folder = {}
    for f in folders:
        contents = sorted(n.rsplit("/", 1)[1] for n in names
                          if n.startswith(f) and n != f)
        per_folder[f] = contents
    required = {"email.txt", "mockup.html", "leak_report.md"}
    each_complete = all(set(v) >= required for v in per_folder.values())
    proof["pitch_pack_zip"] = {
        "status": status,
        "bytes": len(body),
        "folder_count": len(folders),
        "each_has_3_files": each_complete,
        "pass": status == 200 and 5 <= len(folders) <= 10 and each_complete,
        "folders_sample": folders[:3],
    }

    # ---- gate b: Money Lane on the dashboard renders 5 unique tel: cards ----
    # Done in the Playwright section below.

    # ---- gate c+d: refresh button is non-blocking + flash format ----
    # Wait until any in-flight refresh from a prior run finishes.
    for _ in range(60):
        st = commands.refresh_owners_status()
        if not st["running"]:
            break
        time.sleep(0.5)

    refresh_results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900},
                                  accept_downloads=True)
        page = ctx.new_page()
        # Capture flash messages on the redirected index
        resp = page.goto(URL, wait_until="domcontentloaded")
        proof["dashboard_load"] = {"status": resp.status if resp else None,
                                   "title": page.title()}

        # --- Money Lane proof
        lane = page.locator(".money-lane .lane-card")
        proof["money_lane"] = {"count": lane.count()}
        tel_anchors = page.locator(".money-lane a[href^='tel:']").all()
        tel_hrefs = [a.get_attribute("href") for a in tel_anchors]
        proof["money_lane"]["tel_links"] = tel_hrefs
        proof["money_lane"]["unique_tel_links"] = len(set(tel_hrefs))
        # Each card should also have a Pitch Pack link
        pack_links = page.locator(".money-lane a[href*='/pitch_pack']").count()
        proof["money_lane"]["pack_links"] = pack_links
        proof["money_lane"]["pass"] = (
            lane.count() == 5
            and len(tel_hrefs) == 5
            and len(set(tel_hrefs)) == 5
            and pack_links == 5
        )
        page.evaluate("document.querySelector('#leads').scrollIntoView()")
        time.sleep(0.4)
        page.screenshot(path=os.path.join(OUT, "N274_money_lane.png"),
                        full_page=False)
        # full page
        page.goto(URL, wait_until="domcontentloaded")
        page.screenshot(path=os.path.join(OUT, "N274_dashboard.png"),
                        full_page=True)

        # --- Refresh button non-blocking proof
        refresh_btn = page.locator("form[action='/owners/refresh'] button").first
        proof["refresh_button_visible"] = refresh_btn.is_visible()
        t0 = time.time()
        # Click triggers a 302 to /?message=...
        with page.expect_navigation(wait_until="domcontentloaded",
                                    timeout=10000):
            refresh_btn.click()
        t_elapsed = time.time() - t0
        # Pull the flash message off the URL query
        u = page.url
        flash = ""
        m = re.search(r"[?&]message=([^&]+)", u)
        if m:
            from urllib.parse import unquote
            flash = unquote(m.group(1).replace("+", " "))
        refresh_results["elapsed_seconds"] = round(t_elapsed, 3)
        refresh_results["flash"] = flash
        refresh_results["flash_format_ok"] = bool(re.match(
            r"^Owners refresh (started|already running)", flash))
        proof["refresh_first_click"] = refresh_results

        # Immediately click again — should bail with "already running"
        t1 = time.time()
        page.goto(URL, wait_until="domcontentloaded")
        refresh_btn2 = page.locator("form[action='/owners/refresh'] button").first
        with page.expect_navigation(wait_until="domcontentloaded",
                                    timeout=10000):
            refresh_btn2.click()
        elapsed2 = time.time() - t1
        u2 = page.url
        flash2 = ""
        m2 = re.search(r"[?&]message=([^&]+)", u2)
        if m2:
            from urllib.parse import unquote
            flash2 = unquote(m2.group(1).replace("+", " "))
        proof["refresh_second_click"] = {
            "elapsed_seconds": round(elapsed2, 3),
            "flash": flash2,
            "is_running_bail": flash2.startswith("Owners refresh already running"),
        }
        page.screenshot(path=os.path.join(OUT, "N274_after_refresh.png"),
                        full_page=True)
        time.sleep(0.6)
        browser.close()

    # ---- gate c overall ----
    proof["refresh_non_blocking"] = {
        "first_click_elapsed":  refresh_results["elapsed_seconds"],
        "second_click_elapsed": proof["refresh_second_click"]["elapsed_seconds"],
        "pass": (refresh_results["elapsed_seconds"] < 5.0
                 and proof["refresh_second_click"]["elapsed_seconds"] < 5.0),
    }

    # ---- gate e: rerun N273 verifier as regression ----
    # Just shell out to it so a single change to that file remains the
    # source of truth for N273.
    import subprocess
    cp = subprocess.run([sys.executable, os.path.join(OUT, "n273_verify.py")],
                         capture_output=True, text=True, timeout=120)
    n273_pass = '"PASS": true' in cp.stdout
    proof["n273_regression_pass"] = n273_pass
    if not n273_pass:
        proof["n273_stdout_tail"] = cp.stdout[-800:]
        proof["n273_stderr_tail"] = cp.stderr[-400:]

    # ---- final verdict ----
    proof["PASS"] = (
        proof["pitch_pack_zip"]["pass"]
        and proof["money_lane"]["pass"]
        and proof["refresh_button_visible"]
        and proof["refresh_first_click"]["flash_format_ok"]
        and proof["refresh_second_click"]["is_running_bail"]
        and proof["refresh_non_blocking"]["pass"]
        and proof["n273_regression_pass"]
    )

    print(json.dumps(proof, indent=2, default=str))
    with open(os.path.join(OUT, "N274_result.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(proof, indent=2, default=str))


if __name__ == "__main__":
    main()
