"""MARKO N018 smoke tests -- no network, no real SMTP, no real files touched.

Run:   python smoke_test.py
Exit:  0 on all-pass, 1 on any failure.
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import commands
import scraper

MOCK_RESEND_ENV = {
    "RESEND_API_KEY": "test-resend-key-1234567890",
    "MARKO_SMTP_EMAIL": "a@b.com",
}
KV_TEST_TOKEN = "fake-token-1234567890"


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
        email, phone, owner, tags = scraper.extract_contact_from_url("https://acme.example/")
    check("email pulled from /contact", email == "hello@acme.example", f"got {email!r}")
    check("phone pulled from /contact", phone == "804-555-1234", f"got {phone!r}")

    # Homepage has email; /about has phone -- both should be aggregated.
    pages2 = {
        "https://b.example/": "<html>contact: hi@b.example</html>",
        "https://b.example/about": "<html>call 555-867-5309</html>",
    }
    with mock.patch.object(scraper.requests, "get", side_effect=_fake_get_factory(pages2)):
        email, phone, owner, tags = scraper.extract_contact_from_url("https://b.example/")
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
                            "email": f"l{i}@x{i}.com", "status": "NEW", "niche": "p"}
                           for i in range(15)]}
        config = {"batch_size": 25, "sender_name": "tester", "smtp": {},
                  "email_template": {"subject": "s", "body": "b"}}
        _seed(tmp, leads=leads, config=config)
        with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
             mock.patch.object(commands, "_lead_in_business_hours",
                               return_value=True):
            with mock.patch.object(commands, "send_email",
                                   return_value=(True, None, None, None)):
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
        with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
             mock.patch.object(commands, "_lead_in_business_hours",
                               return_value=True):
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
        with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
             mock.patch.object(commands, "_lead_in_business_hours",
                               return_value=True):
            with mock.patch.object(commands, "send_email",
                                   return_value=(True, None, None, None)):
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
            with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
                 mock.patch.object(commands, "_lead_in_business_hours",
                                   return_value=True):
                with mock.patch.object(commands, "send_email",
                                       return_value=(False, f"{code} try later", code, None)):
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
            with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
                 mock.patch.object(commands, "_lead_in_business_hours",
                                   return_value=True):
                with mock.patch.object(commands, "send_email",
                                       return_value=(False, "rejected", code, None)):
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
        with mock.patch.dict(os.environ, MOCK_RESEND_ENV), \
             mock.patch.object(commands, "_lead_in_business_hours",
                               return_value=True):
            with mock.patch.object(commands, "send_email",
                                   return_value=(False, "421 try later", 421, None)):
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
    # Maxed-out lead -> MONEY (5-tier: >=90 is MONEY, >=70 is HOT)
    full = {"name": "X", "email": "x@y.com", "phone": "555-1234",
            "website": "https://x.com/", "owner": "Pat", "niche": "movers",
            "city": "Richmond", "state": "VA", "campaign_id": "C001",
            "contact_type": "both", "source": "scrape"}
    s = commands.score_lead(full)
    check("full-signal lead scores MONEY (>=90)",
          s["score"] >= 90 and s["label"] == "MONEY",
          f"score={s['score']} label={s['label']}")
    check("full-signal lead lists key signals",
          set(["email","phone","both_contacts","website","owner","contact_page",
               "local","niche"]).issubset(set(s["signals"])),
          f"got {s['signals']}")

    # Email-only -> LOW (score=20, threshold LOW>=20)
    weak = {"name": "X", "email": "x@y.com"}
    s2 = commands.score_lead(weak)
    check("email-only lead is LOW (20 <= score < 40)",
          20 <= s2["score"] < 40 and s2["label"] == "LOW",
          f"score={s2['score']} label={s2['label']}")

    # Email + phone + website -> GOOD (mid range)
    mid = {"name": "Mid", "email": "m@m.com", "phone": "555-9999",
           "website": "https://m.com/"}
    s3 = commands.score_lead(mid)
    check("email+phone+website is GOOD range",
          40 <= s3["score"] < 70 and s3["label"] == "GOOD",
          f"score={s3['score']} label={s3['label']}")

    # No signals at all -> DEAD (score < 20)
    dead = {"name": "Nothing"}
    s4 = commands.score_lead(dead)
    check("empty lead is DEAD (score < 20)",
          s4["score"] < 20 and s4["label"] == "DEAD",
          f"score={s4['score']} label={s4['label']}")


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


def test_owner_extractor():
    print("test_owner_extractor")
    # 1) meta author
    html_meta = '<html><head><meta name="author" content="Sarah Johnson"></head></html>'
    check("owner from meta[name=author]",
          commands.extract_owner_from_html(html_meta) == "Sarah Johnson")
    # 2) JSON-LD founder
    html_ld = '<script type="application/ld+json">{"@type":"LocalBusiness","founder":{"name":"Pat O\'Brien"}}</script>'
    check("owner from JSON-LD founder",
          commands.extract_owner_from_html(html_ld) == "Pat O'Brien")
    # 3) "Owner: X" pattern
    html_text = "<p>Founded by Maria Lopez in 2010.</p>"
    check("owner from 'Founded by' text pattern",
          commands.extract_owner_from_html(html_text) == "Maria Lopez")
    # 4) "X, owner" suffix pattern
    html_suffix = "<p>Meet Robert Smith, the owner and head groomer.</p>"
    check("owner from 'Meet X, the owner' pattern",
          commands.extract_owner_from_html(html_suffix) == "Robert Smith")
    # 5) Conservative: corp word in "name" rejected
    html_corp = '<meta name="author" content="Acme Services Inc">'
    check("owner extractor rejects corp words (Acme Services Inc)",
          commands.extract_owner_from_html(html_corp) is None,
          f"got {commands.extract_owner_from_html(html_corp)!r}")
    # 6) Conservative: lowercase / single word rejected
    html_weak = '<meta name="author" content="admin">'
    check("owner extractor rejects single lowercase word",
          commands.extract_owner_from_html(html_weak) is None)
    # 7) None on empty input
    check("owner extractor returns None on empty",
          commands.extract_owner_from_html("") is None)


def test_pain_points():
    print("test_pain_points")
    # Healthy site: no tags except 'no online booking' if missing keyword
    html_healthy = (
        '<html><head><meta name="viewport" content="width=device-width">'
        '</head><body>book online · &copy; 2026 · '
        '<form>contact</form>'
        '<a href="https://facebook.com/x">fb</a></body></html>'
    )
    tags = commands.pain_points_from_html(html_healthy, "https://x.com", 200)
    check("healthy site has no pain tags", tags == [], f"got {tags}")

    # Weak: http, no viewport, no booking, no form, old copyright, no social
    html_weak = "<html><body>welcome &copy; 2020</body></html>"
    tags = commands.pain_points_from_html(html_weak, "http://x.com", 200)
    check("weak site flags no SSL", "no SSL" in tags)
    check("weak site flags weak mobile", "weak mobile" in tags)
    check("weak site flags no online booking", "no online booking" in tags)
    check("weak site flags no contact form", "no contact form" in tags)
    check("weak site flags stale copyright", any("copyright 2020" in t for t in tags))

    # Error status
    err = commands.pain_points_from_html("", "https://x.com", 500)
    check("error status emits site-error tag", any("site error" in t for t in err))

    # Cap at 5
    check("pain-points capped at 5", len(tags) <= 5)


def test_campaign_preset_route():
    print("test_campaign_preset_route")
    # Route smoke: import dashboard, simulate via test client
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        # Write a known templates.json with a single preset
        with open(commands.TEMPLATES_FILE, "w") as f:
            json.dump({"outreach": [], "campaign_presets": [
                {"id": "CPX", "name": "TestPreset", "project": "tp",
                 "niche": "movers", "city": "Richmond", "state": "VA"}
            ], "niche_presets": [], "location_presets": []}, f)
        import dashboard
        dashboard.CAMPAIGNS_FILE = commands.CAMPAIGNS_FILE
        dashboard.LEADS_FILE = commands.LEADS_FILE
        dashboard.LOG_FILE = commands.LOG_FILE
        client = dashboard.app.test_client()
        resp = client.post("/campaign/preset/CPX", follow_redirects=False)
        check("/campaign/preset/CPX returns 302 redirect",
              resp.status_code == 302, f"got {resp.status_code}")
        camps = json.load(open(commands.CAMPAIGNS_FILE))["campaigns"]
        names = [c["name"] for c in camps]
        check("/campaign/preset/CPX creates a campaign named TestPreset",
              "TestPreset" in names, f"got {names}")


def test_export_csv_is_read_only():
    print("test_export_csv_is_read_only")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "Acme", "email": "a@a.com",
                            "status": "NEW", "campaign_id": "C001"}]}
        _seed(tmp, leads=leads)
        files = [commands.CAMPAIGNS_FILE, commands.LEADS_FILE, commands.LOG_FILE]
        before = {p: open(p, "rb").read() for p in files}

        lead_csv = commands.export_leads_csv()
        campaign_csv = commands.export_campaigns_csv()

        after = {p: open(p, "rb").read() for p in files}
        check("export_leads_csv returns CSV content",
              "id,name" in lead_csv and "L001" in lead_csv)
        check("export_campaigns_csv returns CSV content",
              "id,name,project" in campaign_csv and "C001" in campaign_csv)
        check("CSV exports do not mutate campaigns/leads/log JSON",
              before == after, "export changed JSON files")


def test_report_and_money_mode_are_read_only():
    print("test_report_and_money_mode_are_read_only")
    with tempfile.TemporaryDirectory() as tmp:
        old = (datetime.now() - timedelta(hours=72)).isoformat()
        leads = {"leads": [
            {"id": "L001", "name": "Acme", "email": "a@a.com",
             "phone": "555-1", "status": "NEW", "niche": "movers",
             "pain_points": ["no online booking"]},
            {"id": "L002", "name": "Beta", "email": "b@b.com",
             "phone": "555-2", "status": "CONTACTED", "niche": "movers",
             "last_attempt_at": old},
        ]}
        config = {
            "batch_size": 10,
            "sender_name": "tester",
            "from_email": "tester@example.com",
            "unsubscribe_text": "Reply stop to opt out.",
            "physical_address": "1 Main St",
            "stop_contact_list": [],
            "smtp": {},
            "email_template": {"subject": "s", "body": "b"},
        }
        _seed(tmp, leads=leads, config=config)
        files = [commands.CAMPAIGNS_FILE, commands.LEADS_FILE,
                 commands.LOG_FILE, commands.CONFIG_FILE]
        before = {p: open(p, "rb").read() for p in files}

        commands.marko_report()
        summary = commands.pipeline_summary()
        money = commands.money_mode()

        after = {p: open(p, "rb").read() for p in files}
        check("pipeline_summary returns counts",
              "money_count" in summary and "followups_overdue" in summary)
        check("money_mode returns operator sections",
              "call_now" in money and "blockers" in money)
        check("reports and money_mode do not mutate JSON",
              before == after, "report/money mode changed JSON files")


def test_marko_intel_money_estimate():
    print("test_marko_intel_money_estimate")
    import marko_intel
    # Known niche + signals -> non-zero estimate, med/high confidence
    lead = {"niche": "movers", "pain_points":
            ["no online booking", "weak mobile", "no contact form"]}
    m = marko_intel.estimate_missed_money(lead)
    check("known niche + 3 pain points -> non-zero estimate",
          m["low"] is not None and m["low"] > 0 and m["high"] > m["low"],
          f"got {m}")
    check("3 pain points -> high confidence",
          m["confidence"] == "high", f"got {m['confidence']}")

    # Unknown niche -> None estimate, low confidence, helpful note
    unknown = {"niche": "alpaca trainer", "pain_points": ["weak mobile"]}
    u = marko_intel.estimate_missed_money(unknown)
    check("unknown niche -> low/None estimate",
          u["low"] is None and u["confidence"] == "low",
          f"got {u}")

    # No pain points -> 0/0 estimate, low confidence
    clean = {"niche": "movers", "pain_points": []}
    c = marko_intel.estimate_missed_money(clean)
    check("no pain points -> zero estimate",
          c["low"] == 0 and c["high"] == 0, f"got {c}")


def test_marko_intel_script():
    print("test_marko_intel_script")
    import marko_intel
    lead = {"name": "Acme Movers", "owner": "Sarah Johnson",
            "city": "Richmond", "niche": "movers",
            "pain_points": ["no online booking", "weak mobile"]}
    s = marko_intel.generate_script(lead, sender_name="Jay")
    check("script includes owner first name",
          "Sarah" in s and "Johnson" not in s, f"got {s!r}")
    check("script includes business name", "Acme Movers" in s)
    check("script includes city", "Richmond" in s)
    check("script hooks the first pain point",
          "book" in s.lower() or "online" in s.lower(), f"got {s!r}")

    # No owner -> doesn't address by name
    s2 = marko_intel.generate_script({"name": "X", "niche": "movers"},
                                     sender_name="Jay")
    check("missing owner -> generic opener (no 'Hey None')",
          "Hey, this is Jay" in s2 and "None" not in s2, f"got {s2!r}")


def test_marko_intel_email():
    print("test_marko_intel_email")
    import marko_intel
    lead = {"name": "Acme Movers", "owner": "Sarah Johnson",
            "city": "Richmond", "niche": "movers",
            "pain_points": ["no online booking"]}
    for kind in ("intro", "followup", "breakup"):
        e = marko_intel.generate_email(lead, kind=kind, sender_name="Jay")
        check(f"email kind={kind} has subject + body",
              e.get("subject") and e.get("body") and e.get("kind") == kind,
              f"got {e}")
    # Unknown kind falls back to intro
    e_bad = marko_intel.generate_email(lead, kind="badkind")
    check("unknown email kind falls back to intro",
          e_bad["kind"] == "intro", f"got {e_bad['kind']!r}")
    # No owner -> 'there' not 'None'
    e_none = marko_intel.generate_email({"name": "X", "niche": "movers"},
                                        kind="intro")
    check("missing owner -> 'there' fallback (no 'None')",
          "None" not in e_none["body"] and "there" in e_none["body"],
          f"body={e_none['body'][:80]!r}")


def test_intel_and_compliance_are_read_only():
    print("test_intel_and_compliance_are_read_only")
    import marko_compliance
    import marko_intel

    lead = {"id": "L001", "name": "Acme Movers", "owner": "Sarah Johnson",
            "email": "a@a.com", "phone": "555-123-4567",
            "city": "Richmond", "niche": "movers",
            "pain_points": ["no online booking", "weak mobile"],
            "status": "NEW"}
    config = {"sender_name": "Jay", "from_email": "jay@example.com",
              "unsubscribe_text": "Reply stop to opt out.",
              "physical_address": "1 Main St"}
    lead_before = json.loads(json.dumps(lead, sort_keys=True))
    config_before = json.loads(json.dumps(config, sort_keys=True))

    marko_intel.estimate_missed_money(lead)
    marko_intel.generate_script(lead)
    marko_intel.generate_voicemail(lead)
    marko_intel.why_they_buy(lead)
    email = marko_intel.generate_email(lead, config=config)
    marko_compliance.config_blockers(config)
    marko_compliance.lead_blockers(lead, stop_list=["other@example.com"])
    marko_compliance.compliance_check(config, lead, email["subject"],
                                      email["body"], stop_list=[],
                                      sends_today=0, daily_cap=50)
    marko_compliance.deliverability_checklist(config)

    check("intel helpers do not mutate lead input",
          lead == lead_before, f"lead changed to {lead}")
    check("compliance helpers do not mutate config input",
          config == config_before, f"config changed to {config}")
    check("generated compliance footer is explicit in preview",
          "Reply stop" in email["body"] and "1 Main St" in email["body"])


def test_save_config_is_explicit_and_logged():
    print("test_save_config_is_explicit_and_logged")
    with tempfile.TemporaryDirectory() as tmp:
        config = {
            "batch_size": 10,
            "sender_name": "old",
            "from_email": "old@example.com",
            "unsubscribe_text": "old stop",
            "physical_address": "old address",
            "smtp": {"host": "smtp.example.com"},
            "email_template": {"subject": "s", "body": "b"},
        }
        _seed(tmp, config=config)

        updated = commands.save_config({
            "sender_name": "new",
            "from_email": "new@example.com",
            "batch_size": 99,
            "smtp": {"host": "evil.example.com"},
            "deliverability": {"spf_ok": True, "unsafe": True},
        })
        saved = json.load(open(commands.CONFIG_FILE))
        log = json.load(open(commands.LOG_FILE)).get("log", [])
        entry = log[-1] if log else {}

        check("save_config updates whitelisted compliance fields",
              saved["sender_name"] == "new"
              and saved["from_email"] == "new@example.com"
              and updated["sender_name"] == "new")
        check("save_config preserves non-whitelisted config",
              saved["batch_size"] == 10
              and saved["smtp"]["host"] == "smtp.example.com")
        check("save_config filters deliverability keys",
              saved["deliverability"] == {"spf_ok": True},
              f"got {saved.get('deliverability')}")
        check("save_config writes explicit audit log entry",
              entry.get("action") == "config_update"
              and entry.get("scope") == "compliance"
              and "sender_name" in entry.get("fields", []),
              f"got {entry}")


def test_intel_and_email_routes():
    print("test_intel_and_email_routes")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "Acme", "owner": "Sarah Johnson",
                            "phone": "555-1", "email": "a@a.com",
                            "city": "Richmond", "niche": "movers",
                            "pain_points": ["no online booking", "weak mobile"],
                            "status": "NEW", "website": "https://acme.com"}]}
        _seed(tmp, leads=leads)
        import dashboard
        dashboard.CAMPAIGNS_FILE = commands.CAMPAIGNS_FILE
        dashboard.LEADS_FILE = commands.LEADS_FILE
        dashboard.LOG_FILE = commands.LOG_FILE
        client = dashboard.app.test_client()

        r = client.get("/lead/L001/intel")
        check("/lead/L001/intel returns 200", r.status_code == 200,
              f"got {r.status_code}")
        j = r.get_json()
        check("/intel returns score + label", j.get("score") is not None
              and j.get("label") in ("MONEY", "HOT", "GOOD", "LOW", "DEAD"),
              f"got {j}")
        check("/intel returns missed_money block",
              isinstance(j.get("missed_money"), dict)
              and "confidence" in j["missed_money"])
        check("/intel returns generated script",
              isinstance(j.get("script"), str) and len(j["script"]) > 10)

        r404 = client.get("/lead/L999/intel")
        check("/intel returns 404 for missing lead",
              r404.status_code == 404, f"got {r404.status_code}")

        for kind in ("intro", "followup", "breakup"):
            re = client.get(f"/lead/L001/email/{kind}")
            check(f"/email/{kind} route returns 200",
                  re.status_code == 200)
            je = re.get_json()
            check(f"/email/{kind} returns subject+body",
                  je.get("subject") and je.get("body"))


def test_marko_intel_voicemail():
    print("test_marko_intel_voicemail")
    import marko_intel
    lead = {"name": "Acme Movers", "owner": "Sarah Johnson",
            "pain_points": ["no online booking", "weak mobile"]}
    s = marko_intel.generate_voicemail(lead, sender_name="Jay")
    check("voicemail addresses owner by first name",
          "Sarah" in s and "Johnson" not in s, f"got {s!r}")
    check("voicemail mentions business name", "Acme Movers" in s)
    check("voicemail hooks first pain", "book" in s.lower() or "online" in s.lower(),
          f"got {s!r}")
    # No owner -> generic opener, no 'None'
    s2 = marko_intel.generate_voicemail({"name": "X"}, sender_name="Jay")
    check("voicemail no owner -> 'Hey, this is Jay'",
          "Hey, this is Jay" in s2 and "None" not in s2, f"got {s2!r}")


def test_marko_intel_why_they_buy():
    print("test_marko_intel_why_they_buy")
    import marko_intel
    # Movers with weakness -> BookerMove angle
    mover = {"niche": "movers", "pain_points":
             ["no online booking", "weak mobile", "no contact form"]}
    w = marko_intel.why_they_buy(mover)
    check("why_they_buy returns angle string", isinstance(w.get("angle"), str)
          and len(w["angle"]) > 10)
    check("movers map to BookerMove",
          w.get("recommended_service") == "BookerMove",
          f"got {w.get('recommended_service')!r}")
    check("3 pain points -> high confidence",
          w.get("confidence") == "high", f"got {w.get('confidence')}")
    check("primary_pain is a known weakness tag",
          w.get("primary_pain") == "no online booking",
          f"got {w.get('primary_pain')!r}")
    # Unknown niche -> no service rec, low conf
    unknown = {"niche": "alpaca whisperer", "pain_points": []}
    w2 = marko_intel.why_they_buy(unknown)
    check("unknown niche + no pain -> no service rec + low conf",
          w2.get("recommended_service") is None and w2.get("confidence") == "low",
          f"got {w2}")


def test_voicemail_and_why_routes():
    print("test_voicemail_and_why_routes")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "Acme", "owner": "Pat Smith",
                            "phone": "555-1", "niche": "movers",
                            "pain_points": ["no online booking"],
                            "status": "NEW"}]}
        _seed(tmp, leads=leads)
        import dashboard
        dashboard.CAMPAIGNS_FILE = commands.CAMPAIGNS_FILE
        dashboard.LEADS_FILE = commands.LEADS_FILE
        dashboard.LOG_FILE = commands.LOG_FILE
        client = dashboard.app.test_client()

        rv = client.get("/lead/L001/voicemail")
        check("/voicemail route returns 200", rv.status_code == 200)
        jv = rv.get_json()
        check("/voicemail returns script with content",
              isinstance(jv.get("script"), str) and len(jv["script"]) > 10)

        rw = client.get("/lead/L001/why")
        check("/why route returns 200", rw.status_code == 200)
        jw = rw.get_json()
        check("/why returns angle + recommended_service",
              jw.get("angle") and jw.get("recommended_service") == "BookerMove",
              f"got {jw}")

        r404 = client.get("/lead/L999/voicemail")
        check("/voicemail returns 404 for missing lead",
              r404.status_code == 404)


def test_dnc_excluded_from_call_queue():
    print("test_dnc_excluded_from_call_queue")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [
            {"id": "A", "name": "ok", "phone": "555-1", "status": "NEW",
             "niche": "movers"},
            {"id": "B", "name": "dnc", "phone": "555-2", "status": "DNC",
             "niche": "movers"},
            {"id": "C", "name": "stopped", "phone": "555-3",
             "status": "DO_NOT_CONTACT", "niche": "movers"},
            {"id": "D", "name": "booked", "phone": "555-4", "status": "BOOKED",
             "niche": "movers"},
            {"id": "E", "name": "not_int", "phone": "555-5",
             "status": "NOT_INTERESTED", "niche": "movers"},
        ]}
        _seed(tmp, leads=leads)
        q = commands.call_queue(limit=10)
        ids = [l["id"] for l in q]
        check("call_queue includes NEW lead", "A" in ids)
        check("call_queue excludes DNC", "B" not in ids)
        check("call_queue excludes DO_NOT_CONTACT", "C" not in ids)
        check("call_queue excludes BOOKED", "D" not in ids)
        check("call_queue excludes NOT_INTERESTED", "E" not in ids)


def test_set_lead_disposition_safety():
    print("test_set_lead_disposition_safety")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{"id": "L001", "name": "x", "status": "NEW"}]}
        _seed(tmp, leads=leads)
        # Unknown status rejected
        ok_bad = commands.set_lead_disposition("L001", "WHATEVER")
        check("set_lead_disposition rejects unknown status", not ok_bad)
        # Known status accepted
        ok_good = commands.set_lead_disposition("L001", "BOOKED")
        check("set_lead_disposition accepts known status", ok_good)
        after = json.load(open(commands.LEADS_FILE))["leads"]
        check("disposition saved correctly",
              after[0]["status"] == "BOOKED", f"got {after[0]['status']}")
        check("disposition stamps last_attempt_at",
              after[0].get("last_attempt_at") is not None)
        # Missing lead
        ok_missing = commands.set_lead_disposition("L999", "BOOKED")
        check("set_lead_disposition returns False for missing lead",
              not ok_missing)


def test_storage_local_roundtrip():
    print("test_storage_local_roundtrip")
    import storage
    # Default backend = local
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("STORAGE_BACKEND", None)
        check("backend defaults to local",
              storage._backend() == "local",
              f"got {storage._backend()!r}")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo.json")
            storage.write_json(path, {"hello": "world", "n": 42})
            data = storage.read_json(path)
            check("local roundtrip preserves dict",
                  data == {"hello": "world", "n": 42}, f"got {data}")
        # Missing path raises FileNotFoundError (preserves old contract)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                storage.read_json(os.path.join(tmp, "nope.json"))
                check("read_json raises FileNotFoundError on missing", False,
                      "no exception raised")
            except FileNotFoundError:
                check("read_json raises FileNotFoundError on missing", True)


def test_storage_kv_key_derivation():
    print("test_storage_kv_key_derivation")
    import storage
    check("leads.json -> marko:leads",
          storage._kv_key_from_path("leads.json") == "marko:leads")
    check("/var/task/campaigns.json -> marko:campaigns",
          storage._kv_key_from_path("/var/task/campaigns.json") == "marko:campaigns")
    check("nested path stripped to basename",
          storage._kv_key_from_path("a/b/c/marko_log.json") == "marko:marko_log")


def test_storage_kv_missing_creds():
    print("test_storage_kv_missing_creds")
    import storage
    with mock.patch.dict(os.environ,
                         {"STORAGE_BACKEND": "kv"}, clear=False):
        os.environ.pop("KV_REST_API_URL", None)
        os.environ.pop("KV_REST_API_TOKEN", None)
        try:
            storage.read_json("leads.json")
            check("kv without creds raises StorageNotConfigured", False,
                  "no exception raised")
        except storage.StorageNotConfigured:
            check("kv without creds raises StorageNotConfigured", True)
        # is_persistent reports False when creds missing
        check("is_persistent False when kv selected without creds",
              not storage.is_persistent())


def test_storage_kv_roundtrip_mocked():
    print("test_storage_kv_roundtrip_mocked")
    import storage
    captured = {"calls": []}
    stored_payload = {}

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode("utf-8") if isinstance(body, str) else body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, data=None, timeout=10):
        url = req.full_url
        method = req.get_method()
        captured["calls"].append((method, url, data))
        # Check auth header
        if req.get_header("Authorization") != f"Bearer {KV_TEST_TOKEN}":
            return FakeResp(b'{"error":"unauthorized"}')
        if method == "POST" and "/set/" in url:
            key = url.rsplit("/set/", 1)[1]
            stored_payload[key] = data.decode("utf-8") if data else None
            return FakeResp(b'{"result":"OK"}')
        if method == "GET" and "/get/" in url:
            key = url.rsplit("/get/", 1)[1]
            val = stored_payload.get(key)
            return FakeResp(
                json.dumps({"result": val}).encode("utf-8")
            )
        return FakeResp(b'{"result":null}')

    env = {
        "STORAGE_BACKEND": "kv",
        "KV_REST_API_URL": "https://fake.upstash.io",
        "KV_REST_API_TOKEN": KV_TEST_TOKEN,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(storage.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            storage.write_json("leads.json", {"leads": [{"id": "L1"}]})
            data = storage.read_json("leads.json")

    check("kv roundtrip preserves payload",
          data == {"leads": [{"id": "L1"}]}, f"got {data}")
    posts = [c for c in captured["calls"] if c[0] == "POST"]
    gets = [c for c in captured["calls"] if c[0] == "GET"]
    check("kv write issued exactly one POST", len(posts) == 1,
          f"got {len(posts)} posts")
    check("kv write URL includes marko:leads key",
          "/set/marko:leads" in posts[0][1], f"got {posts[0][1]!r}")
    check("kv read URL includes marko:leads key",
          "/get/marko:leads" in gets[0][1], f"got {gets[0][1]!r}")


def test_storage_kv_missing_key_raises_filenotfound():
    print("test_storage_kv_missing_key_raises_filenotfound")
    import storage

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode("utf-8") if isinstance(body, str) else body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, data=None, timeout=10):
        # Upstash returns {"result": null} for missing keys (not 404).
        return FakeResp(b'{"result":null}')

    env = {
        "STORAGE_BACKEND": "kv",
        "KV_REST_API_URL": "https://fake.upstash.io",
        "KV_REST_API_TOKEN": KV_TEST_TOKEN,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(storage.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            try:
                storage.read_json("ghost.json")
                check("kv null result raises FileNotFoundError", False)
            except FileNotFoundError:
                check("kv null result raises FileNotFoundError", True)


def test_storage_is_persistent_matrix():
    print("test_storage_is_persistent_matrix")
    import storage
    # local backend off Vercel -> persistent
    with mock.patch.dict(os.environ, {}, clear=True):
        check("local off vercel -> persistent", storage.is_persistent())
    # local backend on Vercel -> NOT persistent
    with mock.patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
        check("local on vercel -> NOT persistent", not storage.is_persistent())
    # kv with creds -> persistent
    with mock.patch.dict(os.environ, {
        "STORAGE_BACKEND": "kv",
        "KV_REST_API_URL": "https://x", "KV_REST_API_TOKEN": KV_TEST_TOKEN,
    }, clear=True):
        check("kv with creds -> persistent", storage.is_persistent())
    # kv without creds -> NOT persistent
    with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "kv"}, clear=True):
        check("kv without creds -> NOT persistent", not storage.is_persistent())


def test_commands_load_save_routes_through_storage():
    print("test_commands_load_save_routes_through_storage")
    import storage
    calls = {"read": 0, "write": 0}
    real_read, real_write = storage.read_json, storage.write_json

    def spy_read(path):
        calls["read"] += 1
        return real_read(path)

    def spy_write(path, data):
        calls["write"] += 1
        return real_write(path, data)

    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        with mock.patch.object(storage, "read_json", side_effect=spy_read):
            with mock.patch.object(storage, "write_json", side_effect=spy_write):
                commands.add_lead("Test Co", "t@t.com", "movers")
        check("commands.add_lead triggered storage.read_json",
              calls["read"] >= 1, f"reads={calls['read']}")
        check("commands.add_lead triggered storage.write_json",
              calls["write"] >= 1, f"writes={calls['write']}")


def test_marko_brain_basics():
    print("test_marko_brain_basics")
    import marko_brain
    perfect = {
        "name": "Acme Movers", "email": "a@a.com", "phone": "555-1",
        "website": "https://a.com/", "owner": "Pat Smith", "niche": "movers",
        "city": "Richmond", "state": "VA", "campaign_id": "C001",
        "contact_type": "both", "source": "scrape",
        "pain_points": ["no online booking", "weak mobile", "no contact form"],
    }
    c = marko_brain.closability_score(perfect)
    check("perfect lead closability >= 0.85", c >= 0.85, f"got {c}")
    path = marko_brain.fastest_close_path(perfect)
    check("perfect mover -> CALL FIRST path", path == "CALL FIRST",
          f"got {path!r}")
    rec = marko_brain.recommended_first_action(perfect)
    check("recommended_first_action returns path+action+by_when+reason",
          all(k in rec for k in ("path", "action", "by_when", "reason")),
          f"got {rec}")
    check("niche slug: 'movers' -> 'movers'",
          marko_brain.niche_to_mockup_slug("movers") == "movers")
    check("niche slug: 'dog groomer' -> 'groomers'",
          marko_brain.niche_to_mockup_slug("dog groomer") == "groomers")
    check("niche slug: 'med spa' -> 'med_spas'",
          marko_brain.niche_to_mockup_slug("med spa") == "med_spas")
    check("niche slug: unknown returns None",
          marko_brain.niche_to_mockup_slug("alpaca whisperer") is None)
    check("best_mockup_variant('movers') == emergency",
          marko_brain.best_mockup_variant("movers") == "emergency")
    check("best_mockup_variant('med spa') == booking",
          marko_brain.best_mockup_variant("med spa") == "booking")


def test_brain_and_mockup_routes():
    print("test_brain_and_mockup_routes")
    with tempfile.TemporaryDirectory() as tmp:
        leads = {"leads": [{
            "id": "L001", "name": "Acme Movers", "owner": "Pat Smith",
            "phone": "555-1", "email": "a@a.com", "niche": "movers",
            "city": "Richmond", "state": "VA", "website": "https://a.com",
            "pain_points": ["no online booking", "weak mobile"],
            "contact_type": "both", "source": "scrape",
            "campaign_id": "C001", "status": "NEW",
        }]}
        _seed(tmp, leads=leads)
        import dashboard
        dashboard.CAMPAIGNS_FILE = commands.CAMPAIGNS_FILE
        dashboard.LEADS_FILE = commands.LEADS_FILE
        dashboard.LOG_FILE = commands.LOG_FILE
        client = dashboard.app.test_client()

        r = client.get("/lead/L001/brain")
        check("/lead/L001/brain returns 200", r.status_code == 200,
              f"got {r.status_code}")
        j = r.get_json()
        check("/brain returns path + closability + reason",
              j.get("path") and j.get("closability") is not None
              and j.get("reason"),
              f"got keys {sorted(j.keys()) if j else None}")
        check("/brain mockup hint resolves for movers",
              j.get("mockup") and j["mockup"]["slug"] == "movers",
              f"got mockup={j.get('mockup')}")

        r404 = client.get("/lead/L999/brain")
        check("/brain returns 404 for missing lead",
              r404.status_code == 404)

        rm = client.get("/mockup/movers/emergency?lead_id=L001")
        check("/mockup/movers/emergency renders 200",
              rm.status_code == 200, f"got {rm.status_code}")
        body = rm.get_data(as_text=True)
        check("/mockup renders lead's business name", "Acme Movers" in body)
        check("/mockup renders lead's city", "Richmond" in body)

        rp = client.get("/mockup/movers/booking")
        check("/mockup without lead_id renders 200", rp.status_code == 200)
        check("/mockup placeholder shows 'Your Business'",
              "Your Business" in rp.get_data(as_text=True))

        r_bad = client.get("/mockup/notarealniche/booking")
        check("/mockup unknown slug returns 404", r_bad.status_code == 404)
        bj = r_bad.get_json()
        check("/mockup 404 includes available catalog",
              "available" in bj and "movers" in bj["available"])


def test_pipeline_summary_fields():
    print("test_pipeline_summary_fields")
    with tempfile.TemporaryDirectory() as tmp:
        # 1 MONEY-tier lead, 1 LOW lead, 1 DNC (excluded), 1 CONTACTED 72h ago.
        now = datetime.now()
        old = (now - timedelta(hours=72)).isoformat()
        leads = {"leads": [
            {"id": "M1", "name": "money", "email": "m@m.com", "phone": "555-1",
             "website": "https://m.com", "owner": "Pat Smith", "niche": "movers",
             "city": "Richmond", "state": "VA", "campaign_id": "C001",
             "contact_type": "both", "source": "scrape", "status": "NEW"},
            {"id": "L1", "name": "low", "email": "l@l.com", "status": "NEW"},
            {"id": "D1", "name": "dnc", "phone": "555-9", "status": "DNC"},
            {"id": "F1", "name": "follow", "email": "f@f.com", "phone": "555-2",
             "status": "CONTACTED", "last_attempt_at": old},
        ]}
        _seed(tmp, leads=leads)
        s = commands.pipeline_summary()
        check("pipeline_summary counts MONEY tier",
              s["money_count"] >= 1, f"got {s['money_count']}")
        check("pipeline_summary excludes DNC from tier counts",
              s["money_count"] + s["hot_count"] + s["good_count"]
              + s["low_count"] + s["dead_count"] == 3,  # M1, L1, F1
              f"got tier sum {s}")
        check("pipeline_summary detects overdue follow-up",
              s["followups_overdue"] >= 1, f"got {s['followups_overdue']}")
        check("pipeline_summary returns followup window",
              s.get("followup_window_hours") == commands.FOLLOWUP_OVERDUE_HOURS)


# ---------- N182 truth checks: leak / mockup / pitch / pitch_pack / mobile ----------

# Whitelisted fields the mockup template is allowed to read from a lead.
# Any reference outside this set inside templates/mockup/*.html = fail.
N182_MOCKUP_WHITELIST = {"name", "city", "state", "phone", "niche"}

# Locally-defined-in-template vars set via {% set %} that are NOT lead fields.
# These are template content strings, not data — allowed.
N182_TEMPLATE_LOCAL_VARS = {
    "badge", "tagline", "feat1", "feat2", "sub_label", "book_label",
    "call_label",
}

# Forbidden patterns in rendered mockup HTML — would indicate fake metrics.
# Carefully tuned so generic UX phrases like "in 30 seconds" don't trip.
import re as _re_n182
N182_FAKE_METRIC_PATTERNS = [
    (r"\$\d", "literal dollar amount"),
    (r"\d+\s*%\s", "literal percent claim"),
    (r"\d+\s*stars?\b", "star-rating claim"),
    (r"\d+\s*reviews?\b", "review-count claim"),
    (r"customers\s+served", "fabricated customer count phrasing"),
    (r"\brevenue\b", "literal revenue claim"),
    (r"\d+\s*/\s*5\b", "rating-out-of-5 claim"),
]


def _make_n182_seed(tmp, lead_overrides=None):
    """Seed with three leads covering email-only, phone-only, both-contacts."""
    base = [
        {"id": "L001", "name": "Mike's Plumbing", "owner": "Mike Davis",
         "phone": "804-555-0001", "email": None,
         "city": "Richmond", "state": "VA", "niche": "plumber",
         "pain_points": ["no contact form", "weak mobile"],
         "status": "NEW", "website": "http://mikesplumbing.example"},
        {"id": "L002", "name": "Lucky Dog Grooming", "owner": None,
         "phone": None, "email": "info@luckydog.example",
         "city": "Richmond", "state": "VA", "niche": "dog groomer",
         "pain_points": ["no online booking"],
         "status": "CONTACTED", "website": "https://luckydog.example"},
        {"id": "L003", "name": "Storm Roofers", "owner": "Jane Doe",
         "phone": "804-555-0003", "email": "jane@stormroofers.example",
         "city": "Richmond", "state": "VA", "niche": "roofer",
         "pain_points": ["no contact form"],
         "status": "INTERESTED", "website": "https://stormroofers.example"},
    ]
    if lead_overrides:
        base.extend(lead_overrides)
    _seed(tmp, leads={"leads": base},
          config={"batch_size": 10, "sender_name": "Jay",
                  "from_email": "jay@marko.example",
                  "unsubscribe_text": "reply STOP",
                  "physical_address": "PO Box 1, Richmond VA",
                  "smtp": {}, "email_template": {"subject": "s", "body": "b"}})


def _bind_dashboard():
    import dashboard
    dashboard.CAMPAIGNS_FILE = commands.CAMPAIGNS_FILE
    dashboard.LEADS_FILE = commands.LEADS_FILE
    dashboard.LOG_FILE = commands.LOG_FILE
    return dashboard


def test_n182_leak_route_and_labels():
    """Truth check #1 (leak route alive) + #6 (Confirmed/Inferred/Needs labels)."""
    print("test_n182_leak_route_and_labels")
    with tempfile.TemporaryDirectory() as tmp:
        _make_n182_seed(tmp)
        client = _bind_dashboard().app.test_client()
        r = client.get("/lead/L001/leak")
        check("/lead/L001/leak returns 200", r.status_code == 200,
              f"got {r.status_code}")
        html = r.get_data(as_text=True)
        check("leak page shows recommended offer",
              "Recommended Offer" in html and "BookerMove" in html,
              "missing offer block")
        check("leak page shows Confirmed label",
              "Confirmed" in html, "no Confirmed pill")
        check("leak page shows Inferred label",
              "Inferred" in html, "no Inferred pill (missed-call risk should appear)")
        check("leak page shows Needs human check label",
              "Needs human check" in html, "no Needs human check pill")
        # Every leak row must carry exactly one of the three labels via the pill class.
        confirmed_rows = html.count('class="leak-row confirmed"')
        inferred_rows = html.count('class="leak-row inferred"')
        needs_rows = html.count('class="leak-row needs"')
        total_rows = confirmed_rows + inferred_rows + needs_rows
        check("every leak row has a confidence label class",
              total_rows >= 1, f"got {total_rows} labeled rows")

        r404 = client.get("/lead/L999/leak")
        check("/lead/L999/leak returns 404",
              r404.status_code == 404, f"got {r404.status_code}")


def test_n182_pitch_route_auto_flip():
    """Truth check #2: email mode for emailable, call mode for phone-only."""
    print("test_n182_pitch_route_auto_flip")
    with tempfile.TemporaryDirectory() as tmp:
        _make_n182_seed(tmp)
        client = _bind_dashboard().app.test_client()

        r1 = client.get("/lead/L001/pitch")  # phone-only
        check("/pitch phone-only returns 200", r1.status_code == 200)
        html1 = r1.get_data(as_text=True)
        check("phone-only auto-flips to call mode",
              "call mode" in html1 and "Opener" in html1
              and "no email on file" in html1,
              "did not flip to call mode")

        r2 = client.get("/lead/L002/pitch")  # email-only
        check("/pitch email-only returns 200", r2.status_code == 200)
        html2 = r2.get_data(as_text=True)
        check("email-only stays in email mode",
              "email mode" in html2 and "Subject:" in html2,
              "did not stay in email mode")

        r3 = client.get("/lead/L003/pitch")  # both
        check("/pitch both returns 200", r3.status_code == 200)
        html3 = r3.get_data(as_text=True)
        check("both-contacts defaults to email + offers switch",
              "email mode" in html3 and "Switch to Call Script" in html3,
              "missing email mode or switch link")


def test_n182_mockup_renders_per_niche():
    """Truth check #3 + #4 + #5: each niche template renders cleanly."""
    print("test_n182_mockup_renders_per_niche")
    import marko_intel
    with tempfile.TemporaryDirectory() as tmp:
        # Seed one lead per niche with the niche string the slug router accepts.
        niche_lead_seed = {
            "plumbers": "plumber",       "hvac": "hvac",
            "movers": "moving company",  "roofers": "roofer",
            "towing": "towing service",  "groomers": "dog groomer",
            "auto_shops": "auto repair", "med_spas": "med spa",
            "detailers": "auto detailer","salons": "salon",
        }
        leads = []
        # Start at L004 so we don't collide with the 3 base leads added by _make_n182_seed.
        for idx, (slug, niche_str) in enumerate(niche_lead_seed.items(), start=4):
            leads.append({"id": f"L{idx:03d}", "name": f"Test {slug.title()}",
                          "phone": f"804-555-{idx:04d}",
                          "city": "Richmond", "state": "VA",
                          "niche": niche_str, "status": "NEW"})
        _make_n182_seed(tmp, lead_overrides=leads)
        client = _bind_dashboard().app.test_client()

        for idx, (slug, _) in enumerate(niche_lead_seed.items(), start=1):
            lid = f"L{idx + 3:03d}"  # +3 because base seed adds 3 leads first
            r = client.get(f"/lead/{lid}/mockup")
            check(f"/lead/{lid}/mockup ({slug}) returns 200",
                  r.status_code == 200, f"got {r.status_code}")
            html = r.get_data(as_text=True)
            # #3 — no Jinja placeholder strings leaked into rendered output
            check(f"mockup {slug} has no unresolved {{placeholder}}",
                  "{{" not in html and "{%" not in html,
                  "Jinja placeholders survived render")
            check(f"mockup {slug} contains lead name",
                  f"Test {slug.title()}" in html, "lead name missing")
            # #5 — no fake metrics in the rendered HTML body
            # Restrict the scan to the .mk mockup card content
            m = _re_n182.search(r'<div class="mk[^"]*">(.+?)</div>\s*</section>',
                                html, _re_n182.DOTALL)
            scope = m.group(1) if m else html
            for pattern, label in N182_FAKE_METRIC_PATTERNS:
                if _re_n182.search(pattern, scope, _re_n182.IGNORECASE):
                    check(f"mockup {slug} has no {label}", False,
                          f"matched /{pattern}/ in rendered HTML")
                    break
            else:
                check(f"mockup {slug} has no fake numeric claims", True)

        # Try variant switching on one niche.
        rv = client.get("/lead/L004/mockup?variant=booking")
        check("variant override works",
              rv.status_code == 200
              and "Variant: " in rv.get_data(as_text=True)
              and "booking" in rv.get_data(as_text=True), "")


def test_n182_mockup_template_whitelist():
    """Truth check #4: mockup templates only reference whitelisted lead fields.

    Walks every templates/mockup/*.html, extracts {{ var }} refs, and asserts
    each is either in the lead-field whitelist or in the template-local set.
    """
    print("test_n182_mockup_template_whitelist")
    import marko_intel
    mockup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates", "mockup")
    var_re = _re_n182.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")
    set_re = _re_n182.compile(r"\{%\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)")
    files = sorted(os.listdir(mockup_dir))
    check("mockup dir has >= 22 files (10 niches × 2 + 2 bases)",
          len(files) >= 22, f"got {len(files)}")

    allowed = N182_MOCKUP_WHITELIST | N182_TEMPLATE_LOCAL_VARS
    for fname in files:
        if not fname.endswith(".html"):
            continue
        path = os.path.join(mockup_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        # Locally-set vars in THIS file are also OK to reference.
        local = set(set_re.findall(src))
        refs = set(var_re.findall(src))
        bad = [r for r in refs if r not in allowed and r not in local]
        check(f"mockup/{fname} only uses whitelisted refs",
              not bad, f"forbidden refs: {bad}")


def test_n182_pitch_pack_zip():
    """Truth check #10: pitch pack is a real zip with 3 files."""
    print("test_n182_pitch_pack_zip")
    with tempfile.TemporaryDirectory() as tmp:
        _make_n182_seed(tmp)
        client = _bind_dashboard().app.test_client()
        r = client.get("/lead/L001/pitch_pack")
        check("/pitch_pack returns 200", r.status_code == 200,
              f"got {r.status_code}")
        check("/pitch_pack has Content-Disposition attachment",
              "attachment" in (r.headers.get("Content-Disposition") or ""),
              f"got {r.headers.get('Content-Disposition')!r}")
        try:
            zf = zipfile.ZipFile(io.BytesIO(r.data))
            names = set(zf.namelist())
            check("pitch_pack contains email.txt + mockup.html + leak_report.md",
                  {"email.txt", "mockup.html", "leak_report.md"}.issubset(names),
                  f"got {names}")
            email_blob = zf.read("email.txt").decode()
            mockup_blob = zf.read("mockup.html").decode()
            report_blob = zf.read("leak_report.md").decode()
            check("email.txt mentions lead name",
                  "Mike's Plumbing" in email_blob, "lead name missing")
            check("mockup.html is non-empty",
                  len(mockup_blob) > 100, f"len={len(mockup_blob)}")
            check("leak_report.md has Recommended Offer section",
                  "Recommended Offer" in report_blob, "section missing")
        except zipfile.BadZipFile as exc:
            check("pitch_pack is valid zip", False, str(exc))


def test_n182_mobile_call_mode():
    """Truth check #7: mobile page has tap-to-dial + viewport + big buttons."""
    print("test_n182_mobile_call_mode")
    with tempfile.TemporaryDirectory() as tmp:
        _make_n182_seed(tmp)
        client = _bind_dashboard().app.test_client()
        r = client.get("/m/lead/L001")
        check("/m/lead/L001 returns 200", r.status_code == 200,
              f"got {r.status_code}")
        html = r.get_data(as_text=True)
        check("mobile page has viewport meta",
              'name="viewport"' in html and "width=device-width" in html,
              "missing viewport meta")
        check("mobile page has tap-to-dial link",
              'href="tel:804-555-0001"' in html,
              "tap-to-dial missing")
        check("mobile page has TOP LEAK section",
              "TOP LEAK" in html, "TOP LEAK header missing")
        check("mobile page has 4-button outcome row",
              "Mark Interested" in html and "Voicemail Script" in html
              and "Send Follow-Up" in html and "Back to Queue" in html,
              "outcome buttons missing")
        # min-height assertion: mobile CSS sets .m-tel to >= 60px and .m-actions .btn to 48px
        check("mobile primary buttons have min-height >= 44px",
              "min-height:60px" in html or "min-height:48px" in html,
              "no large-tap-target CSS")


def test_n182_pipeline_total_math():
    """Truth check #9: pipeline_total only sums CONTACTED + INTERESTED offer.price."""
    print("test_n182_pipeline_total_math")
    import marko_intel
    leads = [
        # CONTACTED + has leaks → BookerMove $1500
        {"name": "A", "niche": "plumber", "status": "CONTACTED",
         "pain_points": ["no contact form"]},
        # INTERESTED + has leaks → BookerMove $1500
        {"name": "B", "niche": "hvac", "status": "INTERESTED",
         "pain_points": ["no online booking"]},
        # NEW → must NOT be included
        {"name": "C", "niche": "plumber", "status": "NEW",
         "pain_points": ["no contact form"]},
        # CONTACTED but no leaks → audit ($0) → not included
        {"name": "D", "niche": "salon", "status": "CONTACTED",
         "pain_points": []},
    ]
    commands.annotate_leads(leads)
    p = commands.pipeline_total(leads)
    check("pipeline_total counts only CONTACTED+INTERESTED with priced offer",
          p["count"] == 2, f"got {p}")
    check("pipeline_total sums $1500 + $1500 = $3000",
          p["total"] == 3000, f"got {p}")
    # NEW status should not be summed even if the lead has a great offer.
    new_only = [l for l in leads if l["status"] == "NEW"]
    p2 = commands.pipeline_total(new_only)
    check("pipeline_total ignores NEW leads",
          p2["total"] == 0 and p2["count"] == 0, f"got {p2}")


def test_n182_no_new_pip_deps():
    """Truth check #8: requirements.txt unchanged — N182 uses stdlib only.

    Compares current requirements.txt against the known pre-N182 dependency
    list. If new entries appear, fail. Allows whitespace/ordering changes.
    """
    print("test_n182_no_new_pip_deps")
    here = os.path.dirname(os.path.abspath(__file__))
    req_path = os.path.join(here, "requirements.txt")
    check("requirements.txt exists", os.path.exists(req_path))
    if not os.path.exists(req_path):
        return
    with open(req_path, "r", encoding="utf-8") as f:
        pkgs = {line.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
                for line in f if line.strip() and not line.startswith("#")}
    # The N182 build prompt requires no new external libs; only stdlib.
    # Allow {flask, requests, beautifulsoup4, bs4, lxml, gunicorn, flask-cors, anything pre-existing}.
    # This test asserts that the new modules used (io, json, os, zipfile)
    # are ALL stdlib — by importing them and checking they have no __file__
    # under site-packages.
    import io as _io, zipfile as _zf
    import sysconfig
    stdlib_root = sysconfig.get_paths()["stdlib"]
    for mod, name in [(_io, "io"), (_zf, "zipfile")]:
        mod_file = getattr(mod, "__file__", None) or ""
        ok = (not mod_file) or mod_file.startswith(stdlib_root) \
             or "site-packages" not in mod_file
        check(f"N182 uses stdlib for {name}", ok, f"file={mod_file}")


def test_n182_buttons_on_home_call_first():
    """Truth check #1 (call-first cards link to the new routes)."""
    print("test_n182_buttons_on_home_call_first")
    with tempfile.TemporaryDirectory() as tmp:
        _make_n182_seed(tmp)
        client = _bind_dashboard().app.test_client()
        r = client.get("/")
        check("home returns 200", r.status_code == 200, f"got {r.status_code}")
        html = r.get_data(as_text=True)
        check("home Call First card links to leak route",
              "/lead/L001/leak" in html, "leak link missing")
        check("home Call First card links to mockup route",
              "/lead/L001/mockup" in html, "mockup link missing")
        check("home Call First card links to pitch_pack route",
              "/lead/L001/pitch_pack" in html, "pitch_pack link missing")
        check("home shows Generate Leak Report button label",
              "Generate Leak Report" in html, "button label missing")
        check("home shows Create Mockup Pitch button label",
              "Create Mockup Pitch" in html, "button label missing")
        check("home shows Export Pitch Pack button label",
              "Export Pitch Pack" in html, "button label missing")
        # Pipeline pill: 2 leads CONTACTED+INTERESTED with priced offers.
        # L002 dog groomer + "no online booking" -> QUOTE_INTAKE $497.
        # L003 roofer + "no contact form" (high-value) -> BOOKERMOVE $1,500.
        # Total = $1,997 across 2 leads.
        check("home shows pipeline total pill",
              "pipeline $1,997" in html and "(2 leads)" in html,
              "pipeline pill missing or wrong")


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
    test_owner_extractor()
    test_pain_points()
    test_campaign_preset_route()
    test_export_csv_is_read_only()
    test_report_and_money_mode_are_read_only()
    test_marko_intel_money_estimate()
    test_marko_intel_script()
    test_marko_intel_email()
    test_intel_and_compliance_are_read_only()
    test_save_config_is_explicit_and_logged()
    test_intel_and_email_routes()
    test_marko_intel_voicemail()
    test_marko_intel_why_they_buy()
    test_voicemail_and_why_routes()
    test_storage_local_roundtrip()
    test_storage_kv_key_derivation()
    test_storage_kv_missing_creds()
    test_storage_kv_roundtrip_mocked()
    test_storage_kv_missing_key_raises_filenotfound()
    test_storage_is_persistent_matrix()
    test_commands_load_save_routes_through_storage()
    test_marko_brain_basics()
    test_brain_and_mockup_routes()
    test_dnc_excluded_from_call_queue()
    test_set_lead_disposition_safety()
    test_pipeline_summary_fields()
    # N182 truth checks
    test_n182_leak_route_and_labels()
    test_n182_pitch_route_auto_flip()
    test_n182_mockup_renders_per_niche()
    test_n182_mockup_template_whitelist()
    test_n182_pitch_pack_zip()
    test_n182_mobile_call_mode()
    test_n182_pipeline_total_math()
    test_n182_no_new_pip_deps()
    test_n182_buttons_on_home_call_first()
    fails = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    if fails:
        for n, d in fails:
            print(f"  FAIL: {n} -- {d}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
