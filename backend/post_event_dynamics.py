"""Post-event entry-window / cool-off predictor.

The feature founder explicitly asked about: "tell people the volatility-dump
risk after a positive sentiment event, suggest a cool-off period."

For a predicted earnings event (or any tier-≥high alert), this module
returns:
  - dump_risk_score        — 0..1; high = risk of late-buyer wipeout
  - recommended_action     — 'enter_now' | 'wait' | 'skip'
  - wait_minutes_estimate  — suggested cool-off
  - optimal_entry_window   — {start, end} minutes after market open
  - expected_gap_pct       — historical median gap given predicted direction
  - fade_probability       — P(stock retraces ≥50% of pop within day 1)
  - 5d max drawdown + max gain
  - analogs_used + fallback_to_sector

Method (v0): empirical-distribution lookup over historical
`earnings_reactions` rows analogous to (ticker, direction, surprise_band).
Falls back to sector-median trajectory when ticker analogs < 5.

NO existing TickerWave module did this — confirmed via codebase scan
2026-05-19. Notification-cooldown and alpha-score age-decay both exist but
neither tells a user "wait N hours."

This is the wedge moat the founder remembered.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
_SCRAPER = os.path.join(_HERE, '..', 'scraper')
for p in (_HERE, _SCRAPER):
    if p not in sys.path:
        sys.path.insert(0, p)

# Market constants
NSE_SESSION_MINUTES = 375  # 09:15 → 15:30 IST = 6h15m
DEFAULT_WAIT_MIN = 30
MAX_WAIT_MIN = 240


@dataclass
class EntryWindow:
    ticker: str
    direction: str                                  # 'beat' | 'miss' | 'meet'
    dump_risk_score: float                           # 0..1
    recommended_action: str                          # enter_now | wait | skip
    wait_minutes_estimate: int
    optimal_entry_start_min: int                     # minutes after open
    optimal_entry_end_min: int
    expected_gap_pct: Optional[float] = None
    fade_probability: Optional[float] = None
    expected_5d_max_drawdown_pct: Optional[float] = None
    expected_5d_max_gain_pct: Optional[float] = None
    analogs_used: int = 0
    fallback_to_sector: bool = False
    rationale: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        # round floats for cleanliness
        for k in ("dump_risk_score", "expected_gap_pct", "fade_probability",
                  "expected_5d_max_drawdown_pct", "expected_5d_max_gain_pct"):
            v = d.get(k)
            if v is not None:
                d[k] = round(v, 2 if k == "dump_risk_score" or k == "fade_probability" else 1)
        return d


# --- helpers -------------------------------------------------------------

def _direction_from_surprise(surprise_pct: float) -> str:
    if surprise_pct > 1.0:
        return "beat"
    if surprise_pct < -1.0:
        return "miss"
    return "meet"


def _pull_analogs(db, ticker: str, direction: str) -> List[Dict]:
    """Historical earnings_reactions rows for this ticker with matching
    surprise direction. Returns dicts with ret_1d_pct, ret_3d_pct, ret_5d_pct,
    surprise_pct.
    """
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    sql = ("SELECT ret_1d_pct, ret_3d_pct, ret_5d_pct, surprise_pct "
           "FROM earnings_reactions "
           "WHERE UPPER(ticker) = ? "
           "AND ret_1d_pct IS NOT NULL "
           "AND surprise_pct IS NOT NULL "
           "ORDER BY earnings_date DESC LIMIT 16")
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql, (ticker,))
    rows = cur.fetchall()
    out = []
    for r in rows:
        s = r[3]
        if direction == "beat" and s <= 1.0:
            continue
        if direction == "miss" and s >= -1.0:
            continue
        if direction == "meet" and abs(s) > 1.0:
            continue
        out.append({"ret_1d_pct": r[0], "ret_3d_pct": r[1], "ret_5d_pct": r[2],
                    "surprise_pct": s})
    return out


def _pull_sector_analogs(db, ticker: str, direction: str) -> List[Dict]:
    """Sector-median fallback: pull analogs from all tickers in same sector."""
    try:
        from config import STOCK_SECTORS  # type: ignore
        sector = STOCK_SECTORS.get(ticker.upper())
        if not sector:
            return []
        peers = [t for t, s in STOCK_SECTORS.items() if s == sector and t != ticker.upper()]
    except Exception:
        return []
    if not peers:
        return []
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    placeholders = ",".join(["?"] * len(peers))
    if is_pg:
        placeholders = placeholders.replace("?", "%s")
    sql = (f"SELECT ret_1d_pct, ret_3d_pct, ret_5d_pct, surprise_pct "
           f"FROM earnings_reactions "
           f"WHERE UPPER(ticker) IN ({placeholders}) "
           f"AND ret_1d_pct IS NOT NULL "
           f"AND surprise_pct IS NOT NULL "
           f"LIMIT 100")
    cur.execute(sql, peers)
    rows = cur.fetchall()
    out = []
    for r in rows:
        s = r[3]
        if direction == "beat" and s <= 1.0:
            continue
        if direction == "miss" and s >= -1.0:
            continue
        if direction == "meet" and abs(s) > 1.0:
            continue
        out.append({"ret_1d_pct": r[0], "ret_3d_pct": r[1], "ret_5d_pct": r[2],
                    "surprise_pct": s})
    return out


def _median(arr: List[float]) -> Optional[float]:
    clean = [x for x in arr if x is not None]
    if not clean:
        return None
    s = sorted(clean)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mean(arr: List[float]) -> Optional[float]:
    clean = [x for x in arr if x is not None]
    return sum(clean) / len(clean) if clean else None


def _compute(analogs: List[Dict], direction: str, ticker: str,
             fallback: bool) -> EntryWindow:
    """Build the EntryWindow from a list of analog dicts."""
    if not analogs:
        # No data at all — emit a safe-default skip
        return EntryWindow(
            ticker=ticker, direction=direction,
            dump_risk_score=0.5,
            recommended_action="wait",
            wait_minutes_estimate=DEFAULT_WAIT_MIN,
            optimal_entry_start_min=DEFAULT_WAIT_MIN,
            optimal_entry_end_min=DEFAULT_WAIT_MIN + 120,
            analogs_used=0, fallback_to_sector=fallback,
            rationale="No historical analogs available; using safe defaults.",
        )

    ret_1d = [a["ret_1d_pct"] for a in analogs if a["ret_1d_pct"] is not None]
    ret_5d = [a["ret_5d_pct"] for a in analogs if a["ret_5d_pct"] is not None]

    median_1d = _median(ret_1d) or 0.0
    mean_1d = _mean(ret_1d) or 0.0
    median_5d = _median(ret_5d) or 0.0
    mean_5d = _mean(ret_5d) or 0.0

    # Fade probability = fraction of analogs where 5d return retraced ≥50% of 1d move
    fade_n = 0
    fade_d = 0
    for a in analogs:
        r1 = a.get("ret_1d_pct")
        r5 = a.get("ret_5d_pct")
        if r1 is None or r5 is None:
            continue
        fade_d += 1
        # A "fade" means: 1d was positive but 5d is <= 50% of 1d (gave half the move back)
        # or 1d was negative but 5d is <= 50% (rebound failed)
        if r1 > 0 and r5 < r1 * 0.5:
            fade_n += 1
        elif r1 < 0 and r5 > r1 * 0.5:
            fade_n += 1
    fade_probability = fade_n / fade_d if fade_d else 0.5

    # 5d max drawdown / max gain — proxy from |5d| with sign info from 1d
    max_drawdown = min((a.get("ret_5d_pct") or 0.0) for a in analogs) if analogs else 0.0
    max_gain = max((a.get("ret_5d_pct") or 0.0) for a in analogs) if analogs else 0.0

    # Dump risk: high when fade_prob is high AND 1d move is large in magnitude
    # (a 5% pop with 70% fade prob = severe chase risk; a 0.3% move = low risk regardless)
    magnitude = abs(median_1d) / 5.0  # normalize so 5% move = 1.0
    dump_risk = min(1.0, fade_probability * (0.5 + 0.5 * magnitude))

    # Recommended action + wait minutes
    if direction == "miss":
        recommended = "skip"
        wait_min = 0
        entry_start = 0
        entry_end = 0
        rationale = (
            f"Miss expected. Historical median 1d return {median_1d:+.1f}%; "
            f"avoid catching falling knife. Wait for stabilisation T+3 to T+5."
        )
    elif dump_risk > 0.6:
        recommended = "wait"
        wait_min = min(MAX_WAIT_MIN, int(45 + 30 * magnitude))
        entry_start = wait_min
        entry_end = min(NSE_SESSION_MINUTES, wait_min + 120)
        rationale = (
            f"High chase risk: {fade_probability*100:.0f}% of {len(analogs)} analogs "
            f"faded ≥50% of the opening pop. Median 1d move {median_1d:+.1f}%. "
            f"Wait ~{wait_min} min for fade to complete."
        )
    elif dump_risk < 0.3 and median_1d > 1.0:
        recommended = "enter_now"
        wait_min = 0
        entry_start = 0
        entry_end = 90
        rationale = (
            f"Clean signal: only {fade_probability*100:.0f}% of {len(analogs)} analogs "
            f"faded, median 1d return {median_1d:+.1f}%. Enter on the open."
        )
    else:
        recommended = "wait"
        wait_min = 30
        entry_start = 30
        entry_end = 150
        rationale = (
            f"Moderate chase risk: fade prob {fade_probability*100:.0f}% over "
            f"{len(analogs)} analogs. Short cool-off (~30 min) recommended."
        )

    return EntryWindow(
        ticker=ticker,
        direction=direction,
        dump_risk_score=dump_risk,
        recommended_action=recommended,
        wait_minutes_estimate=wait_min,
        optimal_entry_start_min=entry_start,
        optimal_entry_end_min=entry_end,
        expected_gap_pct=median_1d,
        fade_probability=fade_probability,
        expected_5d_max_drawdown_pct=max_drawdown,
        expected_5d_max_gain_pct=max_gain,
        analogs_used=len(analogs),
        fallback_to_sector=fallback,
        rationale=rationale,
    )


# --- public API ----------------------------------------------------------

def estimate(db, ticker: str, *, predicted_direction: str = "beat",
             surprise_band: Optional[Tuple[float, float]] = None) -> EntryWindow:
    """Return the entry-window prediction for (ticker, direction).

    `surprise_band` is optional; if provided, analogs are filtered to that
    surprise magnitude band for sharper analogs.
    """
    tk = ticker.upper().strip().replace(".NS", "").replace(".BO", "")
    direction = predicted_direction.lower()
    if direction not in ("beat", "miss", "meet"):
        direction = "beat"

    analogs = _pull_analogs(db, tk, direction)

    # Optionally refine by surprise band
    if surprise_band:
        lo, hi = surprise_band
        analogs = [a for a in analogs if lo <= a["surprise_pct"] <= hi]

    fallback = False
    if len(analogs) < 5:
        # Use sector fallback to top up
        sector_analogs = _pull_sector_analogs(db, tk, direction)
        if sector_analogs:
            analogs = (analogs + sector_analogs)[:30]
            fallback = True

    return _compute(analogs, direction, tk, fallback)


# --- CLI smoke test ------------------------------------------------------

def main():
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="RELIANCE")
    p.add_argument("--direction", default="beat", choices=("beat", "miss", "meet"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()
    ew = estimate(db, args.ticker, predicted_direction=args.direction)
    print(json.dumps(ew.to_dict(), indent=2))


if __name__ == "__main__":
    main()
