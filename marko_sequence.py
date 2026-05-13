"""MARKO outbound sequence engine — call/email cadence state machine.

Pure, deterministic functions. Tracks WHICH step each lead is on and
WHEN the next action is due. Nothing here sends mail or makes calls;
it just answers "what should Jay do next on this lead, and is it
overdue?" and emits the field updates Jay's button clicks should
persist.

Step graph (linear, branches at step 2):

    0  NEW              — not yet in any sequence
    1  EMAIL_SENT       — wait 30min → step 2
    2  CALL_DUE         — operator calls and picks a disposition:
                              INTERESTED / BOOKED / CALLBACK → done
                              NOT_INTERESTED / DNC          → done
                              VOICEMAIL                     → step 3
                              (no disposition yet)          → stays at 2
    3  VOICEMAIL_LEFT   — wait 2d → step 4
    4  FOLLOWUP_DUE     — Jay sends follow-up → step 5
    5  FINAL_BUMP_DUE   — wait 3d → done

Lead fields written (only when an event fires):
    sequence_step          int (0..5)
    sequence_next_at       ISO timestamp when next action is due
    sequence_done          bool
    sequence_last_event    str  (the event that produced the last transition)
    sequence_last_event_at ISO timestamp of that transition
    sequence_started_at    ISO timestamp of the first transition
"""
from __future__ import annotations

from datetime import datetime, timedelta


STEP_NEW = 0
STEP_EMAIL_SENT = 1
STEP_CALL_DUE = 2
STEP_VOICEMAIL_LEFT = 3
STEP_FOLLOWUP_DUE = 4
STEP_FINAL_BUMP_DUE = 5
STEP_DONE = -1

STEP_NAMES = {
    0:  "Not started",
    1:  "Email sent — call coming up",
    2:  "Call due now",
    3:  "Voicemail left",
    4:  "Send follow-up email",
    5:  "Send final bump email",
    -1: "Sequence complete",
}

STEP_HINT = {
    0:  "Start by sending the intro email.",
    1:  "Wait ~30 minutes, then call.",
    2:  "Call now — use the opener script.",
    3:  "Follow-up email goes in 2 days.",
    4:  "Send the follow-up email now.",
    5:  "Send the final bump.",
    -1: "Done — move on.",
}

# Wait durations after each transition, in minutes.
WAIT_AFTER = {
    STEP_EMAIL_SENT:     30,                  # 30 min after email -> call
    STEP_VOICEMAIL_LEFT: 60 * 24 * 2,         # 2 days after voicemail -> follow-up
    STEP_FOLLOWUP_DUE:   60 * 24 * 3,         # 3 days after follow-up -> final bump
}


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _format_eta(minutes):
    """Human eta like 'in 28m', 'in 1h 12m', 'in 2d', 'now', '12m ago'."""
    if minutes is None:
        return ""
    if minutes == 0:
        return "now"
    sign = "ago" if minutes < 0 else "in"
    m = abs(int(minutes))
    if m < 60:
        eta = f"{m}m"
    elif m < 60 * 24:
        h, mm = divmod(m, 60)
        eta = f"{h}h {mm}m" if mm else f"{h}h"
    else:
        d, rem = divmod(m, 60 * 24)
        h = rem // 60
        eta = f"{d}d {h}h" if h else f"{d}d"
    return f"{sign} {eta}" if sign == "in" else f"{eta} ago"


def state_for(lead, now=None):
    """Return current sequence state for a lead. Pure read; no writes.

    Output keys:
        step                int  (-1 .. 5)
        step_name           str
        hint                str
        done                bool
        next_at             ISO str or None
        due                 bool  (action ready now)
        overdue_minutes     int   (0 if not overdue / not due)
        eta                 str   (human "in 28m" or "12m ago" or "")
    """
    if now is None:
        now = datetime.now()

    if lead.get("sequence_done"):
        return {
            "step": -1, "step_name": STEP_NAMES[-1], "hint": STEP_HINT[-1],
            "done": True, "next_at": None, "due": False,
            "overdue_minutes": 0, "eta": "",
        }

    step = int(lead.get("sequence_step") or 0)
    next_at = _parse_iso(lead.get("sequence_next_at"))

    # Steps 2 (call), 4 (followup), 5 (final bump) are always operator-due —
    # they are waiting on a human action, not a clock.
    waiting_on_clock = step in WAIT_AFTER and next_at and next_at > now
    due = (step in (STEP_CALL_DUE, STEP_FOLLOWUP_DUE, STEP_FINAL_BUMP_DUE)
           or (next_at is not None and next_at <= now)) and step > STEP_NEW

    overdue_minutes = 0
    eta = ""
    if next_at:
        delta_min = int((next_at - now).total_seconds() // 60)
        eta = _format_eta(delta_min)
        if delta_min < 0:
            overdue_minutes = -delta_min

    return {
        "step": step,
        "step_name": STEP_NAMES.get(step, "Unknown"),
        "hint": STEP_HINT.get(step, ""),
        "done": False,
        "next_at": next_at.isoformat() if next_at else None,
        "due": due and not waiting_on_clock,
        "overdue_minutes": overdue_minutes,
        "eta": eta,
    }


# Event vocabulary the engine accepts. Stays small on purpose so callers
# can't drift the state into nonsense.
VALID_EVENTS = (
    "email_sent",        # 0 -> 1
    "called",            # advances to 2 if behind
    "voicemail",         # 2 -> 3
    "interested",        # 2 -> done
    "booked",            # 2 -> done
    "callback",          # 2 -> done (live convo)
    "not_interested",    # any -> done
    "dnc",               # any -> done
    "followup_sent",     # 4 -> 5
    "final_bump_sent",   # 5 -> done
    "reset",             # back to 0
)


def advance_for_event(lead, event, now=None):
    """Return field updates to apply, given an event. Doesn't write.

    Returns {} when the event doesn't apply (already done, unknown event,
    etc.) so callers can no-op safely.
    """
    if now is None:
        now = datetime.now()
    event = (event or "").lower().strip()
    if event not in VALID_EVENTS:
        return {}

    # 'reset' always applies even when done.
    if event == "reset":
        return {
            "sequence_step": STEP_NEW,
            "sequence_next_at": None,
            "sequence_done": False,
            "sequence_last_event": event,
            "sequence_last_event_at": now.isoformat(),
        }

    if lead.get("sequence_done"):
        return {}

    current = int(lead.get("sequence_step") or 0)
    updates = {
        "sequence_last_event": event,
        "sequence_last_event_at": now.isoformat(),
    }

    def _at(minutes_ahead):
        return (now + timedelta(minutes=minutes_ahead)).isoformat()

    if event == "email_sent":
        updates["sequence_step"] = STEP_CALL_DUE
        updates["sequence_next_at"] = _at(WAIT_AFTER[STEP_EMAIL_SENT])
        if not lead.get("sequence_started_at"):
            updates["sequence_started_at"] = now.isoformat()
    elif event == "called":
        # Generic "I called" with no disposition yet — keep at CALL_DUE so
        # Jay can still log voicemail / booked / etc. afterwards.
        if current < STEP_CALL_DUE:
            updates["sequence_step"] = STEP_CALL_DUE
            updates["sequence_next_at"] = None
    elif event == "voicemail":
        updates["sequence_step"] = STEP_FOLLOWUP_DUE
        updates["sequence_next_at"] = _at(WAIT_AFTER[STEP_VOICEMAIL_LEFT])
    elif event in ("interested", "booked", "callback",
                   "not_interested", "dnc"):
        updates["sequence_done"] = True
        updates["sequence_next_at"] = None
    elif event == "followup_sent":
        updates["sequence_step"] = STEP_FINAL_BUMP_DUE
        updates["sequence_next_at"] = _at(WAIT_AFTER[STEP_FOLLOWUP_DUE])
    elif event == "final_bump_sent":
        updates["sequence_done"] = True
        updates["sequence_next_at"] = None
    else:  # pragma: no cover — gated by VALID_EVENTS above
        return {}

    return updates


# Mapping from dashboard disposition statuses to sequence events, so the
# /lead/<id>/disposition/<value> route can fire the right transition
# without the caller having to know about sequence vocabulary.
DISPOSITION_TO_EVENT = {
    "VOICEMAIL":      "voicemail",
    "INTERESTED":     "interested",
    "BOOKED":         "booked",
    "CALLBACK":       "callback",
    "NOT_INTERESTED": "not_interested",
    "DNC":            "dnc",
    "CLOSED_WON":     "booked",
    "CLOSED_LOST":    "not_interested",
}


def overdue_count(leads, now=None):
    """How many leads have a sequence step due (not done)."""
    if now is None:
        now = datetime.now()
    n = 0
    for l in leads:
        s = state_for(l, now=now)
        if not s["done"] and s["due"]:
            n += 1
    return n


def due_now(leads, limit=10, now=None):
    """Top N leads whose next sequence action is due. Sorted most overdue first."""
    if now is None:
        now = datetime.now()
    out = []
    for l in leads:
        s = state_for(l, now=now)
        if s["done"] or not s["due"]:
            continue
        out.append((l, s))
    out.sort(key=lambda pair: pair[1]["overdue_minutes"], reverse=True)
    return [{"lead": l, "state": s} for l, s in out[:limit]]
