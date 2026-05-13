"""N275A: thin Resend HTTP client.

Single entry point: send(to, subject, body, from_, reply_to, dry_run) ->
{id, status}. Reads RESEND_API_KEY from env. Never prints, returns, or logs
the API key. Dry-run is the default safety belt: if dry_run=True, returns
a synthetic id without ever opening a socket.

Status values:
  - "sent"     : Resend accepted the message and returned an id
  - "dry_run"  : dry_run=True path; no HTTP call made
  - "blocked"  : RESEND_API_KEY missing — refused to send
  - "failed"   : HTTP error or non-2xx from Resend
"""
import json as _json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

API_URL = "https://api.resend.com/emails"


def _redact_key(s):
    """Belt-and-suspenders: scrub the API key out of any string we might
    accidentally bubble up through an exception/logging path."""
    key = os.environ.get("RESEND_API_KEY") or ""
    if key and isinstance(s, str) and key in s:
        return s.replace(key, "***")
    return s


def send(to: str,
         subject: str,
         body: str,
         from_: str,
         reply_to: Optional[str] = None,
         dry_run: bool = True,
         api_url: str = API_URL,
         timeout: float = 10.0,
         headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Send one email through Resend's HTTP API.

    Returns {"id": str, "status": str, "error": str|None}.
    Never raises for HTTP-level problems — surfaces them in the status/error
    fields so callers can decide retry/log behavior.

    N275B: ``headers`` is forwarded as Resend's custom-headers field so MARKO
    can stamp X-Marko-Lead-Id on every send. The webhook handler reads it back
    from data.headers and resolves the lead id even when Resend strips tags.
    """
    if dry_run:
        return {"id": f"dry-{abs(hash((to, subject))) % 10**10}",
                "status": "dry_run", "error": None}

    key = os.environ.get("RESEND_API_KEY") or ""
    if not key.strip():
        return {"id": None, "status": "blocked",
                "error": "RESEND_API_KEY not set"}

    payload = {
        "from": from_,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if headers:
        # Resend accepts custom headers as a flat name->value dict.
        payload["headers"] = {str(k): str(v) for k, v in headers.items()}

    req = urllib.request.Request(
        api_url,
        data=_json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Resend's API sits behind Cloudflare, which 403s the default
            # Python-urllib/X.Y UA (CF error code 1010). Send a stable
            # operator-identifying UA so the request gets through.
            "User-Agent": "MARKO/1.0 (+marko-engine)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = _json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            return {"id": data.get("id"), "status": "sent", "error": None}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        msg = f"HTTP {exc.code}"
        if detail:
            # Redact the key before passing the body upstream just in case
            # Resend echoes the auth header (it doesn't, but defense in depth).
            msg += ": " + _redact_key(detail[:300])
        return {"id": None, "status": "failed", "error": msg}
    except urllib.error.URLError as exc:
        return {"id": None, "status": "failed",
                "error": f"network: {_redact_key(str(exc.reason))}"}
    except Exception as exc:  # pragma: no cover — unexpected
        return {"id": None, "status": "failed",
                "error": f"{type(exc).__name__}: {_redact_key(str(exc))}"}
