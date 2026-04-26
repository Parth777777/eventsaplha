"""
Batch prediction resolver — ticker-grouped, cache-once strategy.

Motivation: the default PredictionTracker issues one yfinance.download() per
prediction, which blows through rate limits when you have thousands pending.
This resolver groups predictions by ticker, fetches a single 90-day price
window per ticker, and resolves every prediction against that in-memory frame.

Typical speedup: 30 predictions/run → 3000+ predictions/run.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from database_schema import TickwaveDB

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"1D": 1, "3D": 3, "5D": 5, "20D": 20}


def _target_date(created_at: Any, horizon: str) -> Optional[datetime]:
    days = HORIZON_DAYS.get(horizon, 1)
    if created_at is None:
        return None
    try:
        if isinstance(created_at, str):
            created = datetime.fromisoformat(created_at.replace("Z", "").split(".")[0])
        else:
            created = created_at
        return created + timedelta(days=days)
    except Exception:
        return None


def _closest_close(df: pd.DataFrame, target: datetime, symbol: str) -> Optional[float]:
    """Return the close on-or-just-before target_date; None if no valid row."""
    if df is None or df.empty:
        return None
    target_ts = pd.Timestamp(target).normalize()
    if df.index.tz is not None:
        target_ts = target_ts.tz_localize(df.index.tz)
    eligible = df.index[df.index <= target_ts]
    if len(eligible) == 0:
        return None
    close_col = "Close"
    if isinstance(df.columns, pd.MultiIndex):
        try:
            close_col = ("Close", symbol)
            return float(df.loc[eligible[-1], close_col])
        except KeyError:
            return None
    try:
        return float(df.loc[eligible[-1], close_col])
    except Exception:
        return None


def _fetch_ticker(symbol: str, start: datetime, end: datetime,
                   retries: int = 3, backoff: float = 2.0) -> Optional[pd.DataFrame]:
    """Download a single ticker window with exponential backoff on rate limits."""
    for attempt in range(retries):
        try:
            df = yf.download(
                symbol,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is not None and not df.empty:
                return df
        except Exception as exc:
            logger.debug("yf download attempt=%d symbol=%s err=%s", attempt + 1, symbol, exc)
        time.sleep(backoff * (2 ** attempt))
    return None


def _hit_target(predicted: float, actual: float) -> bool:
    """Direction-match hit criterion (same as existing tracker)."""
    if predicted >= 0:
        return actual > 0
    return actual < 0


def resolve_all(
    db: TickwaveDB,
    max_tickers: Optional[int] = None,
    per_ticker_sleep: float = 0.5,
    progress_every: int = 10,
) -> Dict[str, Any]:
    """Resolve every past-horizon prediction. Groups by ticker, one fetch each.

    Returns summary with counts, per-ticker results, and any failures.
    """
    # Pull all expired predictions — we'll batch regardless of limit
    expired = db.get_expired_predictions(limit=10000)
    if not expired:
        return {"checked": 0, "updated": 0, "hit": 0, "miss": 0, "tickers": 0}

    grouped: Dict[str, List[Dict]] = defaultdict(list)
    for p in expired:
        t = p.get("ticker")
        if t:
            grouped[t].append(dict(p))

    logger.info("batch resolver: %d predictions across %d tickers", len(expired), len(grouped))

    checked = updated = hits = misses = 0
    failed_tickers: List[str] = []
    per_ticker: Dict[str, Dict[str, int]] = {}

    tickers = list(grouped.keys())
    if max_tickers is not None:
        tickers = tickers[:max_tickers]

    for i, ticker in enumerate(tickers, 1):
        preds = grouped[ticker]
        symbol = f"{ticker}.NS"

        # Window: earliest created_at -2d → latest target +3d
        created_list = [p.get("created_at") for p in preds if p.get("created_at")]
        if not created_list:
            continue
        try:
            created_dts = [datetime.fromisoformat(str(c).replace("Z", "").split(".")[0]) for c in created_list]
        except Exception:
            continue
        start = min(created_dts) - timedelta(days=2)
        # Use 20D horizon as ceiling even if this ticker has no 20D preds
        end = datetime.now() + timedelta(days=3)

        df = _fetch_ticker(symbol, start, end)
        if df is None:
            failed_tickers.append(ticker)
            continue

        per_ticker[ticker] = {"attempted": len(preds), "updated": 0, "hits": 0, "misses": 0}
        for p in preds:
            checked += 1
            horizon = p.get("horizon", "1D")
            created = p.get("created_at")
            entry = p.get("entry_price") or 0
            predicted = p.get("predicted_return_pct") or 0
            if entry <= 0:
                continue
            target = _target_date(created, horizon)
            if target is None or target > datetime.now():
                continue
            price = _closest_close(df, target, symbol)
            if price is None:
                continue
            actual_ret = (price - entry) / entry * 100.0
            hit = _hit_target(predicted, actual_ret)
            ok = db.update_prediction_actual(
                prediction_id=p["id"],
                actual_price=price,
                actual_return_pct=round(actual_ret, 4),
                hit_target=hit,
            )
            if ok:
                updated += 1
                per_ticker[ticker]["updated"] += 1
                if hit:
                    hits += 1
                    per_ticker[ticker]["hits"] += 1
                else:
                    misses += 1
                    per_ticker[ticker]["misses"] += 1

        if i % progress_every == 0:
            logger.info("  progress: %d/%d tickers · %d resolved · %d hits",
                        i, len(tickers), updated, hits)

        if per_ticker_sleep:
            time.sleep(per_ticker_sleep)

    hit_rate = hits / max(updated, 1)
    summary = {
        "checked": checked,
        "updated": updated,
        "hit": hits,
        "miss": misses,
        "hit_rate": round(hit_rate, 4),
        "tickers": len(tickers),
        "failed_tickers": failed_tickers,
        "per_ticker": per_ticker,
    }
    logger.info("batch resolver complete: %s", {k: v for k, v in summary.items() if k != "per_ticker"})
    return summary


if __name__ == "__main__":
    import os
    import sys
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db = TickwaveDB()
    max_t = int(sys.argv[1]) if len(sys.argv) > 1 else None
    result = resolve_all(db, max_tickers=max_t)
    print(result)
