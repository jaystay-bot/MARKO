"""N275A truth verifier — email autopilot, no voice calls.

Gates:
  1. Dry-run marko_send does not open any socket and never calls Resend.
  2. Live-mode marko_send (no API key) refuses with a clear error.
  3. /webhook/email rejects unsigned hits with 401.
  4. /webhook/email rejects badly-signed hits with 401.
  5. /webhook/email accepts a correctly-signed hit and writes the event.
  6. Bot Activity panel reflects seeded events on the next dashboard render.
  7. config_blockers still refuse a live send when unsubscribe / address missing.
  8. Daily cap is still enforced (synthetic log dump beyond cap is BLOCKED).
  9. The webhook surface contains NO voice / SMS / call routes.
 10. N273 + N274 verifiers still PASS (regression).

Requires the dashboard to be running with EMAIL_WEBHOOK_SECRET set in env.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT  = os.path.dirname(__file__)
URL  = "http://127.0.0.1:5000"
SECRET = (os.environ.get("EMAIL_WEBHOOK_SECRET") or "").strip()

sys.path.insert(0, ROOT)
import commands  # noqa: E402


def post(path, body_bytes, sig_header=None):
    req = urllib.request.Request(
        f"{URL}{path}", data=body_bytes, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if sig_header is not None:
        req.add_header("X-Marko-Signature", sig_header)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path):
    with urllib.request.urlopen(f"{URL}{path}", timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    proof = {}

    # ---- gate 1: dry-run never opens a socket ----
    import urllib.request as _ur
    real_urlopen = _ur.urlopen
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        raise RuntimeError("network forbidden in dry-run test")
    _ur.urlopen = spy
    import email_client as _ec
    _ec.urllib.request.urlopen = spy
    # also block the still-imported smtplib path
    import smtplib as _smtp
    real_smtp = _smtp.SMTP
    _smtp.SMTP = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("smtp forbidden in dry-run test"))

    # Drop env so live mode would bail anyway
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_pw  = os.environ.pop("MARKO_SMTP_PASSWORD", None)

    out = commands.marko_send(dry_run=True)
    proof["dry_run"] = {
        "result": out, "network_attempts": calls["n"],
        "pass": calls["n"] == 0 and "Dry run" in (out or ""),
    }

    # ---- gate 2: live-mode without key refuses cleanly ----
    out2 = commands.marko_send(dry_run=False)
    proof["live_no_key"] = {
        "result": out2,
        "pass": "RESEND_API_KEY" in (out2 or "") and calls["n"] == 0,
    }

    # restore
    _ur.urlopen = real_urlopen
    _ec.urllib.request.urlopen = real_urlopen
    _smtp.SMTP = real_smtp
    if saved_key is not None: os.environ["RESEND_API_KEY"] = saved_key
    if saved_pw  is not None: os.environ["MARKO_SMTP_PASSWORD"] = saved_pw

    # ---- gates 3 + 4: unsigned + bad-sig webhook -> 401 ----
    body_bytes = b'{"event":"opened","lead_id":"L016"}'
    s, _ = post("/webhook/email", body_bytes, sig_header=None)
    proof["webhook_unsigned"] = {"status": s, "pass": s == 401}
    s, _ = post("/webhook/email", body_bytes, sig_header="sha256=deadbeef")
    proof["webhook_bad_sig"] = {"status": s, "pass": s == 401}

    # ---- gate 5: correctly signed -> 200 ----
    if not SECRET:
        proof["signed_webhook"] = {
            "pass": False,
            "reason": "EMAIL_WEBHOOK_SECRET not set in this env; can't test signed path"
        }
    else:
        # Snapshot lead state we're about to mutate so we can restore.
        data = commands.load_json(commands.LEADS_FILE)
        target = None
        for l in data.get("leads", []):
            if l.get("id") == "L016":
                target = {k: l.get(k) for k in ("status", "email_status",
                                                "email_status_at",
                                                "sequence_step",
                                                "sequence_done",
                                                "sequence_last_event")}
                break

        # baseline count of today's events
        base = commands.email_activity_today()["counts"]
        for ev in ("opened", "replied"):
            body = json.dumps({"event": ev, "lead_id": "L016"}).encode("utf-8")
            sig = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
            s, txt = post("/webhook/email", body, sig_header=f"sha256={sig}")
            proof.setdefault("signed_writes", []).append(
                {"event": ev, "status": s, "response": txt[:120]})

        # ---- gate 6: panel reflects the seeded events ----
        s, html = get("/")
        m_open = re.search(r"<b>(\d+)</b>\s*opened", html)
        m_repl = re.search(r"<b>(\d+)</b>\s*replied", html)
        opened_now = int(m_open.group(1)) if m_open else -1
        replied_now = int(m_repl.group(1)) if m_repl else -1
        proof["panel_reflects_events"] = {
            "opened_before": base.get("opened", 0),
            "opened_after":  opened_now,
            "replied_before": base.get("replied", 0),
            "replied_after":  replied_now,
            "pass": opened_now > base.get("opened", 0)
                    and replied_now > base.get("replied", 0),
        }
        proof["signed_webhook"] = {"pass": all(w["status"] == 200
                                               for w in proof["signed_writes"])}

        # cleanup: restore L016 to its pre-test state
        if target is not None:
            data2 = commands.load_json(commands.LEADS_FILE)
            for l in data2.get("leads", []):
                if l.get("id") == "L016":
                    for k, v in target.items():
                        if v is None:
                            l.pop(k, None)
                        else:
                            l[k] = v
                    break
            commands.save_json(commands.LEADS_FILE, data2)

    # ---- gate 7: compliance blockers still bite ----
    import marko_compliance as mc
    cfg = commands.get_config() if os.path.exists(commands.CONFIG_FILE) else {}
    # Strip required fields and verify config_blockers reports the gap
    stripped = {k: v for k, v in cfg.items() if k not in
                ("unsubscribe_text", "physical_address")}
    blockers = mc.config_blockers(stripped)
    proof["compliance_blockers"] = {
        "blockers_reported": blockers,
        "pass": any("unsubscribe" in b.lower() for b in blockers)
                or any("address" in b.lower() for b in blockers),
    }

    # ---- gate 8: daily cap still enforced ----
    # marko_send checks credentials before the cap. To exercise the cap
    # we satisfy the credential check by monkey-patching get_smtp_credentials
    # for the duration of this test — never touch config.json on disk so the
    # working copy stays clean.
    log = commands.load_json(commands.LOG_FILE)
    cap = commands.DAILY_SEND_CAP
    today_iso = datetime.now().isoformat()
    backup_log = log.get("log", [])[:]
    log["log"] = backup_log + [
        {"timestamp": today_iso, "action": "send", "status": "sent",
         "campaign_id": "C001", "lead_id": f"FAKE-{i}",
         "recipient": f"x{i}@x.com"}
        for i in range(cap + 5)
    ]
    commands.save_json(commands.LOG_FILE, log)
    real_creds = commands.get_smtp_credentials
    commands.get_smtp_credentials = lambda: ("test@example.invalid", "resend")
    try:
        os.environ["RESEND_API_KEY"] = "sk_test_fake_for_cap_check"
        out = commands.marko_send(dry_run=False)
        proof["daily_cap"] = {"result": out,
                              "pass": "BLOCKED" in (out or "") and "daily cap" in (out or "")}
    finally:
        commands.get_smtp_credentials = real_creds
        log["log"] = backup_log
        commands.save_json(commands.LOG_FILE, log)
        os.environ.pop("RESEND_API_KEY", None)

    # ---- gate 9: no voice / SMS / call automation routes added ----
    # Scan dashboard.py source for forbidden tokens (defense against scope creep).
    with open(os.path.join(ROOT, "dashboard.py"), "r", encoding="utf-8") as f:
        src = f.read()
    forbidden = ["vapi", "bland.ai", "twilio", "/voice/", "/sms/",
                 "voice_client", "sms_client"]
    found = [tok for tok in forbidden if tok.lower() in src.lower()]
    proof["no_voice_or_sms"] = {"found": found, "pass": not found}

    # ---- gate 10: prior verifiers still pass ----
    for name in ("n273_verify.py", "n274_verify.py"):
        cp = subprocess.run(
            [sys.executable, os.path.join(OUT, name)],
            capture_output=True, text=True, timeout=240,
        )
        proof[name] = {
            "exit": cp.returncode,
            "pass": '"PASS": true' in cp.stdout,
        }
        if not proof[name]["pass"]:
            proof[name]["stdout_tail"] = cp.stdout[-600:]
            proof[name]["stderr_tail"] = cp.stderr[-400:]

    # ---- verdict ----
    proof["PASS"] = all(
        v.get("pass", False)
        for v in (
            proof["dry_run"], proof["live_no_key"],
            proof["webhook_unsigned"], proof["webhook_bad_sig"],
            proof["signed_webhook"], proof["panel_reflects_events"],
            proof["compliance_blockers"], proof["daily_cap"],
            proof["no_voice_or_sms"],
            proof["n273_verify.py"], proof["n274_verify.py"],
        )
    )

    print(json.dumps(proof, indent=2, default=str))
    with open(os.path.join(OUT, "N275A_result.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(proof, indent=2, default=str))


if __name__ == "__main__":
    main()
