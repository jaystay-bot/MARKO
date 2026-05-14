# Lessons (carry forward across N's)

- **No fake data, ever.** Verifier banned-needle scans + byte/magic
  checks catch fabrications at build time, not first-customer time.
- **Discovery is pluggable.** Each N can add a source without
  touching the scanner.
- **One file per surface.** Each N adds at most one new route + one
  new card, OR (N007) zero edits to product code.
- **First customer first.** Features are graded by distance to the
  first $50 paid lead.
- **Verifier checks file bytes, not just paths.** Repeat for every
  binary/HTTP artifact. PDF magic + size floor in N006/N007.
- **Outreach must reference an actual observed signal.**
- **Niche-specific weights matter.**
- **Name leak categories for what we ACTUALLY observe.**
- **--targets path beats fake seeding.**
- **In-process Werkzeug beats subprocess** for Playwright-over-Flask
  verifiers. Used in N004, N005, N006, **N007**.
- **Token-gate every operator surface** with the existing pattern.
- **data-test attributes future-proof Playwright selectors.**
- **N005: deterministic FIRST, model SECOND.**
- **N005: page.inner_text("body") is unreliable on long pages** --
  prefer `[data-test="..."]` or `:has-text()` locators.
- **N005: Playwright actionability is stricter than real touch** --
  `dispatch_event("click")` for mobile pointer-event interception.
- **N005: optional integrations need explicit env opt-in + silent
  fallback.**
- **N006: declare deviations IN S1, not after the fact.**
- **N006: real PDF needs magic-byte gate AT EVERY HOP.**
- **N006: lazy-generate + disk cache + ?force=1 reset is the right
  pattern.**
- **N006: 503 honestly beats 200 silently.** Customer-facing buttons
  must never lie.
- **N007: pure-observer verifiers are a real product.** A watcher
  that NEVER edits product code (verified by `git status` + per-route
  hash checks) lets coding agents iterate without breaking the
  observer/observed boundary.
- **N007: hash report.json before AND after PDF generation.** This
  catches a hypothetical write-back bug at the data layer that pure
  HTTP-status checks would miss. The principle generalizes:
  whenever a verifier triggers a side-effect-prone route, hash the
  upstream data file before/after.
- **N007: failure-only screenshot capture keeps logs honest.** Don't
  capture an evidence PNG on every successful run -- only when
  something breaks. Dashboards that produce noise during health
  obscure the actual failure signal.
- **N007: status log = JSON-per-line, append-only.** Trivial to
  `tail -f` (or `Get-Content -Wait` on Windows), trivial to grep,
  no schema migration ever needed.
- **N007: `--watch + --visible` is the operator's friend.** A real
  Chromium window looping checks every N seconds while Jay codes
  catches regressions at the moment they're introduced, not at the
  next deploy.
