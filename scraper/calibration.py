"""
Model calibration — learns from resolved predictions and retunes coefficients.

Runs on a cron (weekly default). Writes fitted parameters to
`model_calibration` table and reads them back on scraper startup so future
signals use the updated weights.

Three calibrations are performed:

1. **Magnitude coefficient** — fits a linear scalar `k` such that
   predicted_return × k best matches actual_return. Currently the event
   magnitude → return translation is hard-coded; if the model systematically
   under-predicts magnitude by 4× (our observed case), k ≈ 4.

2. **Alpha-bucket calibration curve** — fits hit-rate and expected-value per
   alpha bucket so the UI can show honest expected edge rather than raw alpha.

3. **Event-type performance priors** — computes per-event-type hit rates and
   uses them as priors for Bayesian updates when new signals arrive.

The fitted values are persisted and applied to the AlphaScoringEngine via a
lazy-loaded `get_calibration()` helper.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    horizon TEXT NOT NULL,
    params TEXT,
    sample_size INTEGER,
    fitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_calib_kind_horizon ON model_calibration(kind, horizon);
"""

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS model_calibration (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(50) NOT NULL,
    horizon VARCHAR(5) NOT NULL,
    params TEXT,
    sample_size INTEGER,
    fitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_calib_kind_horizon ON model_calibration(kind, horizon);
"""


def ensure_schema(db) -> None:
    try:
        cursor = db.conn.cursor()
        stmts = (_SCHEMA_PG if getattr(db, "is_postgres", False) else _SCHEMA).split(";")
        for s in stmts:
            s = s.strip()
            if s:
                cursor.execute(s)
        db.conn.commit()
    except Exception as exc:
        logger.warning("calibration ensure_schema failed: %s", exc)
        db.conn.rollback()


def _resolved_preds(db, horizon: Optional[str] = None, min_samples: int = 1) -> List[Dict]:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    cursor = db.conn.cursor()
    q = """SELECT s.ticker, s.alpha_score, s.sentiment, s.event_type,
                   s.confidence, s.entry_price,
                   p.horizon, p.predicted_return_pct,
                   p.actual_return_pct, p.hit_target
              FROM signals s
              JOIN predictions p ON s.event_id = p.signal_id
             WHERE p.actual_return_pct IS NOT NULL"""
    args: tuple = ()
    if horizon:
        q += f" AND p.horizon = {p}"
        args = (horizon,)
    cursor.execute(q, args)
    rows = cursor.fetchall()
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        else:
            out.append(dict(zip([c[0] for c in cursor.description], r)))
    return out


def fit_magnitude_coefficient(rows: List[Dict]) -> Dict[str, float]:
    """Least-squares fit: actual = k × predicted (no intercept).

    k > 1 means the model under-predicts magnitude (expand target prices).
    k < 1 means the model over-predicts (shrink target prices).
    """
    pairs = [
        (float(r["predicted_return_pct"]), float(r["actual_return_pct"]))
        for r in rows
        if r.get("predicted_return_pct") is not None and r.get("actual_return_pct") is not None
    ]
    if len(pairs) < 10:
        return {"k": 1.0, "samples": len(pairs), "confidence": "insufficient"}
    num = sum(p * a for p, a in pairs)
    den = sum(p * p for p, a in pairs) or 1e-9
    k = num / den
    # Confidence bucket based on sample size
    conf = "low" if len(pairs) < 50 else "medium" if len(pairs) < 200 else "high"
    # Pearson correlation as a quality check
    mean_p = sum(p for p, _ in pairs) / len(pairs)
    mean_a = sum(a for _, a in pairs) / len(pairs)
    cov = sum((p - mean_p) * (a - mean_a) for p, a in pairs) / len(pairs)
    var_p = sum((p - mean_p) ** 2 for p, _ in pairs) / len(pairs)
    var_a = sum((a - mean_a) ** 2 for _, a in pairs) / len(pairs)
    corr = cov / ((var_p * var_a) ** 0.5 + 1e-12)
    return {
        "k": round(k, 4),
        "pearson_r": round(corr, 4),
        "samples": len(pairs),
        "confidence": conf,
    }


def fit_alpha_buckets(rows: List[Dict]) -> List[Dict]:
    """Empirical hit rate and expected value by alpha bucket."""
    buckets = [
        (0, 50, "<50"),
        (50, 65, "50-65"),
        (65, 80, "65-80"),
        (80, 1000, "80+"),
    ]
    out: List[Dict] = []
    for lo, hi, label in buckets:
        subset = [r for r in rows if lo <= (r.get("alpha_score") or 0) < hi]
        if not subset:
            continue
        n = len(subset)
        hits = sum(1 for r in subset if r.get("hit_target"))
        returns = [float(r.get("actual_return_pct") or 0) for r in subset]
        preds = [float(r.get("predicted_return_pct") or 0) for r in subset]
        # Signal-adjusted PnL
        pnl = [a * (1 if p >= 0 else -1) for a, p in zip(returns, preds)]
        hit_rate = hits / n
        out.append({
            "bucket": label,
            "samples": n,
            "hit_rate": round(hit_rate, 4),
            "avg_signal_pnl": round(sum(pnl) / n, 4),
            "avg_return": round(sum(returns) / n, 4),
        })
    return out


def fit_event_type_priors(rows: List[Dict]) -> Dict[str, Dict[str, float]]:
    """Per-event-type hit rate and signal PnL."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        et = r.get("event_type") or "unknown"
        g = grouped.setdefault(et, {"n": 0, "hits": 0, "pnl": 0.0, "pred_sum": 0.0, "act_sum": 0.0})
        g["n"] += 1
        if r.get("hit_target"):
            g["hits"] += 1
        pred = float(r.get("predicted_return_pct") or 0)
        act = float(r.get("actual_return_pct") or 0)
        g["pnl"] += act * (1 if pred >= 0 else -1)
        g["pred_sum"] += pred
        g["act_sum"] += act
    out: Dict[str, Dict[str, float]] = {}
    for et, g in grouped.items():
        n = g["n"]
        out[et] = {
            "samples": n,
            "hit_rate": round(g["hits"] / n, 4),
            "avg_signal_pnl": round(g["pnl"] / n, 4),
            "bias": round((g["act_sum"] - g["pred_sum"]) / n, 4),  # how much actual > predicted
        }
    return out


def _save(db, kind: str, horizon: str, params: Dict, samples: int) -> None:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        # Delete existing row for same kind+horizon, then insert
        db.conn.cursor().execute(
            f"DELETE FROM model_calibration WHERE kind = {p} AND horizon = {p}",
            (kind, horizon),
        )
        db.conn.cursor().execute(
            f"""INSERT INTO model_calibration (kind, horizon, params, sample_size)
                VALUES ({p}, {p}, {p}, {p})""",
            (kind, horizon, json.dumps(params), samples),
        )
        db.conn.commit()
    except Exception as exc:
        logger.warning("save calibration failed: %s", exc)
        db.conn.rollback()


def load(db, kind: str, horizon: str) -> Optional[Dict]:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        cursor = db.conn.cursor()
        cursor.execute(
            f"""SELECT params, sample_size, fitted_at FROM model_calibration
                 WHERE kind = {p} AND horizon = {p}
                 ORDER BY fitted_at DESC LIMIT 1""",
            (kind, horizon),
        )
        row = cursor.fetchone()
        if not row:
            return None
        params_raw = row[0] if not isinstance(row, dict) else row.get("params")
        samples = row[1] if not isinstance(row, dict) else row.get("sample_size")
        fitted_at = row[2] if not isinstance(row, dict) else row.get("fitted_at")
        return {
            "params": json.loads(params_raw) if params_raw else {},
            "samples": samples,
            "fitted_at": str(fitted_at) if fitted_at else None,
        }
    except Exception as exc:
        logger.debug("load calibration failed: %s", exc)
        return None


def run_calibration(db) -> Dict[str, Any]:
    """Re-fit every calibration from the current resolved set.

    Writes results to model_calibration and returns a summary.
    """
    ensure_schema(db)
    summary: Dict[str, Any] = {"horizons": {}}
    for horizon in ("1D", "3D", "20D"):
        rows = _resolved_preds(db, horizon=horizon)
        if len(rows) < 10:
            summary["horizons"][horizon] = {"skipped": f"only {len(rows)} samples"}
            continue
        mag = fit_magnitude_coefficient(rows)
        alpha = fit_alpha_buckets(rows)
        events = fit_event_type_priors(rows)

        _save(db, "magnitude", horizon, mag, mag.get("samples", 0))
        _save(db, "alpha_buckets", horizon, {"buckets": alpha}, len(rows))
        _save(db, "event_priors", horizon, events, len(rows))

        summary["horizons"][horizon] = {
            "samples": len(rows),
            "magnitude_k": mag["k"],
            "pearson_r": mag["pearson_r"],
            "buckets": len(alpha),
            "event_types": len(events),
        }
    summary["fitted_at"] = datetime.utcnow().isoformat()
    logger.info("calibration complete: %s", summary)
    return summary


# ---------- Live apply ----------

_CACHE: Dict[str, Any] = {"loaded_at": None, "data": {}}


def get_calibration(db, horizon: str = "3D") -> Dict[str, Any]:
    """Cached reader. Returns the fitted params for a given horizon.

    Cache lifetime: 10 minutes, to keep scraper cycles consistent.
    """
    now = datetime.utcnow()
    if _CACHE["loaded_at"] and (now - _CACHE["loaded_at"]) < timedelta(minutes=10):
        if horizon in _CACHE["data"]:
            return _CACHE["data"][horizon]
    data = {}
    for kind in ("magnitude", "alpha_buckets", "event_priors"):
        row = load(db, kind, horizon)
        if row:
            data[kind] = row
    _CACHE["data"][horizon] = data
    _CACHE["loaded_at"] = now
    return data


def magnitude_multiplier(db, horizon: str = "3D") -> float:
    """Returns the multiplier to apply to predicted_return_pct when generating
    signals. Default 1.0 if no calibration yet.

    Capped to [0.5, 3.0] to prevent wild overcorrection.
    """
    cal = get_calibration(db, horizon)
    mag = cal.get("magnitude", {}).get("params", {})
    k = float(mag.get("k", 1.0) or 1.0)
    return max(0.5, min(3.0, k))


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from database_schema import EventAlphaDB

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = EventAlphaDB()
    print(run_calibration(db))
