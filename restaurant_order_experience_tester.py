"""
restaurant_order_experience_tester.py
Detects if a restaurant website has NO online ordering path.
"""
import csv
import requests
from bs4 import BeautifulSoup
import sys
import time

INPUT_FILE = "restaurants_sample.csv"
OUTPUT_FILE = "results.csv"

KEYWORDS = ["order", "order online", "pickup", "delivery"]

LINK_TARGETS = [
    "doordash", "ubereats", "grubhub",
    "toasttab", "chownow", "squareup"
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def check_site(name, url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        return "ERROR", f"Request failed: {str(e)[:80]}"

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check visible text
    text = soup.get_text(separator=" ").lower()
    for kw in KEYWORDS:
        if kw in text:
            return "ORDERING AVAILABLE", f'Found keyword: "{kw}"'

    # Check all href links
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        for target in LINK_TARGETS:
            if target in href:
                return "ORDERING AVAILABLE", f'Found link to: {target}'

    return "NO ORDERING PATH FOUND", "No ordering keywords or links detected"


def run():
    input_file = sys.argv[1] if len(sys.argv) > 1 else INPUT_FILE

    rows = []
    try:
        with open(input_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except FileNotFoundError:
        print(f"[ERROR] Input file not found: {input_file}")
        sys.exit(1)

    results = []
    total = len(rows)

    print(f"\nStarting scan — {total} restaurant(s)\n{'─'*50}")

    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        website = row.get("website", "").strip()

        if not website:
            status, notes = "ERROR", "No website provided"
        else:
            status, notes = check_site(name, website)

        tag = "\U0001f534" if status == "NO ORDERING PATH FOUND" else ("\u2705" if status == "ORDERING AVAILABLE" else "\u26a0\ufe0f")
        print(f"[{i}/{total}] {tag} {name}")
        print(f"        {status} — {notes}")

        results.append({
            "name": name,
            "website": website,
            "status": status,
            "notes": notes
        })

        time.sleep(0.5)  # polite delay

    print(f"\n{'─'*50}")
    print(f"Scan complete. Writing {OUTPUT_FILE}...\n")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "website", "status", "notes"])
        writer.writeheader()
        writer.writerows(results)

    no_order = sum(1 for r in results if r["status"] == "NO ORDERING PATH FOUND")
    available = sum(1 for r in results if r["status"] == "ORDERING AVAILABLE")
    errors = sum(1 for r in results if r["status"] == "ERROR")

    print(f"Results:")
    print(f"  \U0001f534 NO ORDERING PATH : {no_order}")
    print(f"  \u2705 ORDERING AVAILABLE: {available}")
    print(f"  \u26a0\ufe0f ERRORS            : {errors}")
    print(f"\nSaved \u2192 {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
