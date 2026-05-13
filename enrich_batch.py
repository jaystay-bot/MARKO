"""MARKO N263: one-off enrichment backfill.

Walks every lead in leads.json that has a website but is missing
pain_points or owner, fetches its homepage (+ standard subpages via
scraper.extract_contact_from_url), and writes the newly-detected
fields back. Existing fields are never overwritten.

Safe-by-default:
    python enrich_batch.py            # dry run, prints what would change
    python enrich_batch.py --write    # actually writes leads.json

Rate-limited to one request burst per lead with a 1.5s gap between
leads so we don't hammer anyone. No new dependencies; uses the same
requests + extractors the scraper already uses.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import commands
import scraper

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
LOG_FILE = os.path.join(BASE_DIR, "marko_log.json")
SLEEP_BETWEEN_LEADS = 1.5  # seconds

# Local guard against the most common owner-extractor false positives we
# see in real scraped HTML. The upstream regex (commands.extract_owner_
# from_html) passes a name as long as it's TitleCase + no corp word, but
# UI chrome like "Background Check" or "Customer Service" passes that
# filter too. Reject if EITHER word is one of these generic site-text
# tokens. Conservative: rejects with very few real-name false negatives.
_OWNER_REJECT_TOKENS = frozenset({
    "Background", "Check", "Customer", "Service", "Services", "Privacy",
    "Policy", "Terms", "Email", "Phone", "Mailing", "Address", "Quote",
    "Live", "Chat", "Get", "Read", "Click", "Order", "Free", "Best",
    "Top", "Information", "Details", "Page", "Site", "Map", "Help",
    "Support", "Account", "Login", "Search", "Submit", "Schedule",
    "Book", "Booking", "Reserve", "Cart", "Checkout", "About", "Contact",
    "Home", "Form", "Number", "Hours", "Open", "Closed",
})


def _is_plausible_owner(name):
    """Reject names where any token is generic site/UI text."""
    if not name:
        return False
    parts = name.split()
    if len(parts) < 2:
        return False
    return not any(p in _OWNER_REJECT_TOKENS for p in parts)


def _needs_enrichment(lead):
    if not lead.get("website"):
        return False
    pain = lead.get("pain_points")
    if pain is None:
        return True
    if isinstance(pain, list) and len(pain) == 0:
        return True
    if not lead.get("owner"):
        return True
    return False


def _merge(lead, owner, pain_tags):
    """Apply enrichment without clobbering existing data. Returns dict of deltas."""
    delta = {}
    if owner and not lead.get("owner") and _is_plausible_owner(owner):
        lead["owner"] = owner
        delta["owner"] = owner
    elif owner and not _is_plausible_owner(owner):
        delta["owner_rejected"] = owner
    if pain_tags:
        existing = lead.get("pain_points")
        if not existing:
            lead["pain_points"] = list(pain_tags)
            delta["pain_points"] = list(pain_tags)
    return delta


def _log(entry):
    # N271: route through storage abstraction.
    try:
        data = commands.load_json(LOG_FILE)
        data.setdefault("log", []).append(
            {"timestamp": datetime.now().isoformat(), **entry}
        )
        commands.save_json(LOG_FILE, data)
    except FileNotFoundError:
        return
    except Exception:
        pass


def run(write=False):
    data = commands.load_json(LEADS_FILE)
    leads = data.get("leads", [])

    todo = [l for l in leads if _needs_enrichment(l)]
    print(f"Considering {len(leads)} leads, {len(todo)} need enrichment.")
    if not todo:
        print("Nothing to do.")
        return

    print(f"Mode: {'WRITE' if write else 'DRY RUN'}\n")
    enriched = 0
    owner_hits = 0
    pain_hits = 0
    errors = 0
    skipped_no_signal = 0

    for i, lead in enumerate(todo, 1):
        url = lead["website"]
        print(f"[{i:2d}/{len(todo)}] {lead.get('id')} {lead.get('name', '')[:48]}")
        print(f"        {url}")
        try:
            _, _, owner, pain_tags = scraper.extract_contact_from_url(url)
        except Exception as exc:
            errors += 1
            print(f"        ERROR: {exc}")
            time.sleep(SLEEP_BETWEEN_LEADS)
            continue

        delta = _merge(lead, owner, pain_tags)
        applied = {k: v for k, v in delta.items() if k != "owner_rejected"}
        if "owner_rejected" in delta:
            print(f"        ! owner candidate rejected: "
                  f"{delta['owner_rejected']!r} (UI chrome / false positive)")
        if not applied:
            skipped_no_signal += 1
            print(f"        - no new owner/pain signal")
        else:
            enriched += 1
            if "owner" in applied:
                owner_hits += 1
                print(f"        + owner = {applied['owner']!r}")
            if "pain_points" in applied:
                pain_hits += 1
                print(f"        + pain_points = {applied['pain_points']}")

        # Be polite to the websites we're crawling.
        if i < len(todo):
            time.sleep(SLEEP_BETWEEN_LEADS)

    print()
    print(f"=== SUMMARY ===")
    print(f"Considered:        {len(todo)}")
    print(f"Enriched (any):    {enriched}")
    print(f"  owner added:     {owner_hits}")
    print(f"  pain_points add: {pain_hits}")
    print(f"No new signal:     {skipped_no_signal}")
    print(f"Fetch errors:      {errors}")

    if write and enriched:
        data["leads"] = leads
        commands.save_json(LEADS_FILE, data)
        print(f"\nWrote {LEADS_FILE}")
        _log({"action": "enrich_batch", "considered": len(todo),
              "enriched": enriched, "owner_hits": owner_hits,
              "pain_hits": pain_hits, "errors": errors})
    elif not write and enriched:
        print(f"\n(dry run -- re-run with --write to persist)")


if __name__ == "__main__":
    write = "--write" in sys.argv[1:]
    run(write=write)
