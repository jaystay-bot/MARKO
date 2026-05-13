"""MARKO Scraper - Lead collection utilities."""
import csv
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

import commands  # for is_duplicate_lead

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
PHONE_REGEX = r'(?<!\d)\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)'

SUBPAGES = ['/contact', '/contact-us', '/about', '/about-us', '/services']

# Filter lists
SKIP_URLS = ['reddit', 'yelp', 'top10', 'blog', 'article', 'wikipedia', 'facebook', 'twitter', 'instagram', 'linkedin', 'pinterest', 'tiktok', 'youtube']
SKIP_EMAILS = ['wix', 'sentry', 'noreply', 'no-reply', 'example.com', 'test', 'admin@', 'info@wix', 'support@wix', 'domain.com', 'placeholder', 'you@', 'user@', 'email@', 'name@']
SKIP_TITLES = ['top 10', 'top 5', 'top 20', 'best ', 'directory', ' near ', 'r/', 'reddit', 'how to', 'what is', 'guide', 'review']


def load_leads():
    # N271: route through commands.load_json (which delegates to storage).
    return commands.load_json(LEADS_FILE)


def save_leads(data):
    # N083: route through commands.save_json so the write is atomic.
    commands.save_json(LEADS_FILE, data)


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


def _same_domain_subpages(url):
    """Return same-domain subpage URLs to try, given a starting URL."""
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return []
        base = f"{p.scheme}://{p.netloc}"
    except Exception:
        return []
    return [base + sp for sp in SUBPAGES]


def _extract_from_text(text):
    email = None
    phone = None
    for e in re.findall(EMAIL_REGEX, text):
        if is_valid_email(e):
            email = e
            break
    phones = re.findall(PHONE_REGEX, text)
    if phones:
        phone = phones[0]
    return email, phone


def extract_contact_from_url(url):
    """Extract contact + intel from a business homepage and same-domain subpages.

    Walks /contact, /contact-us, /about, /about-us, /services. For each page that
    returns 200, parses for email, phone, owner name, and operator pain-point tags.
    Aggregates across pages -- the union of pain-points and the first found owner.
    Stops walking once we have both email and phone (owner/tags still aggregate
    from already-fetched pages).

    Returns: (email, phone, owner, pain_points)
    """
    email = None
    phone = None
    owner = None
    pain_tags = []
    seen = set()
    candidates = [url] + _same_domain_subpages(url)

    for page in candidates:
        if not page or page in seen:
            continue
        seen.add(page)
        try:
            resp = requests.get(page, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            status = getattr(resp, "status_code", 0)
            if status != 200:
                continue
            text = resp.text
            e, p = _extract_from_text(text)
        except Exception:
            continue
        if not email and e:
            email = e
        if not phone and p:
            phone = p
        if not owner:
            o = commands.extract_owner_from_html(text)
            if o:
                owner = o
        # Only run pain-point analysis on the homepage (cheapest signal,
        # subpages would just re-detect the same site-level issues)
        if page == url and not pain_tags:
            pain_tags = commands.pain_points_from_html(text, page, status)
        if email and phone:
            # we have full contact; subpages won't add more, stop
            break

    return email, phone, owner, pain_tags


def get_contact_type(email, phone):
    """Determine contact type."""
    if email and phone:
        return "both"
    elif email:
        return "email"
    elif phone:
        return "phone"
    return None




def _active_campaign_id():
    # N271: route through storage abstraction. The os.path.exists guard
    # is dropped because the kv backend doesn't have a filesystem; we
    # rely on FileNotFoundError handling instead.
    try:
        data = commands.load_json(CAMPAIGNS_FILE)
        for c in data.get("campaigns", []):
            if c.get("status") == "ACTIVE":
                return c["id"]
    except FileNotFoundError:
        return None
    except Exception:
        pass
    return None


def _log(entry):
    # N271: route through storage abstraction. Missing log file = silent
    # noop (same as before; this is just activity-log best-effort).
    try:
        data = commands.load_json(LOG_FILE)
        data.setdefault("log", []).append({"timestamp": datetime.now().isoformat(), **entry})
        commands.save_json(LOG_FILE, data)
    except FileNotFoundError:
        return
    except Exception:
        pass


def scrape(niche, city, state, max_results=20):
    """Scrape leads for a niche in a location. Returns added count."""
    query = f"{niche} in {city}, {state}"
    print(f"Searching: {query}")

    data = load_leads()
    leads = data.get("leads", [])
    campaign_id = _active_campaign_id()
    added = 0
    skipped_junk = 0
    skipped_no_contact = 0
    skipped_dupe = 0

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"Search error: {e}")
        _log({"action": "scrape", "niche": niche, "city": city, "state": state,
              "added": 0, "error": str(e)})
        return 0

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

        # Quick dedupe pre-fetch: website domain or name+city already present
        if commands.is_duplicate_lead(leads, name=name, website=url, city=city):
            skipped_dupe += 1
            continue

        print(f"  Checking: {name[:40]}...")
        if url:
            email, phone, owner, pain_points = extract_contact_from_url(url)
        else:
            email, phone, owner, pain_points = (None, None, None, [])

        # Must have at least one contact method
        contact_type = get_contact_type(email, phone)
        if not contact_type:
            skipped_no_contact += 1
            print(f"    - Skipped: no contact info")
            continue

        # Post-fetch dedupe: same email/phone as an existing lead
        if commands.is_duplicate_lead(leads, name=name, email=email, phone=phone, website=url, city=city):
            skipped_dupe += 1
            continue

        new_id = f"L{len(leads) + 1:03d}"
        lead = {
            "id": new_id,
            "name": name,
            "owner": owner,
            "city": city,
            "state": state,
            "website": url,
            "source_url": url,
            "phone": phone,
            "email": email,
            "contact_type": contact_type,
            "niche": niche,
            "status": "NEW",
            "source": "scrape",
            "campaign_id": campaign_id,
            "created_at": datetime.now().isoformat(),
            "pain_points": pain_points,
        }

        leads.append(lead)
        added += 1
        owner_note = f" owner={owner}" if owner else ""
        tags_note = f" tags={pain_points}" if pain_points else ""
        print(f"    + Added [{contact_type}]: {email or ''} {phone or ''}{owner_note}{tags_note}")

    data["leads"] = leads
    save_leads(data)

    _log({"action": "scrape", "niche": niche, "city": city, "state": state,
          "added": added, "skipped_junk": skipped_junk,
          "skipped_no_contact": skipped_no_contact, "skipped_dupe": skipped_dupe,
          "campaign_id": campaign_id})

    print(f"\n=== RESULTS ===")
    print(f"Added: {added}")
    print(f"Skipped (junk): {skipped_junk}")
    print(f"Skipped (no contact): {skipped_no_contact}")
    print(f"Skipped (duplicate): {skipped_dupe}")
    return added


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
        "status": "NEW",
        "campaign_id": _active_campaign_id(),
        "created_at": datetime.now().isoformat(),
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
