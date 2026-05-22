"""End-to-end forecast orchestrator.

Composes baseline + features + classifier + entry-window into a single
forecast dict, and handles storage (idempotent upsert into
`earnings_forecasts` table).

Called from:
  - `/api/v1/earnings/forecast/<ticker>` endpoint (on-demand)
  - `forecast_job.py` daily cron (batch refresh for upcoming earnings)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
for p in (_HERE, os.path.join(_HERE, '..', 'scraper')):
    if p not in sys.path:
        sys.path.insert(0, p)

MODEL_VERSION = "v0"


def compute_full_forecast(db, ticker: str) -> Dict[str, Any]:
    """Compute a complete forecast for one ticker. Returns a dict matching
    the `earnings_forecasts` row shape (with extra `top_drivers` list and
    `entry_window` dict). Does NOT write to DB — caller decides.
    """
    from earnings_forecaster import forecast as compute_baseline
    from forecast_features import compute as compute_features
    from forecast_classifier import classify
    from post_event_dynamics import estimate as estimate_entry_window

    tk = ticker.upper().strip().replace(".NS", "").replace(".BO", "")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    baseline = compute_baseline(db, tk)
    features = compute_features(db, tk)
    pred = classify(features, baseline, db=db)
    entry = estimate_entry_window(db, tk, predicted_direction=pred.predicted_hit)

    target = pred.target_earnings_date or baseline.target_earnings_date or features.target_earnings_date

    return {
        "ticker": tk,
        "forecast_date": today,
        "target_earnings_date": target,
        "model_version": MODEL_VERSION,
        # Numbers
        "revenue_estimate": baseline.revenue_estimate,
        "revenue_low": baseline.revenue_low,
        "revenue_high": baseline.revenue_high,
        "eps_estimate": baseline.eps_estimate,
        "eps_low": baseline.eps_low,
        "eps_high": baseline.eps_high,
        "eps_internal_estimate": baseline.eps_internal_estimate,
        "net_profit_estimate": baseline.net_profit_estimate,
        "net_profit_low": baseline.net_profit_low,
        "net_profit_high": baseline.net_profit_high,
        "ebitda_estimate": baseline.ebitda_estimate,
        "ebitda_low": baseline.ebitda_low,
        "ebitda_high": baseline.ebitda_high,
        "operating_income_estimate": baseline.operating_income_estimate,
        "operating_margin_pct": baseline.operating_margin_pct,
        "ebitda_margin_pct": baseline.ebitda_margin_pct,
        "revenue_growth_yoy_pct": baseline.revenue_growth_yoy_pct,
        "revenue_growth_qoq_pct": baseline.revenue_growth_qoq_pct,
        # Hit / miss
        "p_beat": round(pred.p_beat, 3),
        "p_meet": round(pred.p_meet, 3),
        "p_miss": round(pred.p_miss, 3),
        "predicted_hit": pred.predicted_hit,
        "hit_confidence": round(pred.hit_confidence, 3),
        # Reaction
        "expected_ret_1d_pct": round(pred.expected_ret_1d_pct, 2) if pred.expected_ret_1d_pct is not None else None,
        "ret_1d_ci_low": round(pred.ret_1d_ci_low, 2) if pred.ret_1d_ci_low is not None else None,
        "ret_1d_ci_high": round(pred.ret_1d_ci_high, 2) if pred.ret_1d_ci_high is not None else None,
        # Entry window
        "entry_window": entry.to_dict(),
        # Explainability
        "top_drivers": pred.top_drivers,
        # Provenance
        "features": features.to_dict(),
        "baseline_notes": baseline.notes,
        "data_insufficient": baseline.data_insufficient or (pred.hit_confidence < 0.35),
        "narrative_summary": None,  # filled by forecast_llm.py async
        "llm_generated": False,
    }


def upsert(db, fc: Dict[str, Any]) -> None:
    """Idempotent UPSERT into earnings_forecasts table."""
    if not fc.get("target_earnings_date"):
        logger.debug(f"skip upsert for {fc.get('ticker')} — no target_earnings_date")
        return
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    ew = fc.get("entry_window") or {}
    sql = """
    INSERT INTO earnings_forecasts (
        ticker, forecast_date, target_earnings_date, model_version,
        revenue_estimate, revenue_low, revenue_high,
        eps_estimate, eps_low, eps_high,
        net_profit_estimate, net_profit_low, net_profit_high,
        ebitda_estimate, ebitda_low, ebitda_high,
        operating_income_estimate, operating_margin_pct, ebitda_margin_pct,
        revenue_growth_yoy_pct, revenue_growth_qoq_pct,
        p_beat, p_meet, p_miss, predicted_hit, hit_confidence,
        expected_ret_1d_pct, ret_1d_ci_low, ret_1d_ci_high,
        dump_risk_score, recommended_action, wait_minutes_estimate,
        optimal_entry_start_min, optimal_entry_end_min,
        expected_gap_pct, fade_probability,
        expected_5d_max_drawdown_pct, expected_5d_max_gain_pct,
        analogs_used, fallback_to_sector,
        top_drivers, narrative_summary, llm_generated,
        features_json, data_insufficient
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ticker, target_earnings_date, model_version) DO UPDATE SET
        forecast_date = excluded.forecast_date,
        revenue_estimate = excluded.revenue_estimate,
        revenue_low = excluded.revenue_low,
        revenue_high = excluded.revenue_high,
        eps_estimate = excluded.eps_estimate,
        eps_low = excluded.eps_low,
        eps_high = excluded.eps_high,
        net_profit_estimate = excluded.net_profit_estimate,
        net_profit_low = excluded.net_profit_low,
        net_profit_high = excluded.net_profit_high,
        ebitda_estimate = excluded.ebitda_estimate,
        ebitda_low = excluded.ebitda_low,
        ebitda_high = excluded.ebitda_high,
        operating_income_estimate = excluded.operating_income_estimate,
        operating_margin_pct = excluded.operating_margin_pct,
        ebitda_margin_pct = excluded.ebitda_margin_pct,
        revenue_growth_yoy_pct = excluded.revenue_growth_yoy_pct,
        revenue_growth_qoq_pct = excluded.revenue_growth_qoq_pct,
        p_beat = excluded.p_beat,
        p_meet = excluded.p_meet,
        p_miss = excluded.p_miss,
        predicted_hit = excluded.predicted_hit,
        hit_confidence = excluded.hit_confidence,
        expected_ret_1d_pct = excluded.expected_ret_1d_pct,
        ret_1d_ci_low = excluded.ret_1d_ci_low,
        ret_1d_ci_high = excluded.ret_1d_ci_high,
        dump_risk_score = excluded.dump_risk_score,
        recommended_action = excluded.recommended_action,
        wait_minutes_estimate = excluded.wait_minutes_estimate,
        optimal_entry_start_min = excluded.optimal_entry_start_min,
        optimal_entry_end_min = excluded.optimal_entry_end_min,
        expected_gap_pct = excluded.expected_gap_pct,
        fade_probability = excluded.fade_probability,
        expected_5d_max_drawdown_pct = excluded.expected_5d_max_drawdown_pct,
        expected_5d_max_gain_pct = excluded.expected_5d_max_gain_pct,
        analogs_used = excluded.analogs_used,
        fallback_to_sector = excluded.fallback_to_sector,
        top_drivers = excluded.top_drivers,
        narrative_summary = excluded.narrative_summary,
        llm_generated = excluded.llm_generated,
        features_json = excluded.features_json,
        data_insufficient = excluded.data_insufficient
    """
    if is_pg:
        sql = sql.replace("?", "%s").replace(
            "ON CONFLICT(ticker, target_earnings_date, model_version) DO UPDATE SET",
            "ON CONFLICT (ticker, target_earnings_date, model_version) DO UPDATE SET"
        )
    params = (
        fc["ticker"], fc["forecast_date"], fc["target_earnings_date"], fc["model_version"],
        fc.get("revenue_estimate"), fc.get("revenue_low"), fc.get("revenue_high"),
        fc.get("eps_estimate"), fc.get("eps_low"), fc.get("eps_high"),
        fc.get("net_profit_estimate"), fc.get("net_profit_low"), fc.get("net_profit_high"),
        fc.get("ebitda_estimate"), fc.get("ebitda_low"), fc.get("ebitda_high"),
        fc.get("operating_income_estimate"),
        fc.get("operating_margin_pct"), fc.get("ebitda_margin_pct"),
        fc.get("revenue_growth_yoy_pct"), fc.get("revenue_growth_qoq_pct"),
        fc.get("p_beat"), fc.get("p_meet"), fc.get("p_miss"),
        fc.get("predicted_hit"), fc.get("hit_confidence"),
        fc.get("expected_ret_1d_pct"), fc.get("ret_1d_ci_low"), fc.get("ret_1d_ci_high"),
        ew.get("dump_risk_score"), ew.get("recommended_action"),
        ew.get("wait_minutes_estimate"), ew.get("optimal_entry_start_min"),
        ew.get("optimal_entry_end_min"),
        ew.get("expected_gap_pct"), ew.get("fade_probability"),
        ew.get("expected_5d_max_drawdown_pct"), ew.get("expected_5d_max_gain_pct"),
        ew.get("analogs_used"), 1 if ew.get("fallback_to_sector") else 0,
        json.dumps(fc.get("top_drivers", [])),
        fc.get("narrative_summary"),
        1 if fc.get("llm_generated") else 0,
        json.dumps(fc.get("features", {}), default=str),
        1 if fc.get("data_insufficient") else 0,
    )
    try:
        cur.execute(sql, params)
        db.conn.commit()
    except Exception as e:
        logger.warning(f"forecast upsert failed for {fc.get('ticker')}: {e}")


def get_latest(db, ticker: str) -> Optional[Dict[str, Any]]:
    """Read the most-recent forecast for ticker's next earnings."""
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    sql = ("SELECT * FROM earnings_forecasts "
           "WHERE UPPER(ticker) = ? AND model_version = ? "
           "AND target_earnings_date >= date('now', '-30 days') "
           "ORDER BY forecast_date DESC LIMIT 1")
    if is_pg:
        sql = sql.replace("?", "%s").replace(
            "date('now', '-30 days')", "(CURRENT_DATE - INTERVAL '30 days')"
        )
    cur.execute(sql, (ticker.upper(), MODEL_VERSION))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    rec = dict(zip(cols, row))
    # Re-inflate JSON columns
    try:
        rec["top_drivers"] = json.loads(rec.get("top_drivers") or "[]")
    except Exception:
        rec["top_drivers"] = []
    try:
        rec["features"] = json.loads(rec.get("features_json") or "{}")
    except Exception:
        rec["features"] = {}
    rec.pop("features_json", None)
    # Reconstruct nested entry_window block
    rec["entry_window"] = {
        "dump_risk_score": rec.pop("dump_risk_score", None),
        "recommended_action": rec.pop("recommended_action", None),
        "wait_minutes_estimate": rec.pop("wait_minutes_estimate", None),
        "optimal_entry_start_min": rec.pop("optimal_entry_start_min", None),
        "optimal_entry_end_min": rec.pop("optimal_entry_end_min", None),
        "expected_gap_pct": rec.pop("expected_gap_pct", None),
        "fade_probability": rec.pop("fade_probability", None),
        "expected_5d_max_drawdown_pct": rec.pop("expected_5d_max_drawdown_pct", None),
        "expected_5d_max_gain_pct": rec.pop("expected_5d_max_gain_pct", None),
        "analogs_used": rec.pop("analogs_used", None),
        "fallback_to_sector": bool(rec.pop("fallback_to_sector", 0)),
    }
    return rec


def get_history(db, ticker: str, limit: int = 20) -> list:
    """All past forecasts for this ticker (chronological)."""
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    sql = ("SELECT ticker, forecast_date, target_earnings_date, predicted_hit, "
           "hit_confidence, p_beat, eps_estimate, expected_ret_1d_pct, "
           "recommended_action FROM earnings_forecasts "
           "WHERE UPPER(ticker) = ? AND model_version = ? "
           "ORDER BY forecast_date DESC LIMIT ?")
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql, (ticker.upper(), MODEL_VERSION, limit))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --- CLI smoke test ------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="RELIANCE")
    p.add_argument("--no-write", action="store_true", help="Skip DB upsert")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()
    fc = compute_full_forecast(db, args.ticker)
    if not args.no_write:
        upsert(db, fc)
        logger.info(f"upserted forecast for {args.ticker}")
    # Print summary (excluding the bulky features dict)
    out = {k: v for k, v in fc.items() if k != "features"}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
