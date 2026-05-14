"""Money-truth analyzer: scores the current lead pool with the project's own
intel/brain modules and prints a Jay-actionable report.

Pure read. No mutation of leads.json. No network. No deps beyond the repo.
"""
import json
import os
import sys
from collections import Counter, defaultdict

# Make the repo importable when run from _truth/
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import commands
import marko_intel
import marko_brain
import marko_compliance

LEADS_FILE = os.path.join(ROOT, "leads.json")

def main():
    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    leads = data.get("leads", [])
    n = len(leads)

    # 1. Contact-field coverage
    has_phone = sum(1 for l in leads if l.get("phone"))
    has_email = sum(1 for l in leads if l.get("email"))
    has_site  = sum(1 for l in leads if l.get("website"))
    has_owner = sum(1 for l in leads if l.get("owner"))
    has_any   = sum(1 for l in leads if l.get("phone") or l.get("email") or l.get("website"))

    # 2. Niche / city distribution
    niches = Counter(l.get("niche") or "?" for l in leads)
    cities = Counter(f"{l.get('city') or '?'}, {l.get('state') or ''}".strip(", ") for l in leads)
    statuses = Counter(l.get("status") or "?" for l in leads)

    # 3. Junk-name detection (the multi-word smash like "DogGroomingPetFoodInRichmondVA")
    def name_looks_junk(name):
        if not name: return True
        # smashed words = no spaces, or weirdly long token
        tokens = name.split()
        longest = max((len(t) for t in tokens), default=0)
        return longest >= 22 or "," in name and " " not in name

    junk_names = [l for l in leads if name_looks_junk(l.get("name") or "")]

    # 4. Score every lead using the dashboard's own scorer
    scored = []
    for l in leads:
        s = commands.score_lead(l)
        l2 = dict(l)
        l2["_score"] = s["score"]
        l2["_label"] = s["label"]
        l2["_signals"] = s["signals"]
        # Leakage / pain
        leaks = marko_intel.compute_leaks(l2)
        l2["_leaks"] = leaks
        l2["_leak_score"] = (len(leaks["confirmed"]) * 2 + len(leaks["inferred"]))
        l2["_missed"] = marko_intel.estimate_missed_money(l2)
        # Closability
        try:
            l2["_closability"] = marko_brain.closability_score(l2)
        except Exception:
            l2["_closability"] = None
        # First action
        try:
            l2["_action"] = marko_brain.recommended_first_action(l2)
        except Exception:
            l2["_action"] = None
        scored.append(l2)

    labels = Counter(l["_label"] for l in scored)

    # 5. Today's call list — must have phone, not DNC/archived/closed-lost, not no_contact
    DEAD = {"DNC", "ARCHIVED", "CLOSED_LOST"}
    callable_today = [
        l for l in scored
        if l.get("phone") and (l.get("status") or "NEW") not in DEAD
        and not l.get("do_not_contact")
    ]
    callable_today.sort(
        key=lambda l: (
            -(l["_closability"] or 0),
            -l["_leak_score"],
            -l["_score"],
        )
    )

    # 6. Pure-junk rows in CSV terms (would Jay see them and groan?)
    csv_junk = [
        l for l in scored
        if not l.get("phone") and not l.get("email")
        or name_looks_junk(l.get("name") or "")
    ]

    # ------- Report -------
    def bar(n, total, width=20):
        if total == 0: return ""
        filled = int(round(width * n / total))
        return "#" * filled + "." * (width - filled)

    out = []
    p = out.append
    p("=" * 72)
    p("MARKO MONEY-TRUTH — lead pool reality check")
    p("=" * 72)
    p(f"Total leads on disk: {n}")
    p("")
    p("CONTACT COVERAGE (need at least one to outreach today):")
    p(f"  phone   {has_phone:>3}/{n}  {bar(has_phone, n)}")
    p(f"  email   {has_email:>3}/{n}  {bar(has_email, n)}")
    p(f"  website {has_site:>3}/{n}  {bar(has_site, n)}")
    p(f"  owner   {has_owner:>3}/{n}  {bar(has_owner, n)}")
    p(f"  any contact field: {has_any}/{n}")
    p("")
    p("STATUS:")
    for k, v in sorted(statuses.items(), key=lambda x: -x[1]):
        p(f"  {k:<12} {v}")
    p("")
    p("LABEL DISTRIBUTION (scored by marko_intel):")
    for k in ("MONEY", "HOT", "GOOD", "WEAK", "COLD", "DEAD"):
        if k in labels:
            p(f"  {k:<6} {labels[k]:>3}  {bar(labels[k], n)}")
    p("")
    p("NICHE DISTRIBUTION:")
    for k, v in niches.most_common(10):
        p(f"  {k:<20} {v}")
    p("")
    p("CITY DISTRIBUTION:")
    for k, v in cities.most_common(10):
        p(f"  {k:<28} {v}")
    p("")
    p("DATA-QUALITY ISSUES:")
    p(f"  junk-looking names: {len(junk_names)}")
    for jn in junk_names[:5]:
        p(f"    - L{jn.get('id','?')}: {repr((jn.get('name') or '')[:60])}")
    p(f"  rows with no phone AND no email: {sum(1 for l in leads if not l.get('phone') and not l.get('email'))}")
    p(f"  CSV-junk rows (no contact OR junk name): {len(csv_junk)}")
    p("")
    p("CALLABLE TODAY (phone present, not dead): " + str(len(callable_today)))
    p("  TOP 7 — sorted by closability × leakage × score")
    p("  -----------------------------------------------------------------")
    for i, l in enumerate(callable_today[:7], 1):
        nm = (l.get("name") or "?")[:40]
        ph = l.get("phone") or "—"
        em = l.get("email") or "—"
        ni = l.get("niche") or "?"
        ci = l.get("city") or "?"
        st = l.get("status") or "?"
        lab = l["_label"]
        cl  = l.get("_closability")
        leak_n = len(l["_leaks"]["confirmed"]) + len(l["_leaks"]["inferred"])
        miss = l.get("_missed", {})
        miss_lo = miss.get("low", 0) if isinstance(miss, dict) else 0
        miss_hi = miss.get("high", 0) if isinstance(miss, dict) else 0
        act = l.get("_action") or {}
        act_a = act.get("action", "?") if isinstance(act, dict) else "?"
        p(f"  #{i} {l.get('id')}  {lab:<5}  closability={cl}  leaks={leak_n}  miss=${miss_lo}-${miss_hi}/mo")
        p(f"      {nm}  ({ni}, {ci})  status={st}")
        p(f"      phone: {ph}    email: {em}")
        p(f"      next: {act_a}")
        # Show top 2 leakage tags so Jay knows what to say on the call
        for ld in (l["_leaks"]["confirmed"] + l["_leaks"]["inferred"])[:2]:
            p(f"      leak: {ld.get('label')} ({ld.get('basis','')})")
        p("")
    p("BEST VERTICAL TO TARGET FIRST:")
    # Highest avg closability + leaks per niche, weighted by N
    by_niche = defaultdict(list)
    for l in scored:
        if l.get("phone") and l.get("_closability") is not None:
            by_niche[l.get("niche") or "?"].append(l)
    ranked = []
    for niche, ls in by_niche.items():
        avg_cl = sum(x["_closability"] for x in ls) / len(ls)
        avg_lk = sum(x["_leak_score"] for x in ls) / len(ls)
        ranked.append((niche, len(ls), avg_cl, avg_lk))
    ranked.sort(key=lambda r: (-r[2], -r[3], -r[1]))
    for niche, n_, cl, lk in ranked[:5]:
        p(f"  {niche:<20}  n={n_:<3}  avg_closability={cl:.1f}  avg_leakage={lk:.1f}")

    p("")
    p("MONEY-TRUTH VERDICT:")
    pass_ok = True
    reasons = []
    if has_any < n * 0.7:
        pass_ok = False
        reasons.append(f"contact coverage too low ({has_any}/{n} have any contact)")
    if len(callable_today) < 3:
        pass_ok = False
        reasons.append(f"only {len(callable_today)} callable leads — not enough to make money today")
    money_hot = labels.get("MONEY", 0) + labels.get("HOT", 0)
    if money_hot < 1:
        reasons.append("no MONEY/HOT leads — pool quality is mid")
    if len(csv_junk) > n * 0.4:
        pass_ok = False
        reasons.append(f"CSV-junk dominates ({len(csv_junk)}/{n})")
    p(f"  PASS={pass_ok}")
    for r in reasons:
        p(f"  - {r}")

    print("\n".join(out))

if __name__ == "__main__":
    main()
