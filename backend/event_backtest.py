"""Event-window backtester for news announcements.

For a given ticker + event date (or a set of historically-similar event
dates), computes three event-window metrics aggregated across the sample:

    Opening Gap %       = (Open_T - Close_{T-1}) / Close_{T-1} * 100
    Intraday %          = (Close_T - Open_T) / Open_T * 100
    5-Day Drift %       = (Close_{T+N} - Close_T) / Close_T * 100

Defensive design:
    - Holiday forward-skip: if the event date is not a trading session, walk
      forward (up to +5 calendar days) to the next session; T-1 then becomes
      the last session strictly before the resolved T.
    - 24h in-memory TTL cache on yfinance downloads — repeat backtests on
      the same ticker do not hit the network.
    - All exceptions are absorbed; a "stale" response is returned so the
      broadcast payload always has a stable shape.

Public API:
    backtest_event(ticker, event_date, similar_dates=None, drift_window=5)
        -> dict matching the PRD `backtest_summary` payload.

This module performs network I/O (yfinance) and is intentionally synchronous.
Callers from the SSE broadcast path should invoke it via a worker thread.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from cachetools import TTLCache

logger = logging.getLogger(__name__)


# 24h TTL — repeat clicks / multiple events on the same ticker reuse the OHLC
# frame instead of hitting yfinance. Keyed by (ticker, start, end).
_OHLC_CACHE: TTLCache = TTLCache(maxsize=512, ttl=24 * 3600)
_CACHE_LOCK = threading.RLock()

DateLike = Union[str, dt.date, dt.datetime]

_DEFAULT_DRIFT_WINDOW = 5
_HOLIDAY_FORWARD_LIMIT_DAYS = 5   # bail if the next trading session is > 5 days out


# ---- DATE PARSING -----------------------------------------------------------

def _as_date(x: DateLike) -> dt.date:
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    if isinstance(x, str):
        s = x.strip().replace("Z", "")
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(s[: len(fmt) + 4], fmt).date()
            except ValueError:
                continue
        # Last resort: pandas parser (handles many edge formats)
        try:
            import pandas as pd
            return pd.to_datetime(x).date()
        except Exception as e:
            raise ValueError(f"Cannot parse date: {x!r}") from e
    raise TypeError(f"Unrecognized date input: {type(x).__name__}")


# ---- YFINANCE FETCH (CACHED) ------------------------------------------------

def _fetch_ohlc(ticker: str, start: dt.date, end: dt.date):
    """Download daily OHLCV. Returns a DataFrame indexed by date, or None.

    Cache key includes the exact start/end so a wider request doesn't
    accidentally short-circuit a narrower one (and vice versa).
    """
    key = (ticker.upper(), start.isoformat(), end.isoformat())
    with _CACHE_LOCK:
        if key in _OHLC_CACHE:
            return _OHLC_CACHE[key]
    df = None
    try:
        import yfinance as yf
        df = yf.download(
            ticker,
            start=start.isoformat(),
            end=(end + dt.timedelta(days=1)).isoformat(),  # yfinance end is exclusive
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if df is not None and df.empty:
            df = None
    except Exception as e:
        logger.warning("event_backtest: yfinance fetch failed for %s: %s", ticker, e)
        df = None
    with _CACHE_LOCK:
        _OHLC_CACHE[key] = df
    return df


def _col(df, name: str):
    """Return a 1-D Series for an OHLC column regardless of whether yfinance
    handed us a flat- or multi-indexed frame."""
    if df is None:
        return None
    import pandas as pd
    if isinstance(df.columns, pd.MultiIndex):
        try:
            sub = df.xs(name, axis=1, level=0)
            # If still multi-column (multiple tickers), pick the first
            if hasattr(sub, "columns"):
                return sub.iloc[:, 0]
            return sub
        except KeyError:
            return None
    return df[name] if name in df.columns else None


def _scalar(v: Any) -> Optional[float]:
    """Coerce a single OHLC value to float, returning None for NaN/missing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---- CORE COMPUTATION -------------------------------------------------------

def _resolve_t_position(idx, target_date: dt.date) -> Optional[int]:
    """Find the index position of the trading session at or after target_date.

    Returns None if (a) the event is later than the last session, or
    (b) the next available session is more than _HOLIDAY_FORWARD_LIMIT_DAYS
    after the event (signals a long closure / bad data, bail rather than
    misalign T-1)."""
    import pandas as pd
    if len(idx) == 0:
        return None
    target_ts = pd.Timestamp(target_date)
    pos = idx.searchsorted(target_ts, side="left")
    if pos >= len(idx):
        return None
    resolved_ts = idx[pos]
    delta_days = (resolved_ts.normalize() - target_ts).days
    if delta_days > _HOLIDAY_FORWARD_LIMIT_DAYS:
        return None
    return int(pos)


def _compute_one_event(
    df,
    event_date: dt.date,
    drift_window: int,
) -> Optional[Tuple[float, float, float, dt.date]]:
    """Returns (gap_pct, intraday_pct, drift_pct, resolved_t_date) or None."""
    if df is None:
        return None
    open_s = _col(df, "Open")
    close_s = _col(df, "Close")
    if open_s is None or close_s is None:
        return None
    idx = df.index

    t_pos = _resolve_t_position(idx, event_date)
    if t_pos is None:
        return None
    if t_pos == 0:
        # No T-1 baseline available (event is first row in window)
        return None
    if t_pos + drift_window >= len(idx):
        # Not enough forward data for the drift window
        return None

    open_t = _scalar(open_s.iloc[t_pos])
    close_t = _scalar(close_s.iloc[t_pos])
    close_prev = _scalar(close_s.iloc[t_pos - 1])
    close_fwd = _scalar(close_s.iloc[t_pos + drift_window])

    if not all(v not in (None, 0, 0.0) for v in (open_t, close_t, close_prev, close_fwd)):
        return None
    # Safety: explicit None check (the `v not in (0, 0.0)` test would let None slip)
    if open_t is None or close_t is None or close_prev is None or close_fwd is None:
        return None
    if close_prev == 0 or open_t == 0 or close_t == 0:
        return None

    gap = (open_t - close_prev) / close_prev * 100.0
    intraday = (close_t - open_t) / open_t * 100.0
    drift = (close_fwd - close_t) / close_t * 100.0

    resolved = idx[t_pos]
    resolved_date = resolved.date() if hasattr(resolved, "date") else dt.date.fromisoformat(str(resolved)[:10])
    return gap, intraday, drift, resolved_date


# ---- RESPONSE SHAPE ---------------------------------------------------------

def _empty_response(ticker: str, reason: str) -> Dict[str, Any]:
    return {
        "ticker": ticker.upper(),
        "sample_events_found": 0,
        "avg_gap_up_pct": 0.0,
        "fade_probability_pct": 0.0,
        "avg_5day_drift_pct": 0.0,
        "events": [],
        "stale": True,
        "reason": reason,
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def backtest_event(
    ticker: str,
    event_date: DateLike,
    *,
    similar_dates: Optional[Iterable[DateLike]] = None,
    drift_window: int = _DEFAULT_DRIFT_WINDOW,
    _df=None,  # injected by tests to bypass yfinance
) -> Dict[str, Any]:
    """Compute event-window analytics for `event_date` plus optional similar
    historical dates, aggregating gap / fade-probability / 5-day-drift.

    `_df` is a private hook for tests; production callers should leave it None.
    """
    if not ticker or not isinstance(ticker, str):
        return _empty_response(str(ticker or ""), reason="invalid_ticker")

    try:
        primary = _as_date(event_date)
    except (ValueError, TypeError) as e:
        return _empty_response(ticker, reason=f"bad_date:{e}")

    dates: List[dt.date] = [primary]
    if similar_dates:
        for d in similar_dates:
            try:
                dates.append(_as_date(d))
            except (ValueError, TypeError):
                continue
    dates = sorted(set(dates))

    if _df is not None:
        df = _df
    else:
        start = dates[0] - dt.timedelta(days=10)
        end = dates[-1] + dt.timedelta(days=drift_window + 10)
        df = _fetch_ohlc(ticker, start, end)

    if df is None:
        return _empty_response(ticker, reason="no_data")

    events: List[Dict[str, Any]] = []
    for d in dates:
        result = _compute_one_event(df, d, drift_window)
        if result is None:
            continue
        gap, intra, drift, resolved = result
        events.append({
            "event_date": d.isoformat(),
            "resolved_trading_date": resolved.isoformat(),
            "gap_pct": round(gap, 2),
            "intraday_pct": round(intra, 2),
            "drift_5d_pct": round(drift, 2),
        })

    if not events:
        return _empty_response(ticker, reason="no_processable_events")

    n = len(events)
    avg_gap = sum(e["gap_pct"] for e in events) / n
    fade_count = sum(1 for e in events if e["intraday_pct"] < 0)
    fade_prob = fade_count / n * 100.0
    avg_drift = sum(e["drift_5d_pct"] for e in events) / n

    return {
        "ticker": ticker.upper(),
        "sample_events_found": n,
        "avg_gap_up_pct": round(avg_gap, 2),
        "fade_probability_pct": round(fade_prob, 2),
        "avg_5day_drift_pct": round(avg_drift, 2),
        "events": events,
        "stale": False,
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---- DIAGNOSTICS ------------------------------------------------------------

def cache_stats() -> Dict[str, Any]:
    with _CACHE_LOCK:
        return {
            "size": len(_OHLC_CACHE),
            "maxsize": _OHLC_CACHE.maxsize,
            "ttl_seconds": _OHLC_CACHE.ttl,
        }


def cache_clear() -> None:
    with _CACHE_LOCK:
        _OHLC_CACHE.clear()
