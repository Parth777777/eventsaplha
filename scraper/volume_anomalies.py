"""Daily volume anomaly detection — z-score of today's volume vs 20-day mean.

Feeds the `vol_divergence` factor in scraper/premover.py. Without this table
the premover factor returns empty, which makes the top-50 board sparse.

Source: yfinance daily bars. Cheap (one API call per ticker, batched).

Strategy: z = (today_vol - mean_20) / std_20. We persist rows with |z| >= 1.5
to keep the table small (one row per ticker per anomaly day).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)


def ensure_schema(db) -> None:
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS volume_anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            z_score REAL NOT NULL,
            volume INTEGER,
            avg_volume_20d INTEGER,
            close REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, as_of_date)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_volanom_tkr_date "
        "ON volume_anomalies(ticker, as_of_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_volanom_zscore "
        "ON volume_anomalies(z_score)"
    )
    db.conn.commit()


def _compute_one(symbol: str) -> Dict[str, Any] | None:
    """Pull 30 days of bars, compute today's z-score vs trailing 20-day mean."""
    try:
        import yfinance as yf
    except Exception as exc:
        logger.warning("yfinance unavailable: %s", exc)
        return None

    yf_sym = f"{symbol}.NS"  # default to NSE; falls back via try/except
    try:
        hist = yf.Ticker(yf_sym).history(period="35d", interval="1d", auto_adjust=False)
    except Exception:
        hist = None
    if hist is None or hist.empty or "Volume" not in hist.columns:
        # try BSE listing
        try:
            hist = yf.Ticker(f"{symbol}.BO").history(period="35d", interval="1d", auto_adjust=False)
        except Exception:
            hist = None
        if hist is None or hist.empty:
            return None

    hist = hist.dropna(subset=["Volume"])
    if len(hist) < 21:
        return None

    today = hist.iloc[-1]
    trailing = hist.iloc[-21:-1]  # 20 days excluding today
    mean_v = float(trailing["Volume"].mean())
    std_v = float(trailing["Volume"].std(ddof=0))
    if std_v <= 0:
        return None

    today_v = float(today["Volume"])
    z = (today_v - mean_v) / std_v
    as_of = today.name.strftime("%Y-%m-%d") if hasattr(today.name, "strftime") else str(today.name)[:10]
    return {
        "ticker": symbol.upper(),
        "as_of_date": as_of,
        "z_score": round(z, 3),
        "volume": int(today_v),
        "avg_volume_20d": int(mean_v),
        "close": round(float(today["Close"]), 2),
    }


def compute_and_persist(db, tickers: Iterable[str], min_abs_z: float = 1.5,
                        per_ticker_sleep: float = 0.3) -> Dict[str, Any]:
    """Compute today's volume z-score for each ticker; persist anomalies.

    Returns {'inserted': N, 'detail': {...}} for the scrape_runner.
    """
    ensure_schema(db)
    inserted = 0
    scanned = 0
    skipped = 0
    z_hits: List[Dict[str, Any]] = []
    cur = db.conn.cursor()
    for tk in (tickers or []):
        scanned += 1
        try:
            row = _compute_one(tk)
        except Exception as exc:
            logger.debug("volume_anomaly %s failed: %s", tk, exc)
            row = None
        if not row:
            skipped += 1
            time.sleep(per_ticker_sleep)
            continue
        if abs(row["z_score"]) < min_abs_z:
            time.sleep(per_ticker_sleep)
            continue
        try:
            cur.execute(
                """
                INSERT OR REPLACE INTO volume_anomalies
                (ticker, as_of_date, z_score, volume, avg_volume_20d, close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["ticker"], row["as_of_date"], row["z_score"],
                 row["volume"], row["avg_volume_20d"], row["close"]),
            )
            inserted += cur.rowcount or 0
            z_hits.append(row)
        except Exception as exc:
            logger.debug("volume_anomaly upsert %s: %s", tk, exc)
        time.sleep(per_ticker_sleep)
    db.conn.commit()
    if inserted:
        logger.info("volume_anomalies: %d new rows (scanned=%d skipped=%d)", inserted, scanned, skipped)
    return {
        "inserted": inserted,
        "detail": {
            "scanned": scanned,
            "skipped": skipped,
            "min_abs_z": min_abs_z,
            "top_hits": sorted(z_hits, key=lambda r: abs(r["z_score"]), reverse=True)[:10],
        },
    }


def recent_anomalies(db, days: int = 7, min_abs_z: float = 1.5) -> List[Dict[str, Any]]:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """
        SELECT ticker, as_of_date, z_score, volume, avg_volume_20d, close
        FROM volume_anomalies
        WHERE as_of_date >= date('now', ?) AND ABS(z_score) >= ?
        ORDER BY ABS(z_score) DESC
        LIMIT 100
        """,
        (f"-{days} days", min_abs_z),
    )
    return [dict(r) for r in cur.fetchall()]
