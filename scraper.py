"""MARKO Scraper - Lead collection utilities."""
import csv
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
PHONE_REGEX = r'(?<!\d)\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)'

# Filter lists
SKIP_URLS = ['reddit', 'yelp', 'top10', 'blog', 'article', 'wikipedia', 'facebook', 'twitter', 'instagram', 'linkedin', 'pinterest', 'tiktok', 'youtube']
SKIP_EMAILS = ['wix', 'sentry', 'noreply', 'no-reply', 'example.com', 'test', 'admin@', 'info@wix', 'support@wix', 'domain.com', 'placeholder', 'you@', 'user@', 'email@', 'name@']
SKIP_TITLES = ['top 10', 'top 5', 'top 20', 'best ', 'directory', ' near ', 'r/', 'reddit', 'how to', 'what is', 'guide', 'review']


def load_leads():
    with open(LEADS_FILE, "r") as f:
        return json.load(f)


def save_leads(data):
    with open(LEADS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_junk_url(url):
    """Check if URL is from a junk source."""
    url_lower = url.lower()
    return any(skip in url_lower for skip in SKIP_URLS)


def is_junk_title(title):
    """Check if title indicates a list/article page, not a business."""
    title_lower = title.lower()
    return any(skip in title_lower for skip in SKIP_TITLES)


def is_valid_email(email):
    """Check if email is valid (not junk)."""
    if not email:
        return False
    email_lower = email.lower()
    return not any(skip in email_lower for skip in SKIP_EMAILS)


def extract_contact_from_url(url):
    """Extract email and phone from a webpage."""
    email = None
    phone = None
    try:
        resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        text = resp.text
        emails = re.findall(EMAIL_REGEX, text)
        phones = re.findall(PHONE_REGEX, text)
        for e in emails:
            if is_valid_email(e):
                email = e
                break
        if phones:
            phone = phones[0]
    except:
        pass
    return email, phone


def get_contact_type(email, phone):
    """Determine contact type."""
    if email and phone:
        return "both"
    elif email:
        return "email"
    elif phone:
        return "phone"
    return None


def is_duplicate(leads, name, city):
    """Check if lead already exists by name and city."""
    for lead in leads:
        if lead.get("name", "").lower() == name.lower() and lead.get("city", "").lower() == city.lower():
            return True
    return False


def scrape(niche, city, state, max_results=20):
    """Scrape leads for a niche in a location."""
    query = f"{niche} in {city}, {state}"
    print(f"Searching: {query}")

    data = load_leads()
    leads = data.get("leads", [])
    added = 0
    skipped_junk = 0
    skipped_no_contact = 0
    skipped_dupe = 0

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"Search error: {e}")
        return

    print(f"Found {len(results)} results, filtering...")

    for r in results:
        title = r.get("title", "").strip()
        url = r.get("href", "")

        if not title:
            continue

        # Filter junk URLs
        if is_junk_url(url):
            skipped_junk += 1
            continue

        # Filter list/article pages
        if is_junk_title(title):
            skipped_junk += 1
            continue

        # Clean business name
        name = title.split(" - ")[0].split(" | ")[0].split(" :: ")[0].strip()

        # Skip if duplicate
        if is_duplicate(leads, name, city):
            skipped_dupe += 1
            continue

        print(f"  Checking: {name[:40]}...")
        email, phone = extract_contact_from_url(url) if url else (None, None)

        # Must have at least one contact method
        contact_type = get_contact_type(email, phone)
        if not contact_type:
            skipped_no_contact += 1
            print(f"    - Skipped: no contact info")
            continue

        new_id = f"L{len(leads) + 1:03d}"
        lead = {
            "id": new_id,
            "name": name,
            "city": city,
            "state": state,
            "website": url,
            "source_url": url,
            "phone": phone,
            "email": email,
            "contact_type": contact_type,
            "niche": niche,
            "status": "NEW",
            "source": "scrape"
        }

        leads.append(lead)
        added += 1
        print(f"    + Added [{contact_type}]: {email or ''} {phone or ''}")

    data["leads"] = leads
    save_leads(data)

    print(f"\n=== RESULTS ===")
    print(f"Added: {added}")
    print(f"Skipped (junk): {skipped_junk}")
    print(f"Skipped (no contact): {skipped_no_contact}")
    print(f"Skipped (duplicate): {skipped_dupe}")


def add_lead_from_source(name, email, niche, source="manual"):
    """Add a lead from any source."""
    data = load_leads()
    leads = data.get("leads", [])

    new_id = f"L{len(leads) + 1:03d}"
    lead = {
        "id": new_id,
        "name": name,
        "email": email,
        "niche": niche,
        "source": source,
        "status": "NEW"
    }

    leads.append(lead)
    data["leads"] = leads
    save_leads(data)
    return lead


def import_leads_from_list(lead_list, niche):
    """Import multiple leads from a list of (name, email) tuples."""
    added = 0
    for name, email in lead_list:
        add_lead_from_source(name, email, niche, source="import")
        added += 1
    return added


CSV_FILE = os.path.join(BASE_DIR, "scraper_results.csv")


def fetch_site(url):
    """Fetch a single site and return title, status, SSL, email, phone."""
    if not url.startswith("http"):
        url = "https://" + url
    ssl = "yes" if url.startswith("https") else "no"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        status = resp.status_code
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else "N/A"
        text = resp.text
        emails = [e for e in re.findall(EMAIL_REGEX, text) if is_valid_email(e)]
        phones = re.findall(PHONE_REGEX, text)
        email = emails[0] if emails else "none"
        phone = phones[0] if phones else "none"
    except Exception as e:
        title = "ERROR"
        status = str(e)
        email = "none"
        phone = "none"
    return {
        "title": title,
        "status": status,
        "ssl": ssl,
        "email": email,
        "phone": phone,
    }


def run_report(input_csv):
    """Read input CSV, fetch each site, print results, and save to scraper_results.csv."""
    rows = []
    with open(input_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        print("No rows found in input CSV.")
        return

    results = []
    print("=" * 70)
    print("MARKO SCRAPER — SITE REPORT")
    print("=" * 70)

    for row in rows:
        name = row.get("name", "").strip()
        website = row.get("website", "").strip()
        if not website:
            continue
        info = fetch_site(website)
        result = {
            "name": name,
            "website": website,
            "title": info["title"],
            "status": info["status"],
            "ssl": info["ssl"],
            "email": info["email"],
            "phone": info["phone"],
        }
        results.append(result)
        print(f"\nName:    {name}")
        print(f"Website: {website}")
        print(f"Title:   {info['title']}")
        print(f"Status:  {info['status']}")
        print(f"SSL:     {info['ssl']}")
        print(f"Email:   {info['email']}")
        print(f"Phone:   {info['phone']}")
        print("-" * 70)

    fieldnames = ["name", "website", "title", "status", "ssl", "email", "phone"]
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nResults saved to {CSV_FILE}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <input.csv>")
        sys.exit(1)
    run_report(sys.argv[1])
