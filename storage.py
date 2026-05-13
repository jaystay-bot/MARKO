"""MARKO storage abstraction (N271).

One read_json / write_json API, two backends, chosen at runtime via
STORAGE_BACKEND env:

    STORAGE_BACKEND=local   (default) -- JSON files on disk
    STORAGE_BACKEND=kv                -- Upstash Redis via REST API

The dispatcher routes every call based on env. The local backend keeps the
existing test suite green; the kv backend unlocks Vercel-persistent prod
once two env vars are wired:

    KV_REST_API_URL       (from Upstash console)
    KV_REST_API_TOKEN     (from Upstash console)

How to flip prod from demo-mode to real:

    1. Vercel Marketplace -> Upstash Redis (free tier, no card).
    2. Connect to the marko project; Vercel injects KV_REST_API_URL and
       KV_REST_API_TOKEN into the project env automatically.
    3. Add STORAGE_BACKEND=kv to the project env (Production scope).
    4. Redeploy. The "demo mode" banner disappears and writes persist
       across cold starts.

Design notes:
- Keys derive from the filename's stem ("leads.json" -> "marko:leads"),
  so the same callsite code serves both backends. No call site needs to
  know which backend is active.
- read_json raises FileNotFoundError on missing path/key, matching the
  old direct-file behavior so existing call sites don't change.
- write_json on local does an atomic tmp+rename. KV is single-key write
  (Upstash is durable; the REST API itself is the commit).
- Concurrency caveat: both backends store one document per "file" (i.e.
  the entire leads list under one key). Two simultaneous writes to the
  same key race; last writer wins. Acceptable for a single-operator
  workflow; not safe for true multi-user.
- Upstash free tier has a ~1MB-per-key limit. marko_log.json is the
  growth risk -- prune or archive when it nears that.

This module is intentionally stdlib-only -- no `redis` or `upstash-redis`
pip dep. Vercel's Python runtime ships urllib; that's all we need.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class StorageNotConfigured(RuntimeError):
    """Raised when STORAGE_BACKEND=kv but credentials are missing."""


def _backend():
    return (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()


def is_kv_backend():
    """Are we configured to use Upstash KV (regardless of cred status)?"""
    return _backend() == "kv"


def _on_vercel():
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def is_persistent():
    """Will writes survive a cold start?

    Local backend is persistent ONLY when not on Vercel serverless (Vercel's
    /tmp is per-invocation). KV backend is persistent when credentials are
    configured. The dashboard's "demo mode" banner reads this to decide
    whether to warn the operator.
    """
    if _backend() == "kv":
        return _kv_configured()
    return not _on_vercel()


# ---------- Local backend (default) ----------

def _read_json_local(path):
    # Match the old commands.load_json contract: raise on missing.
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_local(path, data):
    # Atomic write: tmp + os.replace. Prevents truncation on crash.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------- KV backend (Upstash Redis REST) ----------

KV_NAMESPACE = "marko"


def _kv_url():
    # Vercel's Upstash Marketplace integration prefixes injected env vars
    # with the database alias (e.g. MARKO_KV_REST_API_URL). Prefer the
    # unprefixed name when it looks like a real URL; otherwise fall back
    # to the MARKO_ prefix. A non-URL value (e.g. placeholder description
    # text) is treated as missing.
    for k in ("KV_REST_API_URL", "MARKO_KV_REST_API_URL"):
        v = (os.environ.get(k) or "").strip()
        if v.startswith(("http://", "https://")):
            return v
    return ""


def _kv_token():
    for k in ("KV_REST_API_TOKEN", "MARKO_KV_REST_API_TOKEN"):
        v = (os.environ.get(k) or "").strip()
        # Real Upstash tokens are long opaque strings; reject obvious
        # placeholder text by requiring no whitespace and >=20 chars.
        if v and " " not in v and len(v) >= 20:
            return v
    return ""


def _kv_configured():
    """Both creds present + look real? Returns bool."""
    return bool(_kv_url()) and bool(_kv_token())


def _kv_key_from_path(path):
    """Map a file path to a KV key. e.g. /var/task/leads.json -> marko:leads."""
    base = os.path.basename(path or "")
    stem, _ext = os.path.splitext(base)
    if not stem:
        raise ValueError(f"_kv_key_from_path: cannot derive key from {path!r}")
    return f"{KV_NAMESPACE}:{stem}"


def _kv_creds_or_raise():
    if not _kv_configured():
        raise StorageNotConfigured(
            "STORAGE_BACKEND=kv but KV_REST_API_URL / KV_REST_API_TOKEN are "
            "not set. Provision Upstash Redis via Vercel Marketplace; both "
            "env vars are then auto-injected into the project."
        )
    return (_kv_url().rstrip("/"), _kv_token())


def _kv_request(method, url, token, body=None):
    """Single HTTPS round-trip to Upstash. Returns parsed JSON response."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")
        data = body.encode("utf-8") if isinstance(body, str) else body
    with urllib.request.urlopen(req, data=data, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_json_kv(path):
    base_url, token = _kv_creds_or_raise()
    key = _kv_key_from_path(path)
    try:
        resp = _kv_request("GET", f"{base_url}/get/{key}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(path) from exc
        raise
    raw = resp.get("result") if isinstance(resp, dict) else None
    if raw is None:
        # Upstash returns {"result": null} for missing keys, not 404.
        raise FileNotFoundError(path)
    return json.loads(raw)


def _write_json_kv(path, data):
    base_url, token = _kv_creds_or_raise()
    key = _kv_key_from_path(path)
    payload = json.dumps(data)
    _kv_request("POST", f"{base_url}/set/{key}", token, body=payload)


# ---------- Public API ----------

def read_json(path):
    """Read a JSON document by path. Raises FileNotFoundError if missing.

    The path is interpreted as a filename. Local backend reads it from
    disk; KV backend derives a key from the basename (leads.json -> marko:leads)
    and reads from Upstash. Callers don't need to know which backend is active.
    """
    if _backend() == "kv":
        return _read_json_kv(path)
    return _read_json_local(path)


def write_json(path, data):
    """Write a JSON document to path. Atomic for local backend; single
    SET for KV. Returns None.
    """
    if _backend() == "kv":
        return _write_json_kv(path, data)
    return _write_json_local(path, data)


def backend_info():
    """Diagnostic snapshot. Safe to call from the dashboard or a debug route.

    Never returns the actual token -- only whether it's set.
    """
    return {
        "backend": _backend(),
        "on_vercel": _on_vercel(),
        "persistent": is_persistent(),
        "kv_configured": _kv_configured(),
        "kv_url_set": bool(_kv_url()),
        "kv_token_set": bool(_kv_token()),
    }
