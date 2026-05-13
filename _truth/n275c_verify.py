"""N275C truth verifier — per-domain throttle + send-window gate + one-shot
scheduled batch.

Gates:
  a) Per-domain throttle skips dispatch at PER_DOMAIN_HOURLY_CAP.
  b) Frozen-time outside-business-hours check rejects every lead at 3am UTC
     (where every US state-local clock falls outside [8, 18)).
  c) Scheduler lock: second start_scheduled_send() while a job is in flight
     bails with state='already scheduled'.
  d) Scheduled batch fires once and writes a completion banner.
  e) Prior verifiers (N273, N274, N275A, N275B) still PASS.

Hermetic: every gate snapshots and restores leads.json, campaigns.json,
marko_log.json, config.json, plus cleans up any .scheduled_send* artifacts.

Requires the dashboard running on http://127.0.0.1:5000 with
EMAIL_WEBHOOK_SECRET + ADMIN_TOKEN env (needed by the regression chain).
"""
import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT  = os.path.dirname(__file__)
URL  = "http://127.0.0.1:5000"

sys.path.insert(0, ROOT)
import commands  # noqa: E402


_TRACKED_FILES = (
    "LEADS_FILE", "CAMPAIGNS_FILE", "LOG_FILE", "CONFIG_FILE",
    "SCHEDULED_SEND_FILE", "SCHEDULED_SEND_RESULT",
)


def _read_bytes(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _snapshot():
    """Capture raw bytes of every file the verifier may touch.

    Byte-level so an unrelated re-serialize doesn't flip CRLF to LF
    (which would change md5 even when content is semantically identical).
    """
    snap = {}
    for attr in _TRACKED_FILES:
        path = getattr(commands, attr)
        snap[attr] = (path, _read_bytes(path))
    return snap


def _patient_replace(tmp, path, attempts=20, delay=0.1):
    """N276.2: os.replace on Windows raises PermissionError when the dest is
    held by another reader (Flask handler mid-request, child verifier
    subprocess, antivirus scanner). Retry with linear backoff; clean up the
    tmp file if every attempt fails so we don't leave a `.restore.tmp`
    polluting the working tree.
    """
    last = None
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (i + 1))
    # Give up and clean up.
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    raise last  # surface the original error


def _restore(snap):
    """Restore exact pre-run bytes (or remove the file if it didn't exist)."""
    for path, blob in snap.values():
        if blob is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        tmp = path + ".restore.tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        _patient_replace(tmp, path)


def _post_form(path, fields):
    body = "&".join(f"{k}={v}" for k, v in fields.items()).encode("utf-8")
    req = urllib.request.Request(
        f"{URL}{path}", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        # Don't follow redirects — we want the 302 + Location.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        opener = urllib.request.build_opener(NoRedirect)
        return opener.open(req, timeout=10).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace") + f"|loc={e.headers.get('Location','')}"


def gate_a_throttle():
    """Inject 3 fresh sent-log entries for one domain, then run marko_send
    through a spied email_client and assert the throttle skipped the only
    NEW lead at that domain.
    """
    import email_client as _ec
    real_send = _ec.send
    captured = []
    def spy(to, subject, body, from_, reply_to=None, dry_run=True,
            api_url=None, timeout=10.0, headers=None):
        captured.append({"to": to})
        return {"id": f"spy-{len(captured)}", "status": "sent", "error": None}
    _ec.send = spy

    # Force-pass the SMTP creds + config-blockers gate without touching disk.
    cfg_real = commands.get_config
    creds_real = commands.get_smtp_credentials
    cfg = {"from_email": "test@example.invalid",
           "sender_name": "MARKO Truth",
           "unsubscribe_text": "To unsubscribe, reply STOP.",
           "physical_address": "123 Truth St, TestCity TX 75001",
           "batch_size": 10}
    commands.get_config = lambda: cfg
    commands.get_smtp_credentials = lambda: (cfg["from_email"], "resend")
    os.environ["RESEND_API_KEY"] = "spy_key"

    # Snapshot data files; mutate in memory only; restore at the end.
    snap = _snapshot()
    try:
        DOM = "throttle.test"
        # Build a campaign + leads in memory.
        camps = commands.load_json(commands.CAMPAIGNS_FILE)
        camps["campaigns"] = [{
            "id": "C_N275C_T", "name": "throttle", "status": "ACTIVE",
            "sends": 0, "open_rate": 0, "replies": 0, "signups": 0,
            "verdict": "PENDING", "last_action": "", "next": "SEND",
            "project": "n275c",
        }]
        commands.save_json(commands.CAMPAIGNS_FILE, camps)

        leads = commands.load_json(commands.LEADS_FILE)
        leads["leads"] = [{
            "id": "L_T1", "name": "Throttle Lead", "state": "NY",
            "email": f"lead@{DOM}", "status": "NEW",
            "campaign_id": "C_N275C_T",
        }]
        commands.save_json(commands.LEADS_FILE, leads)

        # Seed three fresh "sent" events to the same domain in the last 60m.
        log = commands.load_json(commands.LOG_FILE)
        now_iso = datetime.now().isoformat()
        for i in range(commands.PER_DOMAIN_HOURLY_CAP):
            log["log"].append({
                "timestamp": now_iso,
                "action": "send", "status": "sent",
                "campaign_id": "C_N275C_T", "lead_id": f"L_PAD{i}",
                "recipient": f"prev{i}@{DOM}",
            })
        commands.save_json(commands.LOG_FILE, log)

        # Sanity: helper sees 3 in last hour.
        observed = commands._count_sends_by_domain_last_hour(DOM)

        # Pin business-hours to True so window doesn't accidentally skip first.
        real_window = commands._lead_in_business_hours
        commands._lead_in_business_hours = lambda lead, now=None: True
        try:
            result = commands.marko_send(dry_run=False)
        finally:
            commands._lead_in_business_hours = real_window

        # Check the log got a send_skipped reason=domain_cap entry.
        log_after = commands.load_json(commands.LOG_FILE).get("log", [])
        skip_entries = [e for e in log_after
                        if e.get("action") == "send_skipped"
                        and e.get("reason") == "domain_cap"
                        and e.get("lead_id") == "L_T1"]
        return {
            "observed_count_last_hour": observed,
            "expected_cap":             commands.PER_DOMAIN_HOURLY_CAP,
            "dispatch_attempts":        len(captured),
            "result":                   result,
            "domain_skip_log_entries":  len(skip_entries),
            "pass": (observed == commands.PER_DOMAIN_HOURLY_CAP
                     and len(captured) == 0
                     and "1 domain cap" in (result or "")
                     and len(skip_entries) == 1),
        }
    finally:
        _ec.send = real_send
        commands.get_config = cfg_real
        commands.get_smtp_credentials = creds_real
        os.environ.pop("RESEND_API_KEY", None)
        _restore(snap)


def gate_b_outside_hours():
    """Pick a UTC instant where every US-state-local clock is outside [8,18):
    3am UTC = 11pm ET (prior day) / 8pm PT / 5pm HT (HI is +10pm? actually
    3am UTC = 5pm HT prior day -> 17:00 which is INSIDE). So pick a UTC that
    misses Hawaii too: 9am UTC = 4am ET / 1am PT / 11pm HT (prior). All
    states < 8am local. Verify _lead_in_business_hours returns False for one
    lead from each TZ band.
    """
    test_utc = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    results = {}
    for state in ("NY", "TX", "CO", "CA", "AK", "HI", "AZ"):
        results[state] = commands._lead_in_business_hours(
            {"state": state, "email": "x@x"}, now=test_utc)
    # And confirm a known in-hours UTC passes (18:00 UTC = 2pm ET DST).
    in_hours_utc = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
    in_hours_ny = commands._lead_in_business_hours(
        {"state": "NY", "email": "x@x"}, now=in_hours_utc)

    # Also wire-test through marko_send: monkey-patch the helper to always
    # return False and confirm the dispatch path counts every lead as skipped.
    import email_client as _ec
    real_send = _ec.send
    captured = []
    _ec.send = lambda **kw: (captured.append(kw) or
                              {"id": "spy", "status": "sent", "error": None})
    cfg_real = commands.get_config
    creds_real = commands.get_smtp_credentials
    cfg = {"from_email": "test@example.invalid", "sender_name": "MARKO",
           "unsubscribe_text": "STOP", "physical_address": "1 X St, Y TX 75001",
           "batch_size": 10}
    commands.get_config = lambda: cfg
    commands.get_smtp_credentials = lambda: (cfg["from_email"], "resend")
    os.environ["RESEND_API_KEY"] = "spy_key"
    snap = _snapshot()
    try:
        camps = commands.load_json(commands.CAMPAIGNS_FILE)
        camps["campaigns"] = [{
            "id": "C_N275C_W", "name": "win", "status": "ACTIVE",
            "sends": 0, "open_rate": 0, "replies": 0, "signups": 0,
            "verdict": "PENDING", "last_action": "", "next": "SEND",
            "project": "n275c",
        }]
        commands.save_json(commands.CAMPAIGNS_FILE, camps)
        leads = commands.load_json(commands.LEADS_FILE)
        leads["leads"] = [
            {"id": "L_W1", "name": "W1", "state": "NY",
             "email": "a@a.test", "status": "NEW",
             "campaign_id": "C_N275C_W"},
            {"id": "L_W2", "name": "W2", "state": "TX",
             "email": "b@b.test", "status": "NEW",
             "campaign_id": "C_N275C_W"},
        ]
        commands.save_json(commands.LEADS_FILE, leads)
        real_window = commands._lead_in_business_hours
        commands._lead_in_business_hours = lambda lead, now=None: False
        try:
            wire_result = commands.marko_send(dry_run=False)
        finally:
            commands._lead_in_business_hours = real_window
    finally:
        _ec.send = real_send
        commands.get_config = cfg_real
        commands.get_smtp_credentials = creds_real
        os.environ.pop("RESEND_API_KEY", None)
        _restore(snap)

    return {
        "frozen_utc":              test_utc.isoformat(),
        "outside_hours_results":   results,
        "in_hours_ny":             in_hours_ny,
        "wire_test_attempts":      len(captured),
        "wire_test_result":        wire_result,
        "pass": (all(v is False for v in results.values())
                 and in_hours_ny is True
                 and len(captured) == 0
                 and "2 outside business hours" in (wire_result or "")),
    }


def gate_c_scheduler_lock():
    """Schedule a job in the future, then immediately schedule again and
    expect 'already scheduled'. Clean the in-flight job afterwards so the
    next gate runs from a clean slate.
    """
    snap = _snapshot()
    try:
        # Make sure no in-flight job exists.
        for p in (commands.SCHEDULED_SEND_FILE,
                  commands.SCHEDULED_SEND_RESULT):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        far_future = (datetime.now() + timedelta(hours=1)).isoformat()
        r1 = commands.start_scheduled_send(
            when_iso=far_future, dry_run=True, batch_size_cap=1)
        r2 = commands.start_scheduled_send(
            when_iso=far_future, dry_run=True, batch_size_cap=1)
        return {
            "r1": r1,
            "r2": r2,
            "pass": (r1.get("state") == "scheduled"
                     and r2.get("state") == "already scheduled"),
        }
    finally:
        # Drop the in-flight marker so the thread (sleeping 1h) finds nothing
        # to write — and so the next gate isn't blocked by leftover state.
        try:
            os.remove(commands.SCHEDULED_SEND_FILE)
        except FileNotFoundError:
            pass
        try:
            os.remove(commands.SCHEDULED_SEND_RESULT)
        except FileNotFoundError:
            pass
        _restore(snap)


def gate_d_scheduled_completion():
    """Schedule a dry-run job to fire 2 seconds from now, then poll up to
    10s for the result banner. Verify ok=True and one-shot — after the run,
    the lock file is gone and there is no follow-up schedule.
    """
    snap = _snapshot()
    try:
        for p in (commands.SCHEDULED_SEND_FILE,
                  commands.SCHEDULED_SEND_RESULT):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        # Tee the body of the lock file as our paper trail of "ONE" execution.
        target = (datetime.now() + timedelta(seconds=2)).isoformat()
        r1 = commands.start_scheduled_send(
            when_iso=target, dry_run=True, batch_size_cap=2)

        banner = None
        for _ in range(60):
            time.sleep(0.25)
            if os.path.exists(commands.SCHEDULED_SEND_RESULT):
                with open(commands.SCHEDULED_SEND_RESULT,
                          "r", encoding="utf-8") as f:
                    banner = json.load(f)
                break

        # After completion, the in-flight lock should be gone (one-shot).
        lock_still_present = os.path.exists(commands.SCHEDULED_SEND_FILE)
        # And there should be no further schedule pending.
        status_after = commands.scheduled_send_status()

        return {
            "schedule_resp":     r1,
            "banner":            banner,
            "lock_after":        lock_still_present,
            "in_flight_after":   status_after.get("in_flight"),
            "pass": (r1.get("state") == "scheduled"
                     and banner is not None
                     and banner.get("ok") is True
                     and lock_still_present is False
                     and status_after.get("in_flight") is False),
        }
    finally:
        for p in (commands.SCHEDULED_SEND_FILE,
                  commands.SCHEDULED_SEND_RESULT):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        _restore(snap)


def gate_e_regression_chain():
    out = {}
    for name in ("n273_verify.py", "n274_verify.py",
                 "n275a_verify.py", "n275b_verify.py"):
        path = os.path.join(OUT, name)
        if not os.path.exists(path):
            out[name] = {"exit": -1, "pass": False, "reason": "missing"}
            continue
        cp = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=600,
            env=os.environ.copy(),
        )
        passed = '"PASS": true' in cp.stdout
        rec = {"exit": cp.returncode, "pass": passed}
        if not passed:
            rec["stdout_tail"] = cp.stdout[-800:]
            rec["stderr_tail"] = cp.stderr[-400:]
        out[name] = rec
    return out


def main():
    # Outer snapshot-restore wraps every gate. Even if a gate raises after
    # mutating, the original data files come back.
    outer_snap = _snapshot()
    try:
        proof = {}
        proof["a_throttle"]            = gate_a_throttle()
        proof["b_outside_hours"]       = gate_b_outside_hours()
        proof["c_scheduler_lock"]      = gate_c_scheduler_lock()
        proof["d_scheduled_completion"] = gate_d_scheduled_completion()
        proof["e_regression"]          = gate_e_regression_chain()

        pass_keys = ("a_throttle", "b_outside_hours",
                     "c_scheduler_lock", "d_scheduled_completion")
        chain_ok = all(v.get("pass") for v in proof["e_regression"].values())
        gates_ok = all(proof[k].get("pass") for k in pass_keys)
        proof["PASS"] = bool(gates_ok and chain_ok)

        print(json.dumps(proof, indent=2, default=str))
        with open(os.path.join(OUT, "N275C_result.json"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps(proof, indent=2, default=str))
    finally:
        _restore(outer_snap)


if __name__ == "__main__":
    main()
