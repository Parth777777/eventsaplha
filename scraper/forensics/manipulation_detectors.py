"""Manipulation detectors built on existing data.

- Circular pattern (upper-circuit run + low float + no Tier-1 news)
- Synthetic insider activity (sells > 3x buys + pledge ↑ + auditor change)
- Auditor / CFO change calendar (parsed from BSE corporate announcements)
- News-rewrite distribution detector (clusters with high velocity & low source diversity)

Each detector returns a list of dicts with `ticker`, `signal`, `score`, `evidence`,
`as_of`. Designed to be run on a schedule and stored in `manipulation_flags` table.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def ensure_schema(db) -> None:
    """Create the manipulation_flags table if missing."""
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS manipulation_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                detector TEXT NOT NULL,
                score REAL,
                band TEXT,
                evidence TEXT,
                as_of TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mflag_tkr ON manipulation_flags(ticker, detector)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mflag_age ON manipulation_flags(as_of)")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"manipulation_flags ensure_schema: {e}")


def _persist(db, flags: List[Dict]) -> int:
    if not flags:
        return 0
    cur = db.conn.cursor()
    n = 0
    for f in flags:
        try:
            cur.execute(
                "INSERT INTO manipulation_flags (ticker, detector, score, band, evidence) VALUES (?, ?, ?, ?, ?)",
                (f.get("ticker"), f.get("detector"), float(f.get("score", 0)), f.get("band"),
                 json.dumps(f.get("evidence") or {}, default=str)),
            )
            n += 1
        except Exception as e:
            logger.warning(f"persist flag failed: {e}")
    db.conn.commit()
    return n


# ---- 1) CIRCULAR PATTERN ----------------------------------------------------

def detect_circular_pattern(db, lookback_days: int = 5) -> List[Dict]:
    """Flag tickers hitting upper circuit ≥3 days in 5, low float, no Tier-1 news.

    Heuristic implementation since circuit data isn't directly stored — we
    proxy via daily change_pct >= 4.5% (typical upper-circuit threshold for
    smaller caps) and absence of Tier-1 events for the ticker in the window.
    """
    ensure_schema(db)
    flags: List[Dict] = []
    try:
        cur = db.conn.cursor()
        # Look for tickers with multiple +5%+ days in the window
        cur.execute(
            """
            SELECT ticker, COUNT(*) AS streak,
                   AVG(price_change_pct) AS avg_change,
                   MAX(created_at) AS last_seen
            FROM stocks
            WHERE created_at >= datetime('now', ?)
              AND price_change_pct >= 4.5
            GROUP BY ticker
            HAVING streak >= 3
            """,
            (f"-{lookback_days} days",),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []

    for row in rows:
        ticker = row["ticker"]
        # Check if any Tier-1/2 news exists for this ticker recently
        try:
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE companies LIKE ?
                  AND created_at >= datetime('now', ?)
                  AND (source LIKE 'BSE%' OR source LIKE 'NSE%' OR source LIKE 'SEBI%'
                       OR source LIKE '%moneycontrol%' OR source LIKE '%economictimes%'
                       OR source LIKE '%livemint%' OR source LIKE '%reuters%')
                """,
                (f"%{ticker}%", f"-{lookback_days} days"),
            )
            news_count = int((cur.fetchone() or {"n": 0})["n"] or 0)
        except Exception:
            news_count = 0

        # No legitimate news + sustained upper-circuit moves = operator pattern
        if news_count == 0:
            score = min(100, 40 + int(row["streak"]) * 12 + int(row["avg_change"] or 0) * 2)
            flags.append({
                "ticker": ticker,
                "detector": "circular_pattern",
                "score": score,
                "band": "likely_manipulated" if score >= 70 else "suspicious",
                "evidence": {
                    "streak_days": row["streak"],
                    "avg_change_pct": round(float(row["avg_change"] or 0), 2),
                    "tier1_news_count": news_count,
                    "lookback_days": lookback_days,
                    "narrative": f"{row['streak']} sessions of +{round(float(row['avg_change'] or 0), 1)}% in {lookback_days}d with no Tier-1 news",
                },
            })

    _persist(db, flags)
    return flags


# ---- 2) SYNTHETIC INSIDER ACTIVITY ------------------------------------------

def detect_insider_exit_pattern(db, window_days: int = 90) -> List[Dict]:
    """Flag tickers showing classic exit pattern:
       insider sells >> buys + pledge ↑ + auditor or CFO change.
    """
    ensure_schema(db)
    flags: List[Dict] = []

    # 1) sells vs buys ratio from sebi_disclosures (if table exists)
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            SELECT ticker,
                   SUM(CASE WHEN transaction_type = 'sell' THEN 1 ELSE 0 END) AS sells,
                   SUM(CASE WHEN transaction_type = 'buy'  THEN 1 ELSE 0 END) AS buys
            FROM sebi_disclosures
            WHERE transaction_date >= date('now', ?)
            GROUP BY ticker
            HAVING sells >= 2 AND sells >= buys * 3
            """,
            (f"-{window_days} days",),
        )
        sells_dom = {row["ticker"]: dict(row) for row in cur.fetchall()}
    except Exception:
        sells_dom = {}

    # 2) pledge increase from promoter_holdings (if table exists)
    pledge_inc: Dict[str, float] = {}
    try:
        cur.execute(
            """
            SELECT ticker, MIN(pledge_pct) AS oldest, MAX(pledge_pct) AS latest
            FROM promoter_holdings
            WHERE as_of_date >= date('now', ?)
            GROUP BY ticker
            HAVING latest - oldest >= 3
            """,
            (f"-{window_days} days",),
        )
        for row in cur.fetchall():
            pledge_inc[row["ticker"]] = float(row["latest"] or 0) - float(row["oldest"] or 0)
    except Exception:
        pass

    # 3) auditor / CFO change events from events table
    auditor_changes: Dict[str, str] = {}
    try:
        cur.execute(
            """
            SELECT companies, title, MAX(created_at) AS last_seen
            FROM events
            WHERE (LOWER(title) LIKE '%auditor%' OR LOWER(title) LIKE '%cfo resign%'
                   OR LOWER(title) LIKE '%chief financial officer%resign%'
                   OR LOWER(title) LIKE '%auditor resign%')
              AND created_at >= datetime('now', ?)
            GROUP BY companies
            """,
            (f"-{window_days} days",),
        )
        for row in cur.fetchall():
            comps = row["companies"]
            if isinstance(comps, str):
                for tk in re.split(r"[,\s]+", comps):
                    tk = tk.strip()
                    if tk:
                        auditor_changes[tk] = row["title"]
    except Exception:
        pass

    # Synthesize: any ticker with 2+ of the 3 signals → flag
    all_tickers = set(sells_dom) | set(pledge_inc) | set(auditor_changes)
    for ticker in all_tickers:
        signals = []
        score = 0
        if ticker in sells_dom:
            sd = sells_dom[ticker]
            signals.append(f"insider sells {sd.get('sells')} vs buys {sd.get('buys')}")
            score += 30
        if ticker in pledge_inc:
            signals.append(f"pledge ↑{pledge_inc[ticker]:.1f}pp in {window_days}d")
            score += 25
        if ticker in auditor_changes:
            signals.append("auditor / CFO change")
            score += 30

        if len(signals) >= 2:
            flags.append({
                "ticker": ticker,
                "detector": "insider_exit_pattern",
                "score": score,
                "band": "likely_manipulated" if score >= 60 else "suspicious",
                "evidence": {
                    "signals": signals,
                    "window_days": window_days,
                    "narrative": "Multi-signal insider exit pattern: " + " + ".join(signals),
                },
            })

    _persist(db, flags)
    return flags


# ---- 3) AUDITOR / CFO CHANGE CALENDAR ---------------------------------------

_AUDITOR_RE = re.compile(
    r"(auditor\s+(resign|change|appoint|replace)|"
    r"(chief\s+financial\s+officer|cfo)\s+(resign|step\s+down|exit)|"
    r"company\s+secretary\s+resign|"
    r"resign(?:ed|ation)?\s+as\s+(auditor|cfo|chief\s+financial))",
    re.IGNORECASE,
)


def calendar_auditor_changes(db, window_days: int = 180) -> List[Dict]:
    """Return a list of recent auditor/CFO change events.
    Used by frontend `earnings.html` and `compare.html` as a watchlist overlay.
    """
    out: List[Dict] = []
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            SELECT event_id, title, summary, source, link, companies, created_at
            FROM events
            WHERE created_at >= datetime('now', ?)
              AND (LOWER(title || ' ' || COALESCE(summary, '')) LIKE '%auditor%'
                   OR LOWER(title || ' ' || COALESCE(summary, '')) LIKE '%cfo%resign%'
                   OR LOWER(title || ' ' || COALESCE(summary, '')) LIKE '%resign%cfo%'
                   OR LOWER(title || ' ' || COALESCE(summary, '')) LIKE '%resign%auditor%')
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (f"-{window_days} days",),
        )
        for row in cur.fetchall():
            text = (row["title"] or "") + " " + (row["summary"] or "")
            if not _AUDITOR_RE.search(text):
                continue
            companies = row["companies"]
            if isinstance(companies, str):
                companies = [c.strip() for c in re.split(r"[,\s]+", companies) if c.strip()]
            out.append({
                "event_id": row["event_id"],
                "title": row["title"],
                "tickers": companies or [],
                "source": row["source"],
                "link": row["link"],
                "as_of": row["created_at"],
            })
    except Exception as e:
        logger.warning(f"calendar_auditor_changes: {e}")
    return out


# ---- 4) NEWS-REWRITE DISTRIBUTION DETECTOR ----------------------------------

def detect_coordinated_distribution(articles: List[Dict], window_minutes: int = 120) -> List[Dict]:
    """Flag clusters where many articles in a short window are clearly rewrites
    of each other AND are concentrated in low-tier (3-4) sources.

    Classic operator-driven coverage: a single press release rewritten by
    multiple low-tier portals to manufacture buzz. High velocity + low source
    diversity = distribution pattern.
    """
    try:
        from source_tiering import cluster_events
    except Exception:
        return []
    flags: List[Dict] = []
    clusters = cluster_events(articles or [])
    for cl in clusters:
        if cl.source_count < 3:
            continue
        # Concentration in tier 3-4
        low_tier = [s for s in cl.sources if s.get("tier", 4) >= 3]
        if not low_tier or len(low_tier) / cl.source_count < 0.7:
            continue
        # Fast cadence
        span_min = (cl.latest_ts - cl.earliest_ts) / 60.0
        if span_min > window_minutes:
            continue
        flags.append({
            "ticker": None,
            "detector": "coordinated_distribution",
            "score": min(100, 40 + cl.source_count * 8),
            "band": "suspicious",
            "evidence": {
                "title": cl.canonical_title,
                "source_count": cl.source_count,
                "low_tier_count": len(low_tier),
                "span_minutes": round(span_min, 1),
                "narrative": f"{cl.source_count} low-tier portals republished similar story in {span_min:.0f}min",
            },
        })
    return flags
