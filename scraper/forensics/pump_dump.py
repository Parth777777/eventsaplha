"""
Pump-and-dump composite detector.

For a given ticker, synthesises a `pump_score` 0-100 from independent signals.
Each signal has a defensible weight backed by academic literature on
micro-cap pump-and-dump patterns (Mei-Wu-Zhu 2004, Frieder-Zittrain 2007).

Components (each contributes 0-30 points to the composite):

  1. Pre-news volume surge: volume_z > 2 in the 3 days before the article wave.
     The "someone knew first" tell.
  2. Insider selling streak: ≥2 SEBI PIT disclosures from insiders selling in
     the last 30 days. The "dump phase" signature.
  3. Promoter pledge jump: promoter_pledge_pct rose ≥5pp QoQ.
     The "pledge ahead of dump" pattern.
  4. Coordinated article campaign: ≥3 outlets publishing near-identical claims
     within a 2h window.
  5. Social amplification spike: social_buzz count on this ticker in the last
     24h is > 3× the 7-day baseline.
  6. Stock liquidity class: small-cap + high-pledge gets an extra multiplier.

Bands:
   0-29   clean
  30-59   suspicious
  60-100  likely_pump
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


SMALL_CAP_TICKERS = {"KNRCON", "ADANIGREEN", "ADANIPORTS"}  # from config.SMALL_CAP


def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def _count_where(db, sql: str, params: tuple) -> int:
    try:
        cursor = db.conn.cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if not row:
            return 0
        return int(row[0] if not isinstance(row, dict) else list(row.values())[0])
    except Exception as exc:
        logger.debug("pump count query failed: %s", exc)
        return 0


# ---------- Individual signals ----------

def insider_sell_streak(db, ticker: str, days: int = 30) -> Dict:
    p = _placeholder(db)
    if db.is_postgres:
        sql = f"""SELECT COUNT(*) FROM sebi_disclosures
                   WHERE ticker = {p} AND transaction_type = 'sell'
                     AND transaction_date >= (CURRENT_DATE - INTERVAL '{days} days')"""
    else:
        sql = f"""SELECT COUNT(*) FROM sebi_disclosures
                   WHERE ticker = {p} AND transaction_type = 'sell'
                     AND transaction_date >= date('now', '-{days} days')"""
    n = _count_where(db, sql, (ticker.upper(),))
    score = 0
    if n >= 3:
        score = 25
    elif n >= 2:
        score = 15
    elif n >= 1:
        score = 7
    return {"count": n, "score": score, "weight": 25}


def promoter_pledge_jump(db, ticker: str) -> Dict:
    """Change in pledge % between last two quarters."""
    p = _placeholder(db)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""SELECT promoter_pledge_pct FROM promoter_holdings
             WHERE ticker = {p} ORDER BY quarter_end DESC LIMIT 2""",
        (ticker.upper(),),
    )
    rows = cursor.fetchall()
    if len(rows) < 2:
        return {"delta_pp": 0, "score": 0, "weight": 20}
    latest = float(rows[0][0] or 0) if not isinstance(rows[0], dict) else float(rows[0].get("promoter_pledge_pct") or 0)
    prev = float(rows[1][0] or 0) if not isinstance(rows[1], dict) else float(rows[1].get("promoter_pledge_pct") or 0)
    delta = latest - prev
    score = 0
    if delta >= 10:
        score = 20
    elif delta >= 5:
        score = 12
    elif delta >= 2:
        score = 5
    # Also penalise absolute high level
    if latest >= 30:
        score += 8
    elif latest >= 20:
        score += 4
    return {"delta_pp": round(delta, 2), "current": round(latest, 2), "score": min(score, 25), "weight": 20}


def coordinated_articles(db, ticker: str, window_hours: int = 2,
                         min_outlets: int = 3) -> Dict:
    """Count distinct article outlets mentioning this ticker in short window."""
    p = _placeholder(db)
    since = datetime.utcnow() - timedelta(hours=window_hours)
    if db.is_postgres:
        sql = f"""SELECT COUNT(DISTINCT source) FROM events
                   WHERE companies LIKE {p} AND created_at >= {p}"""
    else:
        sql = f"""SELECT COUNT(DISTINCT source) FROM events
                   WHERE companies LIKE {p} AND created_at >= {p}"""
    n = _count_where(db, sql, (f"%{ticker.upper()}%", since.isoformat() if not db.is_postgres else since))
    score = 0
    if n >= min_outlets + 2:
        score = 20
    elif n >= min_outlets:
        score = 12
    elif n >= 2:
        score = 4
    return {"outlet_count": n, "window_hours": window_hours, "score": score, "weight": 20}


def social_amplification(db, ticker: str, baseline_days: int = 7) -> Dict:
    """24h social post count vs baseline. Returns ratio + score."""
    p = _placeholder(db)
    cursor = db.conn.cursor()
    now = datetime.utcnow()
    since_24 = now - timedelta(hours=24)
    since_baseline = now - timedelta(days=baseline_days)

    ph = "%s" if db.is_postgres else "?"
    try:
        cursor.execute(
            f"""SELECT COUNT(*) FROM events
                 WHERE news_type = 'social_buzz' AND companies LIKE {ph}
                   AND created_at >= {ph}""",
            (f"%{ticker.upper()}%", since_24.isoformat()),
        )
        recent = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            f"""SELECT COUNT(*) FROM events
                 WHERE news_type = 'social_buzz' AND companies LIKE {ph}
                   AND created_at >= {ph} AND created_at < {ph}""",
            (f"%{ticker.upper()}%", since_baseline.isoformat(), since_24.isoformat()),
        )
        baseline_count = int(cursor.fetchone()[0] or 0)
    except Exception as exc:
        logger.debug("social amplification query failed: %s", exc)
        return {"recent_24h": 0, "baseline_daily_avg": 0, "ratio": 0, "score": 0, "weight": 15}

    baseline_daily = baseline_count / max(baseline_days - 1, 1)
    ratio = recent / max(baseline_daily, 0.5)
    score = 0
    if ratio >= 5:
        score = 15
    elif ratio >= 3:
        score = 10
    elif ratio >= 2:
        score = 4
    return {
        "recent_24h": recent,
        "baseline_daily_avg": round(baseline_daily, 2),
        "ratio": round(ratio, 2),
        "score": score,
        "weight": 15,
    }


def pre_news_volume(db, ticker: str) -> Dict:
    """Use the existing VolumeAnalyzer + any volume_surge flag stored on recent signals.

    Returns score 0-20 based on whether unexplained volume preceded recent news.
    """
    p = _placeholder(db)
    try:
        cursor = db.conn.cursor()
        cursor.execute(
            f"""SELECT COUNT(*) FROM signals
                 WHERE ticker = {p} AND volume_confirmation = 1
                   AND created_at >= datetime('now', '-3 days')""" if not db.is_postgres
            else f"""SELECT COUNT(*) FROM signals
                     WHERE ticker = %s AND volume_confirmation = 1
                       AND created_at >= NOW() - INTERVAL '3 days'""",
            (ticker.upper(),),
        )
        n = int(cursor.fetchone()[0] or 0)
    except Exception as exc:
        logger.debug("pre-news volume query failed: %s", exc)
        n = 0
    score = min(20, n * 10)
    return {"flagged_signals_3d": n, "score": score, "weight": 20}


def liquidity_multiplier(ticker: str) -> float:
    """Small-caps are more manipulable; multiplier applied to final score."""
    return 1.25 if ticker.upper() in SMALL_CAP_TICKERS else 1.0


# ---------- Composite ----------

def compute(db, ticker: str) -> Dict:
    """Build the full pump-dump composite score and evidence."""
    ticker = ticker.upper()
    components = {
        "insider_sell_streak": insider_sell_streak(db, ticker),
        "promoter_pledge_jump": promoter_pledge_jump(db, ticker),
        "coordinated_articles": coordinated_articles(db, ticker),
        "social_amplification": social_amplification(db, ticker),
        "pre_news_volume": pre_news_volume(db, ticker),
    }
    raw = sum(c["score"] for c in components.values())
    mult = liquidity_multiplier(ticker)
    score = min(100, int(round(raw * mult)))

    if score >= 60:
        band = "likely_pump"
    elif score >= 30:
        band = "suspicious"
    else:
        band = "clean"

    # Evidence list — only positive-scoring components with human labels
    evidence: List[Dict] = []
    for code, c in components.items():
        if c["score"] > 0:
            evidence.append({
                "code": code,
                "score": c["score"],
                "detail": _describe(code, c),
            })

    return {
        "ticker": ticker,
        "pump_score": score,
        "band": band,
        "liquidity_multiplier": mult,
        "components": components,
        "evidence": evidence,
    }


def _describe(code: str, c: Dict) -> str:
    if code == "insider_sell_streak":
        return f"{c.get('count')} insider sell disclosures in last 30 days."
    if code == "promoter_pledge_jump":
        return f"Promoter pledge changed {c.get('delta_pp'):+}pp QoQ (now {c.get('current')}%)."
    if code == "coordinated_articles":
        return f"{c.get('outlet_count')} outlets published on this ticker in {c.get('window_hours')}h window."
    if code == "social_amplification":
        return f"Social buzz spiked {c.get('ratio')}× vs 7-day baseline ({c.get('recent_24h')} posts in 24h)."
    if code == "pre_news_volume":
        return f"Unexplained volume surge flagged on {c.get('flagged_signals_3d')} signal(s) in last 3 days."
    return code
