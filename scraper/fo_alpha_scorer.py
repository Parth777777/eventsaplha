"""
Tickwave - F&O Alpha Scoring Engine

Computes 0-100 F&O Alpha Score from 5 weighted factors:
  1. OI Buildup Score      (35%) - aggregate unusual OI build-up near spot
  2. IV Rank Score         (20%) - where current ATM IV sits in trailing 252d
  3. PCR Deviation         (15%) - how far PCR is from its 30d mean
  4. FII Derivative Flow   (15%) - direction + magnitude of FII net position change
  5. Catalyst Proximity    (15%) - earnings/events within expiry window

Output: float in [0, 100]. Each factor's contribution is captured in the
returned `factors` dict for UI transparency.

Inputs come from the snapshot dict produced by fo_signals.analyze_chain plus
its enrichments (max_pain, iv_rank already present after enrichment) and the DB
for trailing PCR + FII history + earnings calendar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


WEIGHTS = {
    'oi_buildup': 0.35,
    'iv_rank':    0.20,
    'pcr_dev':    0.15,
    'fii_flow':   0.15,
    'catalyst':   0.15,
}


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def oi_buildup_score(snapshot: Dict) -> Tuple[float, str]:
    """Aggregate unusual OI build-up near spot. Larger total absolute OI change
    relative to median strike OI change → higher score.
    """
    by_strike = snapshot.get("oi_by_strike") or {}
    spot = snapshot.get("spot") or 0
    if not by_strike or not spot:
        return 0.0, "no chain data"
    near = [(k, v) for k, v in by_strike.items() if abs(k - spot) / spot <= 0.10]
    if not near:
        return 0.0, "no strikes near spot"
    abs_changes = [abs(v["call_change"]) + abs(v["put_change"]) for _, v in near]
    if not any(abs_changes):
        return 0.0, "no OI change activity"
    sorted_ch = sorted(abs_changes)
    median = sorted_ch[len(sorted_ch) // 2] or 1
    top = max(abs_changes)
    if median <= 0:
        return 0.0, "flat OI"
    ratio = top / median
    # 1x = 0, 2x = 30, 5x = 70, 10x+ = 100
    if ratio <= 1:
        score = 0.0
    elif ratio <= 2:
        score = 30 * (ratio - 1)
    elif ratio <= 5:
        score = 30 + 40 * ((ratio - 2) / 3.0)
    elif ratio <= 10:
        score = 70 + 30 * ((ratio - 5) / 5.0)
    else:
        score = 100.0
    return _clip(score), f"top OI change {top:,} = {ratio:.1f}x median"


def iv_rank_score(iv_rank: Optional[float]) -> Tuple[float, str]:
    """High IV rank (>80) signals expanded volatility — larger expected moves.
    Low IV rank (<20) signals compression — directional traders should be cautious.
    Score = iv_rank itself, but with a slight S-curve to reward extremes.
    """
    if iv_rank is None:
        return 50.0, "insufficient IV history (neutral)"
    # S-curve: <20 → 0-30, 20-80 → 30-70, >80 → 70-100
    iv = float(iv_rank)
    if iv <= 20:
        score = (iv / 20.0) * 30
    elif iv <= 80:
        score = 30 + ((iv - 20) / 60.0) * 40
    else:
        score = 70 + ((iv - 80) / 20.0) * 30
    return _clip(score), f"IV rank {iv:.0f}"


def pcr_deviation_score(db, ticker: str, pcr: float) -> Tuple[float, str]:
    """Deviation from trailing 30d mean PCR. Reversion-aware:
       PCR>>mean (lots of put writing) often precedes upside,
       PCR<<mean (call-heavy) often precedes downside.
       We score *magnitude* of deviation — both directions count.
    """
    if not pcr:
        return 50.0, "no current PCR"
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT pcr FROM fo_snapshot
               WHERE ticker = ? AND fetched_at >= datetime('now', '-30 days')
                 AND pcr > 0 ORDER BY fetched_at DESC LIMIT 60""",
            (ticker.upper(),),
        )
        history = [r[0] for r in cur.fetchall() if r[0] is not None and r[0] > 0]
        if len(history) < 5:
            return 50.0, f"only {len(history)} trailing PCR samples (neutral)"
        mean_pcr = sum(history) / len(history)
        if mean_pcr <= 0:
            return 50.0, "zero mean PCR (neutral)"
        deviation_pct = abs(pcr - mean_pcr) / mean_pcr
        # 0 = 0, 0.20 (20% off) = 50, 0.50+ = 100
        score = min(100.0, deviation_pct * 200)
        return _clip(score), f"PCR {pcr:.2f} vs 30d mean {mean_pcr:.2f} ({deviation_pct*100:.0f}% deviation)"
    except Exception as e:
        logger.debug(f"pcr_deviation_score {ticker}: {e}")
        return 50.0, "PCR history unavailable (neutral)"


def fii_flow_score(db) -> Tuple[float, str]:
    """Magnitude of FII derivative net position change vs 5-day mean.
       Big swings in FII positioning are high-conviction macro signals.
    """
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT date, fii_index_fut_net, fii_stock_fut_net
               FROM fo_fii_dii_derivatives
               ORDER BY date DESC LIMIT 6"""
        )
        rows = cur.fetchall()
        if len(rows) < 2:
            return 50.0, "no FII history (neutral)"
        latest = rows[0]
        prior = rows[1:]
        latest_total = (latest[1] or 0) + (latest[2] or 0)
        prior_avg = sum((r[1] or 0) + (r[2] or 0) for r in prior) / max(1, len(prior))
        delta = latest_total - prior_avg
        if prior_avg == 0:
            magnitude = abs(latest_total)
        else:
            magnitude = abs(delta) / max(1, abs(prior_avg))
        # magnitude: 0 = 0, 0.5 = 50, 1.5+ = 100
        score = min(100.0, magnitude * 70)
        direction = "long" if delta > 0 else "short"
        return _clip(score), f"FII {direction} swing {delta:+,} vs 5d avg {prior_avg:+,.0f}"
    except Exception as e:
        logger.debug(f"fii_flow_score: {e}")
        return 50.0, "FII data unavailable (neutral)"


def catalyst_proximity_score(db, ticker: str, expiry: str) -> Tuple[float, str]:
    """Higher score if a known catalyst (earnings/event) falls inside the expiry window.
       Looks at the `events` table (typed as 'earnings' or 'corp_event') for ticker.
    """
    if not expiry:
        return 0.0, "no expiry"
    try:
        for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d"):
            try:
                exp_dt = datetime.strptime(expiry, fmt)
                break
            except Exception:
                continue
        else:
            return 0.0, "expiry parse failed"
        days_to_expiry = (exp_dt - datetime.now()).days
        if days_to_expiry <= 0:
            return 0.0, "expiry passed"
        cur = db.conn.cursor()
        # Loose probe — schema variants exist; fall back gracefully
        try:
            cur.execute(
                """SELECT event_date, event_type FROM events
                   WHERE ticker = ? AND date(event_date) BETWEEN date('now') AND date('now', ?)
                   ORDER BY event_date ASC LIMIT 5""",
                (ticker.upper(), f"+{days_to_expiry} days"),
            )
            ev = cur.fetchall()
        except Exception:
            ev = []
        if not ev:
            return 20.0, f"no catalyst within {days_to_expiry}d expiry"
        # Closer catalyst = higher score
        nearest_days = None
        for row in ev:
            try:
                d = datetime.strptime(row[0][:10], "%Y-%m-%d")
                gap = (d - datetime.now()).days
                if nearest_days is None or gap < nearest_days:
                    nearest_days = gap
            except Exception:
                continue
        if nearest_days is None:
            return 40.0, f"{len(ev)} catalysts in window"
        # 0 days = 100, days_to_expiry days = 40
        score = 100 - (nearest_days / max(1, days_to_expiry)) * 60
        return _clip(score), f"catalyst in {nearest_days}d (within {days_to_expiry}d expiry)"
    except Exception as e:
        logger.debug(f"catalyst_proximity_score {ticker}: {e}")
        return 20.0, "catalyst lookup failed (low default)"


def score_snapshot(db, ticker: str, snapshot: Dict) -> Optional[float]:
    """Compute the F&O alpha score (0-100) for this snapshot. Returns None on
    catastrophic failure so caller can skip persistence.

    Side effect: returns float only — caller decides whether to write factor
    breakdown into payload_json. We attach `_factors` to snapshot for that.
    """
    try:
        s_oi, why_oi = oi_buildup_score(snapshot)
        s_iv, why_iv = iv_rank_score(snapshot.get("iv_rank"))
        s_pcr, why_pcr = pcr_deviation_score(db, ticker, snapshot.get("pcr") or 0)
        s_fii, why_fii = fii_flow_score(db)
        s_cat, why_cat = catalyst_proximity_score(db, ticker, snapshot.get("expiry") or "")
        total = (
            s_oi  * WEIGHTS['oi_buildup'] +
            s_iv  * WEIGHTS['iv_rank']    +
            s_pcr * WEIGHTS['pcr_dev']    +
            s_fii * WEIGHTS['fii_flow']   +
            s_cat * WEIGHTS['catalyst']
        )
        snapshot["_factors"] = {
            "oi_buildup": {"score": round(s_oi, 1), "why": why_oi},
            "iv_rank":    {"score": round(s_iv, 1), "why": why_iv},
            "pcr_dev":    {"score": round(s_pcr, 1), "why": why_pcr},
            "fii_flow":   {"score": round(s_fii, 1), "why": why_fii},
            "catalyst":   {"score": round(s_cat, 1), "why": why_cat},
        }
        return round(_clip(total), 1)
    except Exception as e:
        logger.warning(f"fo_alpha_scorer.score_snapshot {ticker}: {e}")
        return None
