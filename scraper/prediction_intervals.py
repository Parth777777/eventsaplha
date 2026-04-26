"""Confidence intervals for predicted returns.

Wraps the existing PredictionEngine.predict_return output by deriving a
68%/95% range from realized historical residuals (predicted vs. actual)
stored in the predictions table. Falls back to volatility * sqrt(horizon)
when not enough history is available.

Usage:
    from prediction_intervals import compute_interval
    ci = compute_interval(db, horizon='3D', predicted_pct=2.5, volatility=0.02)
    # -> {'lower68': 0.6, 'upper68': 4.4, 'lower95': -1.3, 'upper95': 6.3, 'method': 'historical'}
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, Optional


_HORIZON_DAYS = {"1D": 1, "3D": 3, "5D": 5, "20D": 20}


def _historical_residual_std(db, horizon: str, min_samples: int = 30) -> Optional[float]:
    """Std-dev of (actual - predicted) on resolved predictions for this horizon."""
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            SELECT predicted_return_pct, actual_return_pct
            FROM predictions
            WHERE horizon = ?
              AND actual_return_pct IS NOT NULL
              AND created_at >= datetime('now', '-180 days')
            """,
            (horizon,),
        )
        rows = cur.fetchall()
    except Exception:
        return None
    residuals = []
    for r in rows:
        try:
            residuals.append(float(r["actual_return_pct"]) - float(r["predicted_return_pct"]))
        except Exception:
            continue
    if len(residuals) < min_samples:
        return None
    try:
        return statistics.pstdev(residuals)
    except statistics.StatisticsError:
        return None


def compute_interval(
    db,
    horizon: str,
    predicted_pct: float,
    volatility: float = 0.02,
) -> Dict[str, float]:
    """Compute 68%/95% prediction interval around predicted return.

    Method preference:
      1. Historical residual std (most accurate once enough resolutions exist)
      2. Vol-scaled fallback: predicted ± vol_pct * sqrt(horizon_days)

    Both return % units (so predicted=2.5 means 2.5%).
    """
    horizon_days = _HORIZON_DAYS.get(horizon, 1)
    sigma = _historical_residual_std(db, horizon) if db is not None else None

    if sigma is None:
        # Fallback: vol_pct (e.g., 2 for 2%) * sqrt(days)
        vol_pct = abs(volatility * 100) if volatility < 1 else abs(volatility)
        sigma = vol_pct * math.sqrt(horizon_days)
        method = "vol_fallback"
    else:
        method = "historical"

    return {
        "predicted_pct": round(predicted_pct, 2),
        "lower68": round(predicted_pct - sigma, 2),
        "upper68": round(predicted_pct + sigma, 2),
        "lower95": round(predicted_pct - 1.96 * sigma, 2),
        "upper95": round(predicted_pct + 1.96 * sigma, 2),
        "sigma_pct": round(sigma, 2),
        "method": method,
    }
