"""
Abnormal-return (alpha) enrichment for resolved predictions.

Adds two fields to each resolved prediction:
  - nifty_return_pct   : NIFTY 50 % change over the SAME window as the signal
  - abnormal_return_pct: signal's actual_return − nifty_return

Why: a bearish call in a bearish week "hits" by direction but may deliver zero
alpha (the stock just moved with the market). The abnormal-return view
separates model skill from market beta.

Implementation: caches a NIFTY daily series in memory and computes the return
between each prediction's created_at and its target_date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"1D": 1, "3D": 3, "5D": 5, "20D": 20}

_NIFTY_CACHE: Dict[str, pd.DataFrame] = {}


def _fetch_nifty(start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    key = f"{start.date()}_{end.date()}"
    if key in _NIFTY_CACHE:
        return _NIFTY_CACHE[key]
    try:
        df = yf.download(
            "^NSEI",
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or df.empty:
            return None
        _NIFTY_CACHE[key] = df
        return df
    except Exception as exc:
        logger.debug("nifty fetch failed: %s", exc)
        return None


def _close_on_or_before(df: pd.DataFrame, target: datetime) -> Optional[float]:
    ts = pd.Timestamp(target).normalize()
    if df.index.tz is not None:
        ts = ts.tz_localize(df.index.tz)
    eligible = df.index[df.index <= ts]
    if len(eligible) == 0:
        return None
    close_col = "Close"
    if isinstance(df.columns, pd.MultiIndex):
        try:
            return float(df.loc[eligible[-1], ("Close", "^NSEI")])
        except KeyError:
            return None
    try:
        return float(df.loc[eligible[-1], close_col])
    except Exception:
        return None


def abnormal_return(pred: Dict) -> Optional[Dict[str, float]]:
    """Compute Nifty-adjusted abnormal return for a single resolved prediction.

    Returns dict with nifty_return_pct and abnormal_return_pct, or None.
    """
    actual = pred.get("actual_return_pct")
    created = pred.get("created_at")
    horizon = pred.get("horizon")
    if actual is None or not created or not horizon:
        return None
    try:
        if isinstance(created, str):
            created_dt = datetime.fromisoformat(str(created).replace("Z", "").split(".")[0])
        else:
            created_dt = created
    except Exception:
        return None
    days = HORIZON_DAYS.get(horizon, 1)
    target = created_dt + timedelta(days=days)
    if target > datetime.now():
        return None
    df = _fetch_nifty(created_dt - timedelta(days=3), target + timedelta(days=3))
    if df is None:
        return None
    c0 = _close_on_or_before(df, created_dt)
    c1 = _close_on_or_before(df, target)
    if c0 is None or c1 is None or c0 <= 0:
        return None
    nifty_ret = (c1 - c0) / c0 * 100.0
    abn = float(actual) - nifty_ret
    return {
        "nifty_return_pct": round(nifty_ret, 4),
        "abnormal_return_pct": round(abn, 4),
    }


def enrich_resolved(rows: List[Dict]) -> Dict[str, Any]:
    """Attach abnormal_return to every row and return aggregate alpha stats.

    Input `rows` is mutated in place.
    """
    n_enriched = 0
    abn_returns: List[float] = []
    for r in rows:
        out = abnormal_return(r)
        if out is None:
            continue
        r.update(out)
        n_enriched += 1
        abn_returns.append(out["abnormal_return_pct"])
    if not abn_returns:
        return {"enriched": 0, "avg_abnormal_return": None, "alpha_hit_rate": None}
    # "alpha hit" = signal outperformed (or underperformed for bearish) the market
    alpha_hits = 0
    for r in rows:
        if "abnormal_return_pct" not in r:
            continue
        pred = float(r.get("predicted_return_pct") or 0)
        abn = float(r.get("abnormal_return_pct") or 0)
        if (pred >= 0 and abn > 0) or (pred < 0 and abn < 0):
            alpha_hits += 1
    return {
        "enriched": n_enriched,
        "avg_abnormal_return": round(sum(abn_returns) / len(abn_returns), 4),
        "alpha_hit_rate": round(alpha_hits / len(abn_returns), 4),
        "alpha_hits": alpha_hits,
    }
