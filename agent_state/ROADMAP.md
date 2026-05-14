# MARKO Internet Leak Engine — Roadmap

Phase 1 (this N — N001):
  scanner + leak score + screenshots + report + outreach draft
  Discovery source: leads.json filtered by city + niche substring.
  CLI: `python marko_leak_engine.py --city ... --niche ...`

Phase 2 (NEXT_N candidates):
  Web-discovery sources (DuckDuckGo HTML, OSM Overpass, Yellow Pages)
  so city+niche pairs not already in leads.json yield real businesses.

Phase 3:
  Optional local LLM (Ollama) to write more-personal outreach openings
  per scan signature -- still rule-grounded, no "AI" hype.

Phase 4 (gate: first paid customer):
  Cross-link with money_queue.json so leak reports auto-flow into the
  /review queue. Until first $50 lands, this stays separate.

Phase 5+ deferred until cashflow.
