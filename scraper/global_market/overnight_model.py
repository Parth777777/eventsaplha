"""
Global → India overnight setup model.

Pulls daily OHLC for major global indices + FX + VIX, computes 60-day rolling
correlation with Nifty, and predicts next-day Nifty bias using a simple
linear model fitted weekly offline.

For the MVP-plus tier we ship a trained-on-historical-data default and a
refit routine triggered by a weekly cron. The fit is intentionally simple
(linear regression via numpy) so it's interpretable and easy to validate.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

try:
    import yfinance as yf  # type: ignore

    YF_OK = True
except Exception:
    yf = None
    YF_OK = False

try:
    import numpy as np  # type: ignore
    import pandas as pd  # type: ignore

    NP_OK = True
except Exception:
    np = None
    pd = None
    NP_OK = False

from metrics import inc

logger = logging.getLogger(__name__)

GLOBAL_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "ftse": "^FTSE",
    "nikkei": "^N225",
    "shanghai": "000001.SS",
    "hangseng": "^HSI",
    "dxy": "DX-Y.NYB",
    "usdinr": "INR=X",
    "vix": "^VIX",
    "indiavix": "^INDIAVIX",
    "nifty": "^NSEI",
}

# Default regression weights for next-day Nifty % given overnight global closes.
# Estimated from rough 5-year slope; refresh via refit_weights().
DEFAULT_WEIGHTS = {
    "intercept": 0.02,
    "sp500_lag": 0.42,
    "nasdaq_lag": 0.18,
    "nikkei_lag": 0.08,
    "dxy_lag": -0.12,
    "usdinr_lag": -0.18,
    "vix_lag": -0.06,
}


def _history(yt: str, days: int = 90) -> Optional["pd.DataFrame"]:
    if not YF_OK:
        return None
    try:
        hist = yf.Ticker(yt).history(period=f"{days}d", interval="1d")
        if hist is None or hist.empty:
            return None
        return hist
    except Exception as exc:
        logger.debug("global history fetch failed yt=%s err=%s", yt, exc)
        return None


def snapshot_indices() -> Dict[str, Dict]:
    """Return latest close + overnight %change for each tracked ticker."""
    out: Dict[str, Dict] = {}
    for name, yt in GLOBAL_TICKERS.items():
        hist = _history(yt, days=10)
        if hist is None or len(hist) < 2:
            continue
        close_today = float(hist["Close"].iloc[-1])
        close_prev = float(hist["Close"].iloc[-2])
        pct = (close_today - close_prev) / close_prev * 100.0
        out[name] = {
            "yf_ticker": yt,
            "close": round(close_today, 4),
            "pct_change": round(pct, 3),
            "asof": hist.index[-1].isoformat(),
        }
    return out


def rolling_correlations_to_nifty(days: int = 60) -> Dict[str, float]:
    """60-day rolling correlation of each ticker to Nifty daily returns."""
    if not (YF_OK and NP_OK):
        return {}
    try:
        nifty = _history(GLOBAL_TICKERS["nifty"], days=days + 10)
        if nifty is None:
            return {}
        nifty_ret = nifty["Close"].pct_change().dropna().tail(days)
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for name, yt in GLOBAL_TICKERS.items():
        if name == "nifty":
            continue
        hist = _history(yt, days=days + 10)
        if hist is None:
            continue
        try:
            ret = hist["Close"].pct_change().dropna()
            joined = pd.concat([nifty_ret, ret], axis=1, join="inner").dropna()
            if len(joined) >= 20:
                corr = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
                out[name] = round(corr, 3)
        except Exception:
            continue
    return out


def predict_nifty_bias(snapshot: Dict[str, Dict],
                        weights: Optional[Dict[str, float]] = None) -> Dict:
    """Compute open-bias prediction from the snapshot."""
    w = weights or DEFAULT_WEIGHTS
    x = {
        "sp500_lag": snapshot.get("sp500", {}).get("pct_change", 0.0),
        "nasdaq_lag": snapshot.get("nasdaq", {}).get("pct_change", 0.0),
        "nikkei_lag": snapshot.get("nikkei", {}).get("pct_change", 0.0),
        "dxy_lag": snapshot.get("dxy", {}).get("pct_change", 0.0),
        "usdinr_lag": snapshot.get("usdinr", {}).get("pct_change", 0.0),
        "vix_lag": snapshot.get("vix", {}).get("pct_change", 0.0),
    }
    pred = w.get("intercept", 0.0) + sum(w.get(k, 0.0) * v for k, v in x.items())
    # Rough confidence from magnitude of inputs
    input_energy = sum(abs(v) for v in x.values())
    confidence = max(0.2, min(0.9, 0.3 + input_energy / 12.0))
    direction = "bullish" if pred > 0.1 else ("bearish" if pred < -0.1 else "neutral")
    return {
        "predicted_pct": round(pred, 3),
        "direction": direction,
        "confidence": round(confidence, 2),
        "inputs": {k: round(v, 3) for k, v in x.items()},
    }


def compose_overnight(db) -> Dict:
    snap = snapshot_indices()
    corrs = rolling_correlations_to_nifty()
    pred = predict_nifty_bias(snap)
    payload = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "indices": snap,
        "correlations_to_nifty": corrs,
        "prediction": pred,
    }
    _persist(db, payload, pred)
    inc("overnight_snapshots_total")
    return payload


def _persist(db, payload: Dict, pred: Dict) -> None:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        if db.is_postgres:
            db.conn.cursor().execute(
                """INSERT INTO overnight_snapshots (snap_date, payload, predicted_nifty_bias, confidence)
                   VALUES (CURRENT_DATE, %s, %s, %s)
                   ON CONFLICT (snap_date) DO UPDATE SET
                     payload = EXCLUDED.payload,
                     predicted_nifty_bias = EXCLUDED.predicted_nifty_bias,
                     confidence = EXCLUDED.confidence""",
                (json.dumps(payload, default=str), pred.get("predicted_pct"), pred.get("confidence")),
            )
        else:
            db.conn.execute(
                f"""INSERT OR REPLACE INTO overnight_snapshots
                     (snap_date, payload, predicted_nifty_bias, confidence)
                     VALUES (DATE('now'), {p}, {p}, {p})""",
                (json.dumps(payload, default=str), pred.get("predicted_pct"), pred.get("confidence")),
            )
        db.conn.commit()
    except Exception as exc:
        logger.debug("overnight persist failed: %s", exc)
        db.conn.rollback()


def refit_weights(days: int = 730) -> Dict[str, float]:
    """Fit a simple OLS model of Nifty next-day return on global overnight features.

    Runs weekly. Falls back silently if numpy/pandas unavailable.
    """
    if not (YF_OK and NP_OK):
        return DEFAULT_WEIGHTS
    try:
        nifty = _history(GLOBAL_TICKERS["nifty"], days=days)
        if nifty is None or len(nifty) < 100:
            return DEFAULT_WEIGHTS
        features: Dict[str, "pd.Series"] = {}
        for feat in ("sp500", "nasdaq", "nikkei", "dxy", "usdinr", "vix"):
            h = _history(GLOBAL_TICKERS[feat], days=days)
            if h is None:
                continue
            features[feat + "_lag"] = h["Close"].pct_change().shift(1)
        target = nifty["Close"].pct_change() * 100.0
        df = pd.concat([target.rename("y")] + [features[k].rename(k) * 100.0 for k in features],
                       axis=1, join="inner").dropna()
        if len(df) < 100:
            return DEFAULT_WEIGHTS
        X = df[[c for c in df.columns if c != "y"]].values
        y = df["y"].values
        X_ = np.hstack([np.ones((len(X), 1)), X])
        beta, *_ = np.linalg.lstsq(X_, y, rcond=None)
        fitted = {"intercept": float(beta[0])}
        for i, col in enumerate([c for c in df.columns if c != "y"]):
            fitted[col] = float(beta[i + 1])
        logger.info("refit overnight weights: %s", fitted)
        return fitted
    except Exception as exc:
        logger.warning("refit failed: %s", exc)
        return DEFAULT_WEIGHTS
