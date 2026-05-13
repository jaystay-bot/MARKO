"""MARKO N018 smoke tests -- no network, no real SMTP, no real files touched.

Run:   python smoke_test.py
Exit:  0 on all-pass, 1 on any failure.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import commands
import scraper


# ---------- fixture helpers ----------

def _seed(tmp, leads=None, log=None, campaigns=None, config=None):
    paths = {
        "CAMPAIGNS_FILE": os.path.join(tmp, "campaigns.json"),
        "LEADS_FILE": os.path.join(tmp, "leads.json"),
        "CONFIG_FILE": os.path.join(tmp, "config.json"),
        "LOG_FILE": os.path.join(tmp, "marko_log.json"),
        "TEMPLATES_FILE": os.path.join(tmp, "templates.json"),
    }
    json.dump(campaigns or {"campaigns": [{
        "id": "C001", "name": "Test", "project": "p", "status": "ACTIVE",
        "sends": 0, "open_rate": 0, "replies": 0, "signups": 0,
        "verdict": "PENDING", "next": "SEND", "last_action": "",
    }]}, open(paths["CAMPAIGNS_FILE"], "w"))
    json.dump(leads or {"leads": []}, open(paths["LEADS_FILE"], "w"))
    json.dump(config or {
        "batch_size": 10, "sender_name": "tester",
        "smtp": {}, "email_template": {"subject": "s", "body": "b {sender_name}"},
    }, open(paths["CONFIG_FILE"], "w"))
    json.dump(log or {"log": []}, open(paths["LOG_FILE"], "w"))
    json.dump({"outreach": [], "campaign_presets": [], "niche_presets": []},
              open(paths["TEMPLATES_FILE"], "w"))
    # Rebind module-level paths
    for attr, p in paths.items():
        setattr(commands, attr, p)
    scraper.LEADS_FILE = paths["LEADS_FILE"]
    scraper.LOG_FILE = paths["LOG_FILE"]
    scraper.CAMPAIGNS_FILE = paths["CAMPAIGNS_FILE"]
    return paths


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def _fake_get_factory(pages):
    def _get(url, **kw):
        if url in pages:
            return _FakeResp(pages[url], 200)
        return _FakeResp("", 404)
    return _get


# ---------- tests ----------

results = []

def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    results.append((name, ok, detail))


def test_subpage_extraction():
    print("test_subpage_extraction")
    # Homepage has no contact info; /contact has both.
    pages = {
        "https://acme.example/": "<html><body>welcome</body></html>",
        "https://acme.example/contact": "<html>reach us at hello@acme.example or 804-555-1234</html>",
    }
    with mock.patch.object(scraper.requests, "get", side_effect=_fake_get_factory(pages)):
        email, phone = scraper.extract_contact_from_url("https://acme.example/")
    check("email pulled from /contact", email == "hello@acme.example", f"got {email!r}")
    check("phone pulled from /contact", phone == "804-555-1234", f"got {phone!r}")

    # Homepage has email; /about has phone -- both should be aggregated.
    pages2 = {
        "https://b.example/": "<html>contact: hi@b.example</html>",
        "https://b.example/about": "<html>call 555-867-5309</html>",
    }
    with mock.patch.object(scraper.requests, "get", side_effect=_fake_get_factory(pages2)):
        email, phone = scraper.extract_contact_from_url("https://b.example/")
    check("email from homepage aggregated", email == "hi@b.example", f"got {email!r}")
    check("phone from /about aggregated", phone == "555-867-5309", f"got {phone!r}")


def test_dedup():
    print("test_dedup")
    existing = [{
        "id": "L001", "name": "Acme Inc", "email": "Sales@acme.com",
        "phone": "(555) 123-4567", "website": "https://www.acme.com/", "city": "Richmond",
    }]
    check("dedup blocks same website domain (different path/scheme/www)",
          commands.is_duplicate_lead(existing, website="http://acme.com/about"))
    check("dedup blocks same email (case-insensitive)",
          commands.is_duplicate_lead(existing, email="sales@ACME.com"))
    check("dedup blocks same phone (different formatting)",
          commands.is_duplicate_lead(existing, phone="555.123.4567"))
    check("dedup blocks same name + city",
          commands.is_duplicate_lead(existing, name="acme inc", city="richmond"))
    check("dedup allows truly new lead",
          not commands.is_duplicate_lead(existing,
                                         name="Other Co", email="x@y.com",
                                         phone="999-888-7777", website="other.com",
                                         city="NYC"))
    check("dedup does NOT block on email domain alone (different localpart)",
          not commands.is_duplicate_lead(existing, email="different@acme.com"),
          "email domain alone should not over-block")


def test_batch_cap():
    print("test_batch_cap")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": f"L{i:03d}", "name": f"L{i}",
                            "email": f"l{i}@x.com", "status": "NEW", "niche": "p"}
                           for i in range(15)]}
        config = {"batch_size": 25, "sender_name": "tester", "smtp": {},
                  "email_template": {"subject": "s", "body": "b"}}
        _seed(tmp, leads=leads, config=config)
        with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                          "MARKO_SMTP_PASSWORD": "pw"}):
            with mock.patch.object(commands, "send_email",
                                   return_value=(True, None, None)):
                commands.marko_send(dry_run=False)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        contacted = [l for l in after if l["status"] == "CONTACTED"]
        check("batch capped at 10 even when config.batch_size=25",
              len(contacted) == 10, f"got {len(contacted)} contacted")


def test_daily_cap():
    print("test_daily_cap")
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.now().isoformat()
        log = {"log": [{"timestamp": today, "action": "send", "status": "sent",
                        "campaign_id": "C001", "lead_id": f"L{i}"} for i in range(50)]}
        leads = {"leads": [{"id": "L100", "name": "Z", "email": "z@x.com",
                            "status": "NEW", "niche": "p"}]}
        _seed(tmp, leads=leads, log=log)
        with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                          "MARKO_SMTP_PASSWORD": "pw"}):
            result = commands.marko_send(dry_run=False)
        check("daily cap returns BLOCKED message",
              "BLOCKED" in (result or ""), f"got {result!r}")
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("daily cap leaves lead untouched (still NEW)",
              after[0]["status"] == "NEW", f"got {after[0]['status']}")

    # Throttle case: 48 already sent today, batch_size=10 -> only 2 should fire
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.now().isoformat()
        log = {"log": [{"timestamp": today, "action": "send", "status": "sent",
                        "campaign_id": "C001", "lead_id": f"L{i}"} for i in range(48)]}
        leads = {"leads": [{"id": f"L{100+i}", "name": f"L{i}",
                            "email": f"l{i}@x.com", "status": "NEW", "niche": "p"}
                           for i in range(10)]}
        _seed(tmp, leads=leads, log=log)
        with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                          "MARKO_SMTP_PASSWORD": "pw"}):
            with mock.patch.object(commands, "send_email",
                                   return_value=(True, None, None)):
                commands.marko_send(dry_run=False)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        contacted = [l for l in after if l["status"] == "CONTACTED"]
        check("daily-cap throttle sends only remaining slots (48+2=50)",
              len(contacted) == 2, f"got {len(contacted)} contacted")


def test_smtp_transient():
    print("test_smtp_transient")
    for code in (421, 450, 451, 452):
        with tempfile.TemporaryDirectory() as tmp:
            leads = {"leads": [{"id": "L001", "name": "X", "email": "x@x.com",
                                "status": "NEW", "niche": "p"}]}
            _seed(tmp, leads=leads)
            with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                              "MARKO_SMTP_PASSWORD": "pw"}):
                with mock.patch.object(commands, "send_email",
                                       return_value=(False, f"{code} try later", code)):
                    commands.marko_send(dry_run=False)
            after = json.load(open(commands.LEADS_FILE))["leads"]
            check(f"transient SMTP {code} marks RETRY",
                  after[0]["status"] == "RETRY", f"got {after[0]['status']}")


def test_smtp_permanent():
    print("test_smtp_permanent")
    for code in (550, 553, None):
        with tempfile.TemporaryDirectory() as tmp:
            leads = {"leads": [{"id": "L001", "name": "X", "email": "x@x.com",
                                "status": "NEW", "niche": "p"}]}
            _seed(tmp, leads=leads)
            with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                              "MARKO_SMTP_PASSWORD": "pw"}):
                with mock.patch.object(commands, "send_email",
                                       return_value=(False, "rejected", code)):
                    commands.marko_send(dry_run=False)
            after = json.load(open(commands.LEADS_FILE))["leads"]
            check(f"permanent SMTP {code} marks FAILED",
                  after[0]["status"] == "FAILED", f"got {after[0]['status']}")


def test_retry_count_escalation():
    print("test_retry_count_escalation")
    # A lead that fails transient at retry_count=2 -> next call should mark FAILED (rc=3)
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "X", "email": "x@x.com",
                            "status": "NEW", "niche": "p", "retry_count": 2}]}
        _seed(tmp, leads=leads)
        with mock.patch.dict(os.environ, {"MARKO_SMTP_EMAIL": "a@b.com",
                                          "MARKO_SMTP_PASSWORD": "pw"}):
            with mock.patch.object(commands, "send_email",
                                   return_value=(False, "421 try later", 421)):
                commands.marko_send(dry_run=False)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("retry_count escalation: rc=2 + transient -> FAILED (rc=3)",
              after[0]["status"] == "FAILED" and after[0].get("retry_count") == 3,
              f"got status={after[0]['status']}, rc={after[0].get('retry_count')}")


def test_retry_pending_cooldown_and_cap():
    print("test_retry_pending_cooldown_and_cap")
    # Cooldown gate
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime.now()
        old = (now - timedelta(minutes=120)).isoformat()
        fresh = (now - timedelta(minutes=10)).isoformat()
        leads = {"leads": [
            {"id": "L001", "name": "old1", "email": "o1@x.com", "status": "RETRY",
             "retry_count": 1, "last_attempt_at": old},
            {"id": "L002", "name": "fresh", "email": "f@x.com", "status": "RETRY",
             "retry_count": 1, "last_attempt_at": fresh},
        ]}
        _seed(tmp, leads=leads)
        count = commands.retry_pending(cooldown_minutes=60)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        old_lead = next(l for l in after if l["id"] == "L001")
        fresh_lead = next(l for l in after if l["id"] == "L002")
        check("retry_pending resets lead past cooldown",
              old_lead["status"] == "NEW", f"got {old_lead['status']}")
        check("retry_pending skips lead within cooldown",
              fresh_lead["status"] == "RETRY", f"got {fresh_lead['status']}")
        check("retry_pending returns reset count == 1", count == 1, f"got {count}")

    # Retry cap gate -- at MAX_RETRIES -> not reset
    with tempfile.TemporaryDirectory() as tmp:
        old = (datetime.now() - timedelta(minutes=120)).isoformat()
        leads = {"leads": [{"id": "L001", "name": "max", "email": "m@x.com",
                            "status": "RETRY",
                            "retry_count": commands.MAX_RETRIES,
                            "last_attempt_at": old}]}
        _seed(tmp, leads=leads)
        count = commands.retry_pending(cooldown_minutes=60)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("retry_pending does NOT reset lead at MAX_RETRIES",
              after[0]["status"] == "RETRY" and count == 0,
              f"status={after[0]['status']}, count={count}")

    # Daily cap gate -- 50 sends today blocks all retries
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.now().isoformat()
        log = {"log": [{"timestamp": today, "action": "send", "status": "sent",
                        "campaign_id": "C001", "lead_id": f"L{i}"} for i in range(50)]}
        old = (datetime.now() - timedelta(minutes=120)).isoformat()
        leads = {"leads": [{"id": "L100", "name": "x", "email": "x@x.com",
                            "status": "RETRY", "retry_count": 1,
                            "last_attempt_at": old}]}
        _seed(tmp, leads=leads, log=log)
        count = commands.retry_pending(cooldown_minutes=60)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("retry_pending blocked when daily cap hit",
              after[0]["status"] == "RETRY" and count == 0,
              f"status={after[0]['status']}, count={count}")


def test_template_merge_fields():
    print("test_template_merge_fields")
    lead = {"name": "Acme", "city": "Richmond", "state": "VA",
            "owner": "Pat", "phone": "555-1234", "niche": "movers"}
    # Both brace styles
    tpl = "Hi {{owner}} at {business_name} in {{city}}, {state}. Call {{phone}}. – {sender_name}"
    out = commands.personalize_template(tpl, lead, "Jay")
    expected = "Hi Pat at Acme in Richmond, VA. Call 555-1234. – Jay"
    check("personalize_template handles both {x} and {{x}}",
          out == expected, f"got {out!r}")
    # Empty owner falls back to 'there'
    out2 = commands.personalize_template("Hi {{owner}}", {"name": "x"}, "Jay")
    check("personalize_template owner fallback to 'there'",
          out2 == "Hi there", f"got {out2!r}")


def test_score_lead_signals():
    print("test_score_lead_signals")
    # Maxed-out lead -> HOT
    full = {"name": "X", "email": "x@y.com", "phone": "555-1234",
            "website": "https://x.com/", "owner": "Pat", "niche": "movers",
            "city": "Richmond", "state": "VA", "campaign_id": "C001",
            "contact_type": "both", "source": "scrape"}
    s = commands.score_lead(full)
    check("full-signal lead scores HOT >= 70",
          s["score"] >= 70 and s["label"] == "HOT",
          f"score={s['score']} label={s['label']}")
    check("full-signal lead lists key signals",
          set(["email","phone","both_contacts","website","owner","contact_page",
               "local","niche"]).issubset(set(s["signals"])),
          f"got {s['signals']}")

    # Email-only -> WEAK
    weak = {"name": "X", "email": "x@y.com"}
    s2 = commands.score_lead(weak)
    check("email-only lead is WEAK (score < 40)",
          s2["score"] < 40 and s2["label"] == "WEAK", f"score={s2['score']}")

    # Email + phone + website -> GOOD (mid range)
    mid = {"name": "Mid", "email": "m@m.com", "phone": "555-9999",
           "website": "https://m.com/"}
    s3 = commands.score_lead(mid)
    check("email+phone+website is GOOD range",
          40 <= s3["score"] < 70 and s3["label"] == "GOOD",
          f"score={s3['score']} label={s3['label']}")


def test_call_queue():
    print("test_call_queue")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [
            {"id": "A", "name": "best", "email": "b@b.com", "phone": "555-1",
             "website": "https://b.com", "owner": "Pat", "city": "Richmond",
             "campaign_id": "C001", "contact_type": "both", "source": "scrape",
             "niche": "movers", "status": "NEW"},
            {"id": "B", "name": "no-phone", "email": "n@n.com", "status": "NEW"},
            {"id": "C", "name": "failed", "phone": "555-2", "status": "FAILED"},
            {"id": "D", "name": "called", "phone": "555-3", "status": "CALLED"},
            {"id": "E", "name": "mid", "phone": "555-4", "niche": "x",
             "status": "NEW"},
        ]}
        _seed(tmp, leads=leads)
        q = commands.call_queue(limit=10)
        ids = [l["id"] for l in q]
        check("call_queue excludes leads without phone", "B" not in ids)
        check("call_queue excludes FAILED leads", "C" not in ids)
        check("call_queue excludes CALLED leads", "D" not in ids)
        check("call_queue includes phone+status=NEW leads",
              "A" in ids and "E" in ids)
        check("call_queue sorts by score desc (A before E)",
              ids.index("A") < ids.index("E"), f"ids={ids}")


def test_mark_called():
    print("test_mark_called")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "X", "phone": "555-1",
                            "status": "NEW"}]}
        _seed(tmp, leads=leads)
        ok = commands.mark_called("L001")
        check("mark_called returns True for existing lead", ok)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("mark_called sets status=CALLED",
              after[0]["status"] == "CALLED", f"got {after[0]['status']}")
        check("mark_called sets last_attempt_at",
              after[0].get("last_attempt_at") is not None)
        ok2 = commands.mark_called("L999")
        check("mark_called returns False for missing lead", not ok2)


def main():
    test_subpage_extraction()
    test_dedup()
    test_batch_cap()
    test_daily_cap()
    test_smtp_transient()
    test_smtp_permanent()
    test_retry_count_escalation()
    test_retry_pending_cooldown_and_cap()
    test_template_merge_fields()
    test_score_lead_signals()
    test_call_queue()
    test_mark_called()
    fails = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    if fails:
        for n, d in fails:
            print(f"  FAIL: {n} -- {d}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
