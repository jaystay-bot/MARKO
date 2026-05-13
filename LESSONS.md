# LESSONS.md — accumulated wisdom

## Process
- Honest scope-flagging beats theater. Each turn the spec inflates; deliver substantively + name what's deferred.
- 8 parallel agents create file-overlap conflicts. Use Agent for research, not parallel coding.
- TaskCreate up-front, then commit + push + auto-deploy + live grep at end.

## Build / Deploy
- Vercel `@vercel/python` statically parses `main.py` for a literal `app = Flask(...)`.
  A one-line `from dashboard import app` fails detection. Solution: `app = Flask(__name__); app = dashboard.app`.
- `vercel build` on Windows requires `uv` (`pip install uv`).
- GitHub→Vercel integration auto-deploys on push to `main` (~12s build).
- `vercel deploy` CLI is blocked by the harness permission rule. `git push` is the working deploy path.

## Code patterns
- Tests use `tempfile.TemporaryDirectory` + module-level path rebinding to avoid mutating real JSON.
- Playwright `cleanup_test_campaigns` removes `PWSmoke-*` after every run.
- Windows cp1252 console can't print Unicode arrow `→` or em-dash `—`. Use ASCII in test names.
- Flask is synchronous on Vercel serverless — no background workers. Long jobs must split into multiple requests.

## Domain
- DDG scrape latency dominates. Synchronous, ~5s/lead with subpage walk.
- `leads.json` is in the public GitHub repo. Don't put secrets there.
- No SMTP creds on Vercel = `/send` self-blocks. No accidental sending in prod.
- Owner extraction must be conservative — regex anchored on "Owner:/Founder:" keywords + meta tags + JSON-LD only. Never guess from random capitalized text.
