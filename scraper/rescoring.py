"""
Post-hoc re-scoring using learned calibration.

Maps raw alpha_score → calibrated expected hit rate and expected PnL by
combining three data-driven overrides:

1. Alpha-bucket hit rates (empirical): if alpha 65-80 has hit rate 0.33 in
   resolved data, we shouldn't treat "alpha 75" as high conviction.
2. Event-type priors: if event_type "earnings" has hit rate 0.36 and "merger"
   has 0.67, two signals with identical alpha=70 are not equivalent.
3. Magnitude coefficient k: shrinks predicted_return_pct to realistic sizes.

Produces `calibrated_alpha` (0-100), `expected_hit_rate` (0-1), and
`calibrated_return_pct` (signed %). Works even without calibration — falls
through to identity.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from calibration import load, magnitude_multiplier

logger = logging.getLogger(__name__)

DEFAULT_BUCKET_RATE = 0.5
DEFAULT_EVENT_RATE = 0.5


def _bucket_for(alpha: float) -> str:
    if alpha >= 80:
        return "80+"
    if alpha >= 65:
        return "65-80"
    if alpha >= 50:
        return "50-65"
    return "<50"


def get_alpha_bucket_rates(db, horizon: str = "3D") -> Dict[str, float]:
    """Returns {bucket: hit_rate} from calibration (or {} if not fitted)."""
    row = load(db, "alpha_buckets", horizon)
    if not row:
        return {}
    out: Dict[str, float] = {}
    for b in (row.get("params") or {}).get("buckets", []):
        out[b.get("bucket")] = float(b.get("hit_rate") or DEFAULT_BUCKET_RATE)
    return out


def get_event_priors(db, horizon: str = "3D") -> Dict[str, Dict[str, float]]:
    row = load(db, "event_priors", horizon)
    if not row:
        return {}
    return row.get("params") or {}


def calibrated_expected_hit(db, alpha_score: float, event_type: Optional[str],
                             horizon: str = "3D") -> Dict[str, Any]:
    """Fuse bucket + event prior into expected hit rate + evidence."""
    bucket_rates = get_alpha_bucket_rates(db, horizon)
    event_priors = get_event_priors(db, horizon)

    bucket = _bucket_for(alpha_score or 0)
    bucket_rate = bucket_rates.get(bucket, DEFAULT_BUCKET_RATE)
    ev_row = event_priors.get(event_type or "", {})
    ev_rate = float(ev_row.get("hit_rate", DEFAULT_EVENT_RATE))
    ev_samples = int(ev_row.get("samples", 0))

    # Weighted average: event prior weighted by sample size confidence
    # (Dirichlet smoothing: (x*n + 0.5 * 20) / (n + 20))
    smoothed_ev = (ev_rate * ev_samples + 0.5 * 20) / (ev_samples + 20)
    # 50/50 blend of alpha-bucket and smoothed event rate
    expected = 0.5 * bucket_rate + 0.5 * smoothed_ev
    expected = max(0.05, min(0.95, expected))

    return {
        "expected_hit_rate": round(expected, 4),
        "bucket": bucket,
        "bucket_rate": round(bucket_rate, 4),
        "event_type_rate": round(ev_rate, 4),
        "event_samples": ev_samples,
        "smoothed_event_rate": round(smoothed_ev, 4),
    }


def calibrated_alpha(raw_alpha: float, expected_hit_rate: float) -> float:
    """Re-express raw alpha as a data-calibrated alpha 0-100.

    Anchor: hit_rate 0.50 → calibrated 50. Each 0.01 above 0.50 adds 2 points.
    Clipped to [0, 100]. Raw alpha influences only the tie-breaking across
    signals with the same hit rate, via a small weighting.
    """
    anchor = 50 + (expected_hit_rate - 0.5) * 200  # 0.5→50, 0.65→80, 0.35→20
    # Shrink raw alpha's influence: 80% hit-rate-anchor, 20% raw
    blended = 0.8 * anchor + 0.2 * (raw_alpha or 0)
    return max(0.0, min(100.0, round(blended, 2)))


def rescore_signal(db, signal: Dict, horizon: str = "3D") -> Dict:
    """Enrich a signal dict in place with calibrated fields. Returns the signal."""
    raw_alpha = float(signal.get("alpha_score") or 0)
    event_type = signal.get("event_type")
    raw_pred = float(signal.get("predicted_return_pct") or 0)

    try:
        hit_info = calibrated_expected_hit(db, raw_alpha, event_type, horizon)
    except Exception as exc:
        logger.debug("expected_hit calibration failed: %s", exc)
        hit_info = {
            "expected_hit_rate": DEFAULT_BUCKET_RATE,
            "bucket": _bucket_for(raw_alpha),
            "bucket_rate": DEFAULT_BUCKET_RATE,
            "event_type_rate": DEFAULT_EVENT_RATE,
            "event_samples": 0,
            "smoothed_event_rate": DEFAULT_EVENT_RATE,
        }

    c_alpha = calibrated_alpha(raw_alpha, hit_info["expected_hit_rate"])

    try:
        k = magnitude_multiplier(db, horizon)
    except Exception:
        k = 1.0
    calibrated_return = round(raw_pred * k, 4)

    # Quality gate: mark signals from chronically-weak event types
    event_gate = "keep"
    if hit_info["event_samples"] >= 20 and hit_info["event_type_rate"] < 0.40:
        event_gate = "penalise"
    elif hit_info["event_samples"] >= 20 and hit_info["event_type_rate"] >= 0.60:
        event_gate = "boost"

    signal["calibrated_alpha"] = c_alpha
    signal["expected_hit_rate"] = hit_info["expected_hit_rate"]
    signal["calibrated_return_pct"] = calibrated_return
    signal["magnitude_multiplier_applied"] = round(k, 4)
    signal["event_type_gate"] = event_gate
    signal["rescoring_evidence"] = hit_info
    return signal


def should_surface(signal: Dict, min_calibrated_alpha: float = 40,
                   min_expected_hit: float = 0.45) -> bool:
    """Policy: should this signal appear in the high-conviction feed?

    Strips signals that are below the calibrated threshold OR whose event type
    is chronically weak.
    """
    if signal.get("event_type_gate") == "penalise":
        return False
    if (signal.get("calibrated_alpha") or 0) < min_calibrated_alpha:
        return False
    if (signal.get("expected_hit_rate") or 0) < min_expected_hit:
        return False
    return True
