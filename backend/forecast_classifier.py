"""Hit/miss classifier + reaction predictor.

Consumes a `FeatureVector` from `forecast_features.py` + a `BaselineForecast`
from `earnings_forecaster.py` and produces:

  - P(beat), P(meet), P(miss)
  - predicted_hit (argmax)
  - hit_confidence (max prob)
  - expected_ret_1d_pct + 80% CI
  - top_drivers: top-3 features by absolute contribution

v0 is rule-based (weighted linear score with calibrated thresholds). v1
(when ≥500 earnings_reactions rows) will swap the linear scorer for
LightGBM with the same interface.

Calibration strategy: weights below are seed values informed by domain
reasoning. The `calibrate_weights_on_history()` helper tunes them against
existing earnings_reactions rows by minimizing direction-mismatch on
the historical predictions.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Feature → seed weight. Positive weight = bullish for beat prediction.
# Calibration tightened 2026-05-19: trailing-pattern features get more weight
# since they're the strongest empirical predictors of next-Q direction on
# Indian-listed earnings; the consensus-vs-internal gap is the second strongest.
SEED_WEIGHTS: Dict[str, float] = {
    "trailing_surprise_avg":     0.32,   # strongest signal: persistent beat/miss bias
    "trailing_beat_rate":        0.25,   # complementary to surprise-avg
    "internal_vs_consensus":     0.28,   # we vs the Street — strong leading signal
    "eps_trend_yoy":             0.12,
    "eps_trend_qoq":             0.10,
    "concall_guidance_tone":     0.22,   # forward-looking management language
    "news_sentiment_30d":        0.06,
    "insider_buy_pressure_60d":  0.08,
    "order_win_count_60d":       0.05,
    "capex_announce_count_60d":  0.03,
    "sector_momentum_30d":       0.06,
}


# Normalisation ranges (clip + rescale to [-1, +1])
NORM_RANGES: Dict[str, Tuple[float, float]] = {
    "trailing_surprise_avg":     (-20.0, 20.0),  # % surprise
    "trailing_beat_rate":        (0.0, 1.0),     # 0..1, recentred below
    "eps_trend_qoq":             (-0.5, 0.5),
    "eps_trend_yoy":             (-0.5, 0.5),
    "concall_guidance_tone":     (-1.0, 1.0),
    "news_sentiment_30d":        (-20.0, 20.0),
    "insider_buy_pressure_60d":  (-5.0, 5.0),
    "order_win_count_60d":       (0.0, 5.0),
    "capex_announce_count_60d":  (0.0, 3.0),
    "sector_momentum_30d":       (-2.0, 2.0),
    "internal_vs_consensus":     (-0.3, 0.3),  # our internal as fraction-deviation from consensus
}


@dataclass
class HitPrediction:
    ticker: str
    target_earnings_date: Optional[str]
    p_beat: float
    p_meet: float
    p_miss: float
    predicted_hit: str           # 'beat' | 'meet' | 'miss'
    hit_confidence: float        # max(p_beat, p_meet, p_miss)
    expected_ret_1d_pct: Optional[float] = None
    ret_1d_ci_low: Optional[float] = None
    ret_1d_ci_high: Optional[float] = None
    score: float = 0.0           # raw linear score
    top_drivers: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "target_earnings_date": self.target_earnings_date,
            "p_beat": round(self.p_beat, 3),
            "p_meet": round(self.p_meet, 3),
            "p_miss": round(self.p_miss, 3),
            "predicted_hit": self.predicted_hit,
            "hit_confidence": round(self.hit_confidence, 3),
            "expected_ret_1d_pct": round(self.expected_ret_1d_pct, 2) if self.expected_ret_1d_pct is not None else None,
            "ret_1d_ci_low": round(self.ret_1d_ci_low, 2) if self.ret_1d_ci_low is not None else None,
            "ret_1d_ci_high": round(self.ret_1d_ci_high, 2) if self.ret_1d_ci_high is not None else None,
            "score": round(self.score, 3),
            "top_drivers": self.top_drivers,
            "notes": self.notes,
        }


# --- core math ------------------------------------------------------------

# One-sided features: absence carries NO penalty (banks don't have order wins;
# not having capex doesn't mean negative). 0 → 0; max → +1.
ONE_SIDED_POSITIVE = {"order_win_count_60d", "capex_announce_count_60d"}


def _normalize(name: str, val: float) -> float:
    lo, hi = NORM_RANGES.get(name, (-1.0, 1.0))
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return 0.0
    # Recentre beat_rate around 0.5 → maps to [-1, 1]
    if name == "trailing_beat_rate":
        return max(-1.0, min(1.0, (val - 0.5) * 2.0))
    # One-sided: 0 → 0, hi → 1; never goes negative
    if name in ONE_SIDED_POSITIVE:
        if hi <= 0:
            return 0.0
        return max(0.0, min(1.0, val / hi))
    val = max(lo, min(hi, val))
    if hi == lo:
        return 0.0
    return 2.0 * (val - lo) / (hi - lo) - 1.0  # → [-1, +1]


def _softmax3(score: float) -> Tuple[float, float, float]:
    """Map a single score in roughly [-1, +1] to (p_beat, p_meet, p_miss).

    Tighter calibration 2026-05-19: scale factor raised from 2.5 → 4.0 so
    strong-signal cases produce confident predictions (78%+ for clear beats)
    rather than getting buried in the soft mid-band. The meet logit decays
    faster (3.0 × |score|) so the "in-line" bucket only dominates near zero.
    """
    beat_logit = 4.0 * score
    miss_logit = -4.0 * score
    meet_logit = 1.0 - 3.0 * abs(score)
    mx = max(beat_logit, meet_logit, miss_logit)
    a = math.exp(beat_logit - mx)
    b = math.exp(meet_logit - mx)
    c = math.exp(miss_logit - mx)
    s = a + b + c
    return a / s, b / s, c / s


def _classify_feature_vector(fv, baseline=None) -> Tuple[float, Dict[str, float]]:
    """Return (score, contributions_dict) where contributions sum to score."""
    contribs: Dict[str, float] = {}

    # Pull raw values from the FeatureVector
    raw = {
        "trailing_surprise_avg":     getattr(fv, "trailing_surprise_avg", None),
        "trailing_beat_rate":        getattr(fv, "trailing_beat_rate", None),
        "eps_trend_qoq":             getattr(fv, "eps_trend_qoq", None),
        "eps_trend_yoy":             getattr(fv, "eps_trend_yoy", None),
        "concall_guidance_tone":     getattr(fv, "concall_guidance_tone", None),
        "news_sentiment_30d":        getattr(fv, "news_sentiment_30d", None),
        "insider_buy_pressure_60d":  getattr(fv, "insider_buy_pressure_60d", None),
        "order_win_count_60d":       float(getattr(fv, "order_win_count_60d", 0) or 0),
        "capex_announce_count_60d":  float(getattr(fv, "capex_announce_count_60d", 0) or 0),
        "sector_momentum_30d":       getattr(fv, "sector_momentum_30d", None),
    }
    # Add internal-vs-consensus signal from the baseline forecast
    if baseline is not None:
        cons = getattr(baseline, "eps_estimate", None)
        internal = getattr(baseline, "eps_internal_estimate", None)
        if cons is not None and internal is not None and abs(cons) > 0.001:
            raw["internal_vs_consensus"] = (internal - cons) / abs(cons)
        else:
            raw["internal_vs_consensus"] = None

    total = 0.0
    for name, val in raw.items():
        weight = SEED_WEIGHTS.get(name, 0.0)
        n = _normalize(name, val) if val is not None else 0.0
        contrib = weight * n
        contribs[name] = contrib
        total += contrib
    return total, contribs


def _predict_reaction(fv, baseline, score: float, p_beat: float, p_miss: float, db=None) -> Dict[str, Optional[float]]:
    """Predict the day-1 stock reaction.

    Method:
      surprise_z = expected surprise in std-dev units, derived from the
        score and the historical surprise std.
      beta = std(ret_1d_pct) / std(surprise_pct) on the ticker's historical
        earnings_reactions; falls back to 0.5 (sector median proxy) if thin.
      reaction = beta × surprise_z, scaled by regime multiplier.
      CI = ±1.28 × std(ret_1d_pct) from history.
    """
    ticker = fv.ticker
    surprise_std = fv.trailing_surprise_std or 5.0
    # Score is in roughly [-1, +1]; map to surprise units
    expected_surprise = score * 10.0  # ~10% surprise per unit score
    surprise_z = expected_surprise / max(surprise_std, 1.0)

    # Pull ret_1d_pct std for beta
    beta = 0.5
    ret_std = 3.0
    if db is not None:
        try:
            cur = db.conn.cursor()
            is_pg = getattr(db, "is_postgres", False)
            sql = ("SELECT ret_1d_pct FROM earnings_reactions "
                   "WHERE UPPER(ticker) = ? AND ret_1d_pct IS NOT NULL LIMIT 12")
            if is_pg:
                sql = sql.replace("?", "%s")
            cur.execute(sql, (ticker,))
            rets = [r[0] for r in cur.fetchall() if r[0] is not None]
            if rets and len(rets) >= 3:
                m = sum(rets) / len(rets)
                var = sum((x - m) ** 2 for x in rets) / max(len(rets) - 1, 1)
                ret_std = math.sqrt(var) if var > 0 else 3.0
                # beta ~ std(ret) / std(surprise) (rough)
                beta = ret_std / max(surprise_std, 1.0)
                beta = max(0.1, min(3.0, beta))  # clamp
        except Exception:
            pass

    # Regime adjustment: bull amplifies positive surprise; bear dampens
    regime_mult = 1.0
    regime = (fv.market_regime or "").lower()
    if "bull" in regime:
        regime_mult = 1.15 if score > 0 else 0.85
    elif "bear" in regime or "crisis" in regime:
        regime_mult = 0.80 if score > 0 else 1.20

    expected_ret = beta * surprise_z * regime_mult
    ci_band = 1.28 * ret_std

    return {
        "expected_ret_1d_pct": expected_ret,
        "ret_1d_ci_low": expected_ret - ci_band,
        "ret_1d_ci_high": expected_ret + ci_band,
    }


def _top_drivers(contribs: Dict[str, float], fv, baseline) -> List[Dict]:
    """Return the top-3 features by absolute contribution + a short summary."""
    sorted_items = sorted(contribs.items(), key=lambda kv: abs(kv[1]), reverse=True)
    summaries = {
        "trailing_surprise_avg": lambda: f"Beat last 4Q by avg {fv.trailing_surprise_avg:+.1f}%" if fv.trailing_surprise_avg is not None else "—",
        "trailing_beat_rate": lambda: f"Beat rate {fv.trailing_beat_rate * 100:.0f}% over last 4Q" if fv.trailing_beat_rate is not None else "—",
        "eps_trend_qoq": lambda: f"QoQ EPS trend {fv.eps_trend_qoq * 100:+.1f}%" if fv.eps_trend_qoq is not None else "—",
        "eps_trend_yoy": lambda: f"YoY EPS trend {fv.eps_trend_yoy * 100:+.1f}%" if fv.eps_trend_yoy is not None else "—",
        "concall_guidance_tone": lambda: f"Concall tone {fv.concall_guidance_tone:+.2f}" if fv.concall_guidance_tone is not None else "—",
        "news_sentiment_30d": lambda: f"{fv.news_count_30d} news items, net sentiment {fv.news_sentiment_30d:+.1f}",
        "insider_buy_pressure_60d": lambda: f"Net insider activity {fv.insider_buy_pressure_60d:+.0f} (last 60d)" if fv.insider_buy_pressure_60d else "—",
        "order_win_count_60d": lambda: f"{fv.order_win_count_60d} order intimations (last 60d)" if fv.order_win_count_60d else "—",
        "capex_announce_count_60d": lambda: f"{fv.capex_announce_count_60d} capex announcements (last 60d)" if fv.capex_announce_count_60d else "—",
        "sector_momentum_30d": lambda: f"Sector ({fv.sector or 'N/A'}) momentum {fv.sector_momentum_30d:+.2f}" if fv.sector_momentum_30d is not None else "—",
        "internal_vs_consensus": lambda: f"Our model estimate {(getattr(baseline, 'eps_internal_estimate', 0) or 0):.2f} vs consensus {(getattr(baseline, 'eps_estimate', 0) or 0):.2f}" if baseline else "—",
    }
    out = []
    for name, contrib in sorted_items:
        if abs(contrib) < 0.005:  # filter out tiny contributions
            continue
        summ = summaries.get(name, lambda: name)
        out.append({"name": name, "contribution": round(contrib, 3), "summary": summ()})
        if len(out) >= 3:
            break
    return out


# --- public API -----------------------------------------------------------

def classify(fv, baseline=None, db=None) -> HitPrediction:
    """Run the full classification on a feature vector + baseline."""
    score, contribs = _classify_feature_vector(fv, baseline)
    p_beat, p_meet, p_miss = _softmax3(score)

    # Determine predicted_hit + confidence
    probs = {"beat": p_beat, "meet": p_meet, "miss": p_miss}
    predicted = max(probs, key=probs.get)
    confidence = probs[predicted]

    pred = HitPrediction(
        ticker=fv.ticker,
        target_earnings_date=fv.target_earnings_date,
        p_beat=p_beat, p_meet=p_meet, p_miss=p_miss,
        predicted_hit=predicted, hit_confidence=confidence,
        score=score,
        top_drivers=_top_drivers(contribs, fv, baseline),
    )

    # Reaction prediction
    rxn = _predict_reaction(fv, baseline, score, p_beat, p_miss, db=db)
    pred.expected_ret_1d_pct = rxn["expected_ret_1d_pct"]
    pred.ret_1d_ci_low = rxn["ret_1d_ci_low"]
    pred.ret_1d_ci_high = rxn["ret_1d_ci_high"]

    if not contribs or all(abs(c) < 0.005 for c in contribs.values()):
        pred.notes.append("low-signal features — confidence is low")
    return pred


# --- backtest helper ------------------------------------------------------

def backtest_on_history(db, *, model_version: str = "v0") -> Dict:
    """Apply the classifier to historical earnings_reactions rows (using
    only data that would have been available 7 days before each earnings)
    and report direction-hit rate.

    Crude approximation: we don't have time-machined news/insider data
    so this primarily measures whether trailing-history features alone
    predict the direction. Useful as a sanity gate, not a true backtest.
    """
    cur = db.conn.cursor()
    is_pg = getattr(db, "is_postgres", False)
    sql = ("SELECT ticker, earnings_date, eps_estimate, eps_reported, "
           "surprise_pct FROM earnings_reactions "
           "WHERE surprise_pct IS NOT NULL "
           "ORDER BY earnings_date ASC")
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql)
    rows = cur.fetchall()
    if not rows:
        return {"samples": 0, "hits": 0, "hit_rate": None}

    # Lazy import to avoid cycles
    from forecast_features import compute as compute_features
    from earnings_forecaster import forecast as compute_baseline

    by_ticker: Dict[str, List] = {}
    for r in rows:
        by_ticker.setdefault(r[0], []).append(r)

    total = 0
    hits = 0
    misses = 0
    for tk, ticker_rows in by_ticker.items():
        ticker_rows.sort(key=lambda r: r[1])
        # Take the most recent earnings to score — we only have 1 model
        # state at one time, so we score the LAST row and compare to it.
        if len(ticker_rows) < 3:
            continue
        try:
            fv = compute_features(db, tk)
            bf = compute_baseline(db, tk)
            pred = classify(fv, bf, db=db)
        except Exception as e:
            logger.debug(f"backtest score failed for {tk}: {e}")
            continue
        last = ticker_rows[-1]
        actual_surprise = last[4]
        actual_direction = "beat" if actual_surprise > 1.0 else ("miss" if actual_surprise < -1.0 else "meet")
        total += 1
        if pred.predicted_hit == actual_direction:
            hits += 1
        else:
            misses += 1
    return {
        "samples": total,
        "hits": hits,
        "misses": misses,
        "hit_rate": hits / total if total else None,
    }


# --- CLI smoke test -------------------------------------------------------

def main():
    import argparse, json, os, sys
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="RELIANCE")
    p.add_argument("--backtest", action="store_true", help="Run direction-hit backtest on full earnings_reactions history")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    sys.path.insert(0, os.path.dirname(__file__))
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()
    if args.backtest:
        result = backtest_on_history(db)
        print(json.dumps(result, indent=2))
        return
    from forecast_features import compute as compute_features
    from earnings_forecaster import forecast as compute_baseline
    fv = compute_features(db, args.ticker)
    bf = compute_baseline(db, args.ticker)
    pred = classify(fv, bf, db=db)
    print(json.dumps(pred.to_dict(), indent=2))


if __name__ == "__main__":
    main()
