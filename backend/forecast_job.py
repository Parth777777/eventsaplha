"""Daily forecast batch job.

For each ticker with an upcoming earnings_date in the next N days:
  1. Skip if within blackout (T-2 to T+0) — too close to leak risk.
  2. Compute baseline + features + classifier + entry_window.
  3. Add LLM narrative summary (async / cheap).
  4. Upsert into earnings_forecasts.

Reuses the same ticker priority as `earnings_reactions_backfill.py`:
MONITORED_STOCKS first, then STOCK_UNIVERSE.

CLI:
    python forecast_job.py                    # default: 50 tickers, 30-day forward window
    python forecast_job.py --limit 100 --days-ahead 45
    python forecast_job.py --ticker RELIANCE  # one-shot
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
for p in (_HERE, os.path.join(_HERE, '..', 'scraper')):
    if p not in sys.path:
        sys.path.insert(0, p)


BLACKOUT_DAYS = 2  # don't forecast within T-2 of earnings (leak risk)


def _top_tickers(limit: int) -> List[str]:
    seen, out = set(), []
    try:
        from config import MONITORED_STOCKS  # type: ignore
        for t in MONITORED_STOCKS:
            tk = (t or "").upper().strip()
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
    except Exception as e:
        logger.debug(f"MONITORED_STOCKS load failed: {e}")
    try:
        from config import STOCK_UNIVERSE  # type: ignore
        for t in STOCK_UNIVERSE:
            tk = (t or "").upper().strip()
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
    except Exception as e:
        logger.debug(f"STOCK_UNIVERSE load failed: {e}")
    return out[:limit]


def _has_upcoming_earnings(ticker: str, days_ahead: int) -> Optional[str]:
    """Return ISO date of next earnings if within window AND not in blackout."""
    try:
        import yfinance as yf
        t = yf.Ticker(f"{ticker}.NS")
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
        if ed.index.tz is not None:
            ed.index = ed.index.tz_localize(None)
        now = datetime.utcnow()
        future = ed[ed.index > now].sort_index()
        if future.empty:
            return None
        nxt = future.index[0]
        days_until = (nxt - now).days
        if days_until > days_ahead:
            return None
        if days_until <= BLACKOUT_DAYS:
            logger.info(f"  {ticker}: within blackout (T-{days_until}); skipping")
            return None
        return nxt.strftime("%Y-%m-%d")
    except Exception as e:
        logger.debug(f"  {ticker}: earnings_dates lookup failed: {e}")
        return None


def run_job(db, *, limit: int = 50, days_ahead: int = 30,
            tickers: Optional[List[str]] = None,
            add_llm: bool = True) -> dict:
    started = time.time()
    if tickers:
        ticker_list = [t.upper().strip() for t in tickers]
    else:
        ticker_list = _top_tickers(limit)

    from forecast_orchestrator import compute_full_forecast, upsert
    try:
        from forecast_llm import add_narrative
    except ImportError:
        add_narrative = None

    processed = 0
    skipped_no_earnings = 0
    skipped_blackout = 0
    failures = 0
    upserted = 0

    for tk in ticker_list:
        next_date = _has_upcoming_earnings(tk, days_ahead)
        if not next_date:
            skipped_no_earnings += 1
            continue
        try:
            fc = compute_full_forecast(db, tk)
            if not fc.get("target_earnings_date"):
                skipped_no_earnings += 1
                continue
            if add_llm and add_narrative is not None:
                try:
                    fc = add_narrative(fc)
                except Exception as e:
                    logger.debug(f"  {tk}: narrative failed: {e}")
            try:
                upsert(db, fc)
                upserted += 1
            except Exception as e:
                logger.warning(f"  {tk}: upsert failed: {e}")
                failures += 1
            processed += 1
            hit = fc.get("predicted_hit", "?")
            conf = fc.get("hit_confidence", 0)
            ew = fc.get("entry_window") or {}
            action = ew.get("recommended_action", "?")
            logger.info(f"  {tk}: predicted={hit} conf={conf:.0%} action={action} target={next_date}")
            # Be courteous to yfinance
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"  {tk}: forecast failed: {e}")
            failures += 1

    elapsed = time.time() - started
    summary = {
        "tickers_considered": len(ticker_list),
        "processed": processed,
        "upserted": upserted,
        "skipped_no_earnings": skipped_no_earnings,
        "skipped_blackout": skipped_blackout,
        "failures": failures,
        "elapsed_seconds": round(elapsed, 1),
        "completed_at": datetime.utcnow().isoformat() + "Z",
    }
    logger.info(f"forecast_job complete: {summary}")
    return summary


def main():
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--days-ahead", type=int, default=30)
    p.add_argument("--ticker", action="append", help="One or more tickers (override universe)")
    p.add_argument("--no-llm", action="store_true", help="Skip LLM narrative augmentation")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()
    summary = run_job(db, limit=args.limit, days_ahead=args.days_ahead,
                      tickers=args.ticker, add_llm=not args.no_llm)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
