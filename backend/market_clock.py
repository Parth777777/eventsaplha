"""NSE/BSE market clock — used everywhere we need to know "is the market live right now?".

NSE/BSE main session: Mon-Fri 09:15-15:30 IST.
Pre-open:               09:00-09:15 IST.
Post-close special:     15:40-16:00 IST (block deal window — not main trading).

Exposes:
  is_market_open(now=None)         -> bool       (True only during 09:15-15:30 IST Mon-Fri)
  session_phase(now=None)          -> str        ('pre_open' | 'open' | 'post_close' | 'closed' | 'weekend')
  next_open(now=None)              -> datetime   (UTC) of the next session start
  clock_payload(now=None)          -> dict       (JSON-ready snapshot for the UI banner)

The 2026 NSE trading-holiday list is hand-coded — these are the static known days.
Pre-1900 IST and after 1830 IST a weekday gets bucketed as 'closed' rather than 'post_close'.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone, time
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

OPEN_TIME       = time(9, 15)
CLOSE_TIME      = time(15, 30)
PRE_OPEN_START  = time(9, 0)
POST_CLOSE_END  = time(16, 0)

# NSE 2026 trading-holiday calendar (Indian Republic Day onward). Reuse for BSE.
HOLIDAYS_2026 = {
    "2026-01-26",   # Republic Day
    "2026-03-04",   # Mahashivratri
    "2026-03-13",   # Holi
    "2026-04-03",   # Good Friday
    "2026-04-14",   # Dr. B.R. Ambedkar Jayanti
    "2026-05-01",   # Maharashtra Day
    "2026-08-15",   # Independence Day
    "2026-10-02",   # Gandhi Jayanti
    "2026-11-09",   # Diwali (Lakshmi Pujan — special muhurat session, full day closed otherwise)
    "2026-12-25",   # Christmas
}


def _now_ist(now: Optional[datetime] = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(IST)


def is_holiday(d: datetime) -> bool:
    return d.strftime("%Y-%m-%d") in HOLIDAYS_2026


def is_market_open(now: Optional[datetime] = None) -> bool:
    ist = _now_ist(now)
    if ist.weekday() >= 5:       # Saturday=5, Sunday=6
        return False
    if is_holiday(ist):
        return False
    t = ist.time()
    return OPEN_TIME <= t < CLOSE_TIME


def session_phase(now: Optional[datetime] = None) -> str:
    ist = _now_ist(now)
    if ist.weekday() >= 5:
        return "weekend"
    if is_holiday(ist):
        return "holiday"
    t = ist.time()
    if PRE_OPEN_START <= t < OPEN_TIME:
        return "pre_open"
    if OPEN_TIME <= t < CLOSE_TIME:
        return "open"
    if CLOSE_TIME <= t < POST_CLOSE_END:
        return "post_close"
    return "closed"


def next_open(now: Optional[datetime] = None) -> datetime:
    """Next 09:15 IST that is a trading day. Returns a UTC datetime."""
    ist = _now_ist(now)
    candidate = ist.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute,
                            second=0, microsecond=0)
    # Roll forward until we land on a working day & we haven't already passed today's open
    while True:
        if candidate <= ist:
            candidate = candidate + timedelta(days=1)
            candidate = candidate.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute,
                                          second=0, microsecond=0)
        if candidate.weekday() < 5 and not is_holiday(candidate):
            break
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute,
                                      second=0, microsecond=0)
    return candidate.astimezone(timezone.utc)


def clock_payload(now: Optional[datetime] = None) -> dict:
    """JSON-ready snapshot for the front-end banner."""
    ist = _now_ist(now)
    phase = session_phase(ist)
    nxt   = next_open(ist)
    delta = nxt - ist if phase != "open" else timedelta(0)
    return {
        "now_ist":      ist.isoformat(),
        "phase":        phase,
        "is_open":      phase == "open",
        "next_open_utc": nxt.isoformat(),
        "next_open_ist": nxt.astimezone(IST).isoformat(),
        "minutes_to_open": int(delta.total_seconds() // 60) if phase != "open" else 0,
        "label":        {
            "pre_open":   "Pre-open · session begins at 09:15 IST",
            "open":       "Live · NSE & BSE",
            "post_close": "Closed · main session ended at 15:30 IST",
            "closed":     "After-hours · market resumes at 09:15 IST",
            "weekend":    "Weekend · market resumes Monday 09:15 IST",
            "holiday":    "Trading holiday · NSE/BSE closed today",
        }.get(phase, "Closed"),
    }


__all__ = [
    "IST", "is_market_open", "session_phase", "next_open",
    "clock_payload", "is_holiday",
]
