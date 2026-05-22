"""Forecast feature aggregator.

Computes the feature vector for one ticker as-of a target date (default:
7 days before the next earnings, so the forecast is forward-looking).

Features (built from existing TickerWave data — no new scrapers required):
  1. trailing_surprise_avg          — mean surprise_pct over last 4 Q
  2. trailing_beat_rate              — fraction of last 4 Q that beat
  3. eps_trend_qoq                   — avg sequential EPS growth
  4. eps_trend_yoy                   — year-over-year EPS growth
  5. news_sentiment_30d              — sum(impact × sentiment_sign) over last 30d
  6. insider_buy_pressure_60d        — (buys − sells) from promoter_events 60d
  7. order_win_count_60d             — order_intimation filings count 60d
  8. concall_guidance_tone           — last concall regex tone score
  9. sector_momentum_30d             — sector aggregate momentum
 10. market_regime                   — categorical regime label

Returns a `FeatureVector` dataclass that the classifier (F4) consumes.

This module is pure-aggregator: it doesn't predict anything. F3 collects
the inputs; F4 weights them.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ensure sibling modules importable when called from various entry points
_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_SCRAPER = os.path.join(_HERE, '..', 'scraper')
if _SCRAPER not in sys.path:
    sys.path.insert(0, _SCRAPER)


@dataclass
class FeatureVector:
    ticker: str
    as_of: str                                          # ISO date
    target_earnings_date: Optional[str] = None

    # Earnings history
    trailing_surprise_avg: Optional[float] = None       # %
    trailing_surprise_std: Optional[float] = None       # %
    trailing_beat_rate: Optional[float] = None          # 0..1
    eps_trend_qoq: Optional[float] = None
    eps_trend_yoy: Optional[float] = None

    # News / events
    news_sentiment_30d: Optional[float] = None
    news_count_30d: int = 0

    # Insider activity
    insider_buy_pressure_60d: Optional[float] = None    # net buys - sells
    insider_event_count_60d: int = 0

    # Filings
    order_win_count_60d: int = 0
    capex_announce_count_60d: int = 0

    # Concall guidance
    concall_guidance_tone: Optional[float] = None       # -1..+1
    concall_guidance_filing_id: Optional[int] = None
    concall_guidance_insufficient: bool = False

    # Sector + macro
    sector: Optional[str] = None
    sector_momentum_30d: Optional[float] = None
    market_regime: Optional[str] = None                 # categorical label

    # Provenance / debug
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- helpers --------------------------------------------------------------

def _is_pg(db) -> bool:
    return getattr(db, "is_postgres", False)


def _p(sql: str, is_pg: bool) -> str:
    return sql.replace("?", "%s") if is_pg else sql


def _table_exists(db, table: str) -> bool:
    try:
        cur = db.conn.cursor()
        if _is_pg(db):
            cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (table,))
            return bool(cur.fetchone()[0])
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            return cur.fetchone() is not None
    except Exception:
        return False


# --- per-feature computations --------------------------------------------

def _earnings_history(db, ticker: str, lookback: int = 8) -> Dict[str, Optional[float]]:
    is_pg = _is_pg(db)
    cur = db.conn.cursor()
    sql = _p("SELECT eps_estimate, eps_reported, surprise_pct, earnings_date "
             "FROM earnings_reactions WHERE UPPER(ticker) = ? "
             "ORDER BY earnings_date DESC LIMIT ?", is_pg)
    cur.execute(sql, (ticker, lookback))
    rows = cur.fetchall()
    if not rows:
        return {"trailing_surprise_avg": None, "trailing_surprise_std": None,
                "trailing_beat_rate": None, "eps_trend_qoq": None, "eps_trend_yoy": None}

    surprises = [r[2] for r in rows if r[2] is not None]
    eps_rep = [r[1] for r in rows]

    out: Dict[str, Optional[float]] = {}
    if surprises:
        m = sum(surprises) / len(surprises)
        var = sum((x - m) ** 2 for x in surprises) / max(len(surprises) - 1, 1)
        out["trailing_surprise_avg"] = m
        out["trailing_surprise_std"] = math.sqrt(var)
        out["trailing_beat_rate"] = sum(1 for s in surprises if s > 0) / len(surprises)
    else:
        out["trailing_surprise_avg"] = None
        out["trailing_surprise_std"] = None
        out["trailing_beat_rate"] = None

    # QoQ trend = mean sequential growth on eps_reported
    def _seq_growth(arr):
        growths = []
        for i in range(len(arr) - 1):
            new, old = arr[i], arr[i + 1]
            if new is None or old is None or abs(old) < 0.001:
                continue
            growths.append((new - old) / abs(old))
        return sum(growths) / len(growths) if growths else None

    out["eps_trend_qoq"] = _seq_growth(eps_rep)
    if len(eps_rep) >= 5 and eps_rep[0] is not None and eps_rep[4] is not None and abs(eps_rep[4]) > 0.001:
        out["eps_trend_yoy"] = (eps_rep[0] - eps_rep[4]) / abs(eps_rep[4])
    else:
        out["eps_trend_yoy"] = None
    return out


def _news_sentiment(db, ticker: str, days: int = 30) -> Dict[str, Any]:
    """Aggregate per-ticker news quality over the window."""
    is_pg = _is_pg(db)
    cur = db.conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    sql = _p(
        "SELECT impact_score, sentiment FROM events "
        "WHERE UPPER(COALESCE(companies, '')) LIKE ? "
        "AND COALESCE(published_at, created_at) >= ?", is_pg)
    cur.execute(sql, (f"%{ticker}%", cutoff))
    rows = cur.fetchall()
    if not rows:
        return {"news_sentiment_30d": 0.0, "news_count_30d": 0}
    score = 0.0
    for impact, sentiment in rows:
        if impact is None:
            continue
        sign = 0
        s = (sentiment or "").lower()
        if s in ("bullish", "positive"):
            sign = 1
        elif s in ("bearish", "negative"):
            sign = -1
        score += float(impact) * sign / 100.0  # normalize impact_score 0..100 → -1..+1 contribution
    return {"news_sentiment_30d": score, "news_count_30d": len(rows)}


def _insider_pressure(db, ticker: str, days: int = 60) -> Dict[str, Any]:
    if not _table_exists(db, "promoter_events"):
        return {"insider_buy_pressure_60d": None, "insider_event_count_60d": 0}
    is_pg = _is_pg(db)
    cur = db.conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    sql = _p("SELECT event_type, quantity FROM promoter_events "
             "WHERE UPPER(ticker) = ? AND event_date >= ?", is_pg)
    cur.execute(sql, (ticker, cutoff))
    rows = cur.fetchall()
    if not rows:
        return {"insider_buy_pressure_60d": 0.0, "insider_event_count_60d": 0}

    buys = sells = 0
    for et, qty in rows:
        et = (et or "").lower()
        # Buy patterns: sast_acquire, pit_buy, allotment-like; Sell: sast_dispose, pit_sell
        if any(k in et for k in ("buy", "acquire", "allot", "preferential")):
            buys += 1
        elif any(k in et for k in ("sell", "dispose", "pledge", "off-market")):
            sells += 1
    return {"insider_buy_pressure_60d": float(buys - sells),
            "insider_event_count_60d": len(rows)}


def _filing_counts(db, ticker: str, days: int = 60) -> Dict[str, Any]:
    if not _table_exists(db, "filings"):
        return {"order_win_count_60d": 0, "capex_announce_count_60d": 0}
    is_pg = _is_pg(db)
    cur = db.conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    sql = _p("SELECT filing_type, title FROM filings "
             "WHERE UPPER(ticker) = ? AND filed_at >= ?", is_pg)
    cur.execute(sql, (ticker, cutoff))
    rows = cur.fetchall()
    order_wins = 0
    capex = 0
    for ftype, title in rows:
        ft = (ftype or "").lower()
        ttl = (title or "").lower()
        if "order" in ft or "order" in ttl or "contract win" in ttl:
            order_wins += 1
        if "capex" in ttl or "expansion" in ttl or "new plant" in ttl or "greenfield" in ttl:
            capex += 1
    return {"order_win_count_60d": order_wins, "capex_announce_count_60d": capex}


def _concall_tone(db, ticker: str) -> Dict[str, Any]:
    """Get the most recent concall guidance score for the ticker."""
    try:
        from concall_guidance import latest_for_ticker
    except ImportError:
        return {"concall_guidance_tone": None,
                "concall_guidance_filing_id": None,
                "concall_guidance_insufficient": True}
    g = latest_for_ticker(db, ticker)
    if g is None:
        return {"concall_guidance_tone": None,
                "concall_guidance_filing_id": None,
                "concall_guidance_insufficient": True}
    return {"concall_guidance_tone": g.tone_score,
            "concall_guidance_filing_id": g.filing_id,
            "concall_guidance_insufficient": g.insufficient}


def _sector_and_momentum(db, ticker: str, days: int = 30) -> Dict[str, Any]:
    """Resolve ticker → sector via config.STOCK_SECTORS, then aggregate
    recent ticker events impact_score by sector as a momentum proxy."""
    sector = None
    try:
        from config import STOCK_SECTORS  # type: ignore
        sector = STOCK_SECTORS.get(ticker.upper())
    except Exception:
        pass
    if not sector:
        return {"sector": None, "sector_momentum_30d": None}

    # Compute average impact_score × sentiment_sign for all tickers in the sector
    try:
        from config import STOCK_SECTORS  # type: ignore
        peers = [t for t, s in STOCK_SECTORS.items() if s == sector]
    except Exception:
        peers = [ticker]
    is_pg = _is_pg(db)
    cur = db.conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join(["?"] * len(peers))
    if is_pg:
        placeholders = placeholders.replace("?", "%s")
    sql = (f"SELECT impact_score, sentiment FROM events "
           f"WHERE COALESCE(published_at, created_at) >= {'%s' if is_pg else '?'} "
           f"AND (")
    sql += " OR ".join([f"UPPER(COALESCE(companies, '')) LIKE {'%s' if is_pg else '?'}"
                        for _ in peers])
    sql += ")"
    params = [cutoff] + [f"%{p}%" for p in peers]
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    except Exception as e:
        logger.debug(f"sector aggregation query failed: {e}")
        return {"sector": sector, "sector_momentum_30d": None}
    if not rows:
        return {"sector": sector, "sector_momentum_30d": 0.0}
    score = 0.0
    for impact, sentiment in rows:
        if impact is None: continue
        s = (sentiment or "").lower()
        sign = 1 if s in ("bullish", "positive") else (-1 if s in ("bearish", "negative") else 0)
        score += float(impact) * sign / 100.0
    return {"sector": sector, "sector_momentum_30d": score / max(len(rows), 1)}


def _market_regime(db) -> Optional[str]:
    """Use the existing RegimeDetector if available."""
    try:
        from alpha_scoring_engine import RegimeDetector  # type: ignore
        rd = RegimeDetector()
        # The detector typically reads NIFTY/VIX from yfinance internally;
        # if it raises we fall back to "unknown"
        try:
            regime = rd.detect_regime()
            return getattr(regime, "value", str(regime))
        except Exception as e:
            logger.debug(f"regime detection failed: {e}")
            return None
    except ImportError:
        return None


def _next_earnings_date(ticker: str) -> Optional[str]:
    """Pull the next earnings date from yfinance, if any."""
    try:
        import yfinance as yf
        t = yf.Ticker(f"{ticker}.NS")
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
        if ed.index.tz is not None:
            ed.index = ed.index.tz_localize(None)
        now = datetime.utcnow()
        future = ed[ed.index > now]
        if future.empty:
            return None
        return future.sort_index().index[0].strftime("%Y-%m-%d")
    except Exception:
        return None


# --- public API ----------------------------------------------------------

def compute(db, ticker: str, *, as_of: Optional[datetime] = None) -> FeatureVector:
    """Compute the full feature vector for `ticker`.

    `as_of` lets you point-in-time backtest by setting the reference date;
    defaults to now (live forecast).
    """
    tk = ticker.upper().strip().replace(".NS", "").replace(".BO", "")
    as_of = as_of or datetime.utcnow()
    fv = FeatureVector(ticker=tk, as_of=as_of.strftime("%Y-%m-%d"))
    fv.target_earnings_date = _next_earnings_date(tk)

    try:
        h = _earnings_history(db, tk)
        fv.trailing_surprise_avg = h["trailing_surprise_avg"]
        fv.trailing_surprise_std = h["trailing_surprise_std"]
        fv.trailing_beat_rate = h["trailing_beat_rate"]
        fv.eps_trend_qoq = h["eps_trend_qoq"]
        fv.eps_trend_yoy = h["eps_trend_yoy"]
    except Exception as e:
        fv.notes.append(f"earnings_history failed: {e}")

    try:
        n = _news_sentiment(db, tk)
        fv.news_sentiment_30d = n["news_sentiment_30d"]
        fv.news_count_30d = n["news_count_30d"]
    except Exception as e:
        fv.notes.append(f"news_sentiment failed: {e}")

    try:
        ip = _insider_pressure(db, tk)
        fv.insider_buy_pressure_60d = ip["insider_buy_pressure_60d"]
        fv.insider_event_count_60d = ip["insider_event_count_60d"]
    except Exception as e:
        fv.notes.append(f"insider_pressure failed: {e}")

    try:
        f = _filing_counts(db, tk)
        fv.order_win_count_60d = f["order_win_count_60d"]
        fv.capex_announce_count_60d = f["capex_announce_count_60d"]
    except Exception as e:
        fv.notes.append(f"filing_counts failed: {e}")

    try:
        c = _concall_tone(db, tk)
        fv.concall_guidance_tone = c["concall_guidance_tone"]
        fv.concall_guidance_filing_id = c["concall_guidance_filing_id"]
        fv.concall_guidance_insufficient = c["concall_guidance_insufficient"]
    except Exception as e:
        fv.notes.append(f"concall_tone failed: {e}")

    try:
        s = _sector_and_momentum(db, tk)
        fv.sector = s["sector"]
        fv.sector_momentum_30d = s["sector_momentum_30d"]
    except Exception as e:
        fv.notes.append(f"sector_momentum failed: {e}")

    try:
        fv.market_regime = _market_regime(db)
    except Exception as e:
        fv.notes.append(f"market_regime failed: {e}")

    return fv


# --- CLI smoke test -------------------------------------------------------

def main():
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="RELIANCE")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()
    fv = compute(db, args.ticker)
    print(json.dumps(fv.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
