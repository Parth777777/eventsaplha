"""TickerWave — earnings reactions backfill worker.

Walks a list of NSE tickers, fetches yfinance earnings_dates + daily price
history, computes T+1 / T+3 / T+5 returns and EPS surprise, and idempotently
upserts into the `earnings_reactions` table.

Why this exists: the v1 endpoint `/api/v1/earnings/reactions/<ticker>` used
to call yfinance live on every request — slow, rate-limited, fragile. With
the table populated, the endpoint is a single indexed SELECT (sub-10ms p99).
Plus the dataset becomes ours: every quarter the archive deepens, and we
can serve it without yfinance ever being in the hot path again.

CLI:
    python earnings_reactions_backfill.py            # default: 50 top tickers, 12q each
    python earnings_reactions_backfill.py --limit 200 --quarters 16
    python earnings_reactions_backfill.py --ticker RELIANCE --quarters 20
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Sane defaults — first-run sweep tuned to finish in ~5-10 min on yfinance
# free-tier without tripping rate limits.
DEFAULT_TICKER_LIMIT = 50
DEFAULT_QUARTERS = 12
YF_REQUEST_DELAY_S = 0.25       # space requests modestly
RETRY_DELAY_S = 5.0             # back off on rate-limit / network errors
MAX_RETRIES = 2


def _top_tickers(limit: int) -> List[str]:
    """Return the highest-priority tickers for backfill: monitored stocks
    (from `config.MONITORED_STOCKS`) first, then the rest of STOCK_UNIVERSE.

    Falls back gracefully if either source is missing — we don't want a
    backfill run to crash because a config file moved.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    seen, out = set(), []
    try:
        from config import MONITORED_STOCKS  # type: ignore
        for t in MONITORED_STOCKS:
            if t and t not in seen:
                seen.add(t); out.append(t)
    except Exception as e:
        logger.debug(f"MONITORED_STOCKS unavailable: {e}")
    try:
        from stock_universe import STOCK_UNIVERSE  # type: ignore
        for t in STOCK_UNIVERSE.keys():
            if t and t not in seen:
                seen.add(t); out.append(t)
            if len(out) >= limit:
                break
    except Exception as e:
        logger.debug(f"STOCK_UNIVERSE unavailable: {e}")
    return out[:limit]


def _compute_reactions(ticker: str, quarters: int) -> List[dict]:
    """Pull one ticker's earnings history + price reactions. Returns a list
    of dicts ready for upsert. Empty list on any failure (logged)."""
    import yfinance as yf
    import pandas as pd  # type: ignore

    t = yf.Ticker(f"{ticker}.NS")
    try:
        ed = t.earnings_dates
    except Exception as e:
        logger.debug(f"{ticker}: earnings_dates fetch failed: {e}")
        return []
    if ed is None or (hasattr(ed, "empty") and ed.empty):
        return []

    # Normalize timezone to naive so comparisons don't blow up
    if ed.index.tz is not None:
        ed = ed.copy()
        ed.index = ed.index.tz_localize(None)

    now = pd.Timestamp.now()
    past = ed[ed.index <= now].head(quarters)
    if past.empty:
        return []

    earliest = past.index.min() - pd.Timedelta(days=10)
    try:
        hist = t.history(start=earliest, period=None, interval="1d", auto_adjust=False)
    except Exception as e:
        logger.debug(f"{ticker}: history fetch failed: {e}")
        return []
    if hist is None or hist.empty:
        return []

    closes = hist["Close"].dropna()
    if closes.index.tz is not None:
        closes.index = closes.index.tz_localize(None)

    out = []
    for dt, row in past.iterrows():
        anchor = pd.Timestamp(dt)
        if anchor.tz is not None:
            anchor = anchor.tz_localize(None)
        cutoff = closes[closes.index <= anchor]
        if cutoff.empty:
            continue
        base_price = float(cutoff.iloc[-1])
        base_date = cutoff.index[-1]
        future = closes[closes.index > base_date]

        def _ret(n):
            if len(future) < n:
                return None
            try:
                return round((float(future.iloc[n - 1]) - base_price) / base_price * 100, 2)
            except (ZeroDivisionError, ValueError):
                return None

        est = row.get("EPS Estimate") if hasattr(row, "get") else None
        rep = row.get("Reported EPS") if hasattr(row, "get") else None
        try:
            surprise_pct = None
            if est is not None and rep is not None and not pd.isna(est) and not pd.isna(rep):
                if float(est) != 0:
                    surprise_pct = round((float(rep) - float(est)) / abs(float(est)) * 100, 2)
        except Exception:
            surprise_pct = None

        out.append({
            "ticker": ticker.upper(),
            "earnings_date": str(anchor.date())[:10],
            "base_close": round(base_price, 2),
            "ret_1d_pct": _ret(1),
            "ret_3d_pct": _ret(3),
            "ret_5d_pct": _ret(5),
            "eps_estimate": float(est) if est is not None and not pd.isna(est) else None,
            "eps_reported": float(rep) if rep is not None and not pd.isna(rep) else None,
            "surprise_pct": surprise_pct,
        })
    return out


def _upsert(db, rows: Iterable[dict]) -> int:
    """Idempotent upsert. ON CONFLICT (ticker, earnings_date) DO UPDATE so a
    re-run picks up revised EPS or fills in missing T+5 returns once enough
    trading days have passed."""
    n = 0
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    for r in rows:
        try:
            if is_pg:
                cur.execute(
                    """INSERT INTO earnings_reactions
                          (ticker, earnings_date, base_close,
                           ret_1d_pct, ret_3d_pct, ret_5d_pct,
                           eps_estimate, eps_reported, surprise_pct)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ticker, earnings_date) DO UPDATE SET
                          base_close = COALESCE(EXCLUDED.base_close, earnings_reactions.base_close),
                          ret_1d_pct = COALESCE(EXCLUDED.ret_1d_pct, earnings_reactions.ret_1d_pct),
                          ret_3d_pct = COALESCE(EXCLUDED.ret_3d_pct, earnings_reactions.ret_3d_pct),
                          ret_5d_pct = COALESCE(EXCLUDED.ret_5d_pct, earnings_reactions.ret_5d_pct),
                          eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings_reactions.eps_estimate),
                          eps_reported = COALESCE(EXCLUDED.eps_reported, earnings_reactions.eps_reported),
                          surprise_pct = COALESCE(EXCLUDED.surprise_pct, earnings_reactions.surprise_pct),
                          ingested_at = CURRENT_TIMESTAMP""",
                    (r["ticker"], r["earnings_date"], r["base_close"],
                     r["ret_1d_pct"], r["ret_3d_pct"], r["ret_5d_pct"],
                     r["eps_estimate"], r["eps_reported"], r["surprise_pct"]))
            else:
                # SQLite UPSERT (3.24+). Same semantics: keep newer non-null
                # values when re-running.
                cur.execute(
                    """INSERT INTO earnings_reactions
                          (ticker, earnings_date, base_close,
                           ret_1d_pct, ret_3d_pct, ret_5d_pct,
                           eps_estimate, eps_reported, surprise_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(ticker, earnings_date) DO UPDATE SET
                          base_close = COALESCE(excluded.base_close, earnings_reactions.base_close),
                          ret_1d_pct = COALESCE(excluded.ret_1d_pct, earnings_reactions.ret_1d_pct),
                          ret_3d_pct = COALESCE(excluded.ret_3d_pct, earnings_reactions.ret_3d_pct),
                          ret_5d_pct = COALESCE(excluded.ret_5d_pct, earnings_reactions.ret_5d_pct),
                          eps_estimate = COALESCE(excluded.eps_estimate, earnings_reactions.eps_estimate),
                          eps_reported = COALESCE(excluded.eps_reported, earnings_reactions.eps_reported),
                          surprise_pct = COALESCE(excluded.surprise_pct, earnings_reactions.surprise_pct),
                          ingested_at = CURRENT_TIMESTAMP""",
                    (r["ticker"], r["earnings_date"], r["base_close"],
                     r["ret_1d_pct"], r["ret_3d_pct"], r["ret_5d_pct"],
                     r["eps_estimate"], r["eps_reported"], r["surprise_pct"]))
            n += 1
        except Exception as e:
            logger.warning(f"upsert {r.get('ticker')}/{r.get('earnings_date')} failed: {e}")
    db.conn.commit()
    return n


def run_backfill(db, *, tickers: Optional[List[str]] = None,
                 limit: int = DEFAULT_TICKER_LIMIT,
                 quarters: int = DEFAULT_QUARTERS) -> dict:
    """Main entry point. Backfills earnings_reactions for `tickers` (or the
    top `limit` from MONITORED_STOCKS + STOCK_UNIVERSE).

    Returns a summary dict suitable for the admin endpoint response.
    """
    targets = tickers or _top_tickers(limit)
    if not targets:
        return {"tickers_attempted": 0, "rows_upserted": 0, "errors": []}

    total_rows = 0
    errors: List[Tuple[str, str]] = []
    started = time.time()
    logger.info(f"earnings_reactions backfill: {len(targets)} tickers × {quarters} quarters")

    for i, tk in enumerate(targets):
        attempts = 0
        rows: List[dict] = []
        while attempts <= MAX_RETRIES:
            try:
                rows = _compute_reactions(tk, quarters)
                break
            except Exception as e:
                attempts += 1
                if attempts > MAX_RETRIES:
                    errors.append((tk, str(e)))
                    break
                time.sleep(RETRY_DELAY_S * attempts)
        if rows:
            try:
                n = _upsert(db, rows)
                total_rows += n
                logger.info(f"  [{i+1}/{len(targets)}] {tk}: {n} rows")
            except Exception as e:
                errors.append((tk, f"upsert: {e}"))
        else:
            logger.debug(f"  [{i+1}/{len(targets)}] {tk}: no data")
        time.sleep(YF_REQUEST_DELAY_S)

    elapsed = time.time() - started
    summary = {
        "tickers_attempted": len(targets),
        "rows_upserted": total_rows,
        "errors": [{"ticker": t, "error": e} for t, e in errors[:20]],
        "elapsed_seconds": round(elapsed, 1),
        "completed_at": datetime.utcnow().isoformat() + "Z",
    }
    logger.info(f"earnings_reactions backfill complete: {summary['rows_upserted']} "
                f"rows across {summary['tickers_attempted']} tickers in {summary['elapsed_seconds']}s")
    return summary


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=DEFAULT_TICKER_LIMIT,
                   help=f"Max tickers to backfill (default: {DEFAULT_TICKER_LIMIT})")
    p.add_argument("--quarters", type=int, default=DEFAULT_QUARTERS,
                   help=f"Quarters of history per ticker (default: {DEFAULT_QUARTERS})")
    p.add_argument("--ticker", action="append",
                   help="Specific ticker to backfill (repeatable). Overrides --limit.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()

    summary = run_backfill(
        db,
        tickers=args.ticker,
        limit=args.limit,
        quarters=args.quarters,
    )
    import json as _json
    print(_json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
