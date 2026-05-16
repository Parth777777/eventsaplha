"""
Tickwave - IPO Alpha Scoring Engine

Computes 0-100 IPO Alpha Score from 6 weighted factors:
  1. Subscription Score   (30%) - oversubscription multiplier (QIB weighted)
  2. Sector Score         (15%) - sector momentum from existing signal flow
  3. Buzz Score           (15%) - news/social velocity
  4. Valuation Score      (15%) - issue PE vs sector median
  5. GMP Score            (15%) - grey market premium % of issue price
  6. Fundamentals Score   (10%) - revenue growth + PAT margin

Output: float in [0, 100]. Each factor breakdown is captured in `factors_json`
for UI transparency. Returned as a tuple (score, confidence, factors_dict).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Per-factor weights (must sum to 1.0)
WEIGHTS = {
    'subscription': 0.30,
    'sector':       0.15,
    'buzz':         0.15,
    'valuation':    0.15,
    'gmp':          0.15,
    'fundamentals': 0.10,
}


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def subscription_score(sub_total: float, sub_qib: float, sub_hni: float, sub_retail: float) -> Tuple[float, str]:
    """
    Score based on weighted subscription multipliers.
    QIB carries 2x weight (smart money), HNI 1.2x, retail 0.8x.
    Log-scale: 200x = 100, 50x = 80, 10x = 60, 2x = 40, 1x = 20.
    Pre-IPO (no sub data) returns 0 with low confidence.
    """
    # Use total if granular not available
    if not (sub_qib or sub_hni or sub_retail) and sub_total:
        weighted = float(sub_total)
    else:
        # Weighted: QIB 2x, HNI 1.2x, retail 0.8x; normalize by sum of weights
        w_qib, w_hni, w_ret = 2.0, 1.2, 0.8
        denom = w_qib + w_hni + w_ret
        weighted = (
            w_qib * float(sub_qib or 0) +
            w_hni * float(sub_hni or 0) +
            w_ret * float(sub_retail or 0)
        ) / denom
    if weighted <= 0:
        return 0.0, 'no subscription data'
    # Log-scale: 200x → 100, 1x → 0
    score = math.log10(weighted + 1) / math.log10(201) * 100
    return _clip(score), f"{weighted:.1f}x weighted (QIB {sub_qib:.1f}x, HNI {sub_hni:.1f}x, Retail {sub_retail:.1f}x)"


def sector_score(sector_avg_alpha: Optional[float], sector_signal_count: int) -> Tuple[float, str]:
    """
    Score based on sector momentum from active signals.
    sector_avg_alpha (0-100) maps directly; with low signal count we discount.
    """
    if sector_avg_alpha is None or sector_signal_count < 1:
        return 50.0, 'no sector signals (neutral)'
    # Discount if sample is too thin
    confidence_mult = min(1.0, sector_signal_count / 5.0)
    score = float(sector_avg_alpha) * confidence_mult + 50.0 * (1 - confidence_mult)
    return _clip(score), f"sector avg alpha {sector_avg_alpha:.0f} ({sector_signal_count} signals)"


def buzz_score(news_count: int, days_window: int = 7) -> Tuple[float, str]:
    """
    Score from news article count in the recent window.
    Log-scaled: 50 articles = 100, 10 = 70, 3 = 50, 0 = 0.
    """
    n = max(0, int(news_count or 0))
    if n == 0:
        return 0.0, 'no news mentions'
    score = math.log10(n + 1) / math.log10(51) * 100
    return _clip(score), f"{n} articles in last {days_window}d"


def valuation_score(issue_pe: Optional[float], sector_pe: Optional[float]) -> Tuple[float, str]:
    """
    Compares issue P/E to sector median. Below median = bullish (cheap).
    Discount of 30%+ vs sector = 100; premium of 50%+ = 0; at parity = 50.
    Returns neutral 50 if either input missing.
    """
    if not issue_pe or not sector_pe or issue_pe <= 0 or sector_pe <= 0:
        return 50.0, 'PE comparison unavailable (neutral)'
    ratio = float(issue_pe) / float(sector_pe)  # <1 = cheap, >1 = expensive
    # Map ratio 0.7 → 100, 1.0 → 50, 1.5 → 0
    if ratio <= 0.7:
        score = 100.0
    elif ratio >= 1.5:
        score = 0.0
    elif ratio <= 1.0:
        score = 100.0 - (ratio - 0.7) / 0.3 * 50.0
    else:  # 1.0 < ratio < 1.5
        score = 50.0 - (ratio - 1.0) / 0.5 * 50.0
    return _clip(score), f"PE {issue_pe:.1f} vs sector {sector_pe:.1f} ({(ratio-1)*100:+.0f}%)"


def gmp_score(gmp_pct: float) -> Tuple[float, str]:
    """
    GMP as % of issue price. Indicative grey market signal — heavily smoothed.
    50% GMP = 100, 25% = 80, 10% = 50, 0% = 30, negative = 0.
    """
    g = float(gmp_pct or 0)
    if g <= 0:
        return _clip(30.0 + g * 3.0), f"GMP {g:+.1f}%"  # negative GMP penalty
    # Sigmoid-like: g=10 → 50, g=50 → 100
    score = 30.0 + (1 - math.exp(-g / 18.0)) * 70.0
    return _clip(score), f"GMP {g:+.1f}%"


def fundamentals_score(revenue_growth_pct: Optional[float],
                       revenue_cr: Optional[float],
                       pat_cr: Optional[float]) -> Tuple[float, str]:
    """
    Combines revenue growth and PAT margin signal.
    Growth 50%+ = full credit on growth; PAT positive = full credit on margin.
    """
    parts = []
    growth_score = 50.0
    if revenue_growth_pct is not None:
        g = float(revenue_growth_pct)
        # 0% → 30, 25% → 60, 50% → 90, 100%+ → 100
        if g <= 0:
            growth_score = max(0.0, 30.0 + g * 0.5)
        else:
            growth_score = min(100.0, 30.0 + g * 1.4)
        parts.append(f"rev growth {g:+.0f}%")

    margin_score = 50.0
    if revenue_cr and pat_cr is not None and revenue_cr > 0:
        margin = float(pat_cr) / float(revenue_cr) * 100
        # PAT margin: <0 → 0, 5% → 50, 15% → 90, 25%+ → 100
        if margin < 0:
            margin_score = max(0.0, 30.0 + margin * 5)
        elif margin <= 25:
            margin_score = 30.0 + (margin / 25.0) * 70
        else:
            margin_score = 100.0
        parts.append(f"PAT margin {margin:.1f}%")

    score = (growth_score + margin_score) / 2
    label = ', '.join(parts) if parts else 'fundamentals unavailable'
    return _clip(score), label


def compute_ipo_alpha_score(
    ipo: Dict,
    sector_avg_alpha: Optional[float] = None,
    sector_signal_count: int = 0,
    sector_pe: Optional[float] = None,
) -> Tuple[float, float, Dict]:
    """
    Main entry point. Compute IPO alpha score from a dict of IPO fields.

    Returns:
      (alpha_score 0-100, confidence 0-1, factors_dict)

    `factors_dict` has shape:
      {'subscription': {'weight': 0.30, 'score': 75.2, 'evidence': '12.4x weighted...'},
       'sector': {...}, ...,
       'final_score': 68.4}
    """
    sub_s, sub_e = subscription_score(
        ipo.get('sub_total', 0) or 0,
        ipo.get('sub_qib', 0) or 0,
        ipo.get('sub_hni', 0) or 0,
        ipo.get('sub_retail', 0) or 0,
    )
    sec_s, sec_e = sector_score(sector_avg_alpha, sector_signal_count)
    buzz_s, buzz_e = buzz_score(ipo.get('news_count', 0) or 0)
    val_s, val_e = valuation_score(ipo.get('issue_pe'), sector_pe)
    gmp_s, gmp_e = gmp_score(ipo.get('gmp_pct', 0) or 0)
    fund_s, fund_e = fundamentals_score(
        ipo.get('revenue_growth_pct'),
        ipo.get('revenue_cr'),
        ipo.get('pat_cr'),
    )

    factors = {
        'subscription': {'weight': WEIGHTS['subscription'], 'score': round(sub_s, 1), 'evidence': sub_e},
        'sector':       {'weight': WEIGHTS['sector'],       'score': round(sec_s, 1), 'evidence': sec_e},
        'buzz':         {'weight': WEIGHTS['buzz'],         'score': round(buzz_s, 1), 'evidence': buzz_e},
        'valuation':    {'weight': WEIGHTS['valuation'],    'score': round(val_s, 1), 'evidence': val_e},
        'gmp':          {'weight': WEIGHTS['gmp'],          'score': round(gmp_s, 1), 'evidence': gmp_e},
        'fundamentals': {'weight': WEIGHTS['fundamentals'], 'score': round(fund_s, 1), 'evidence': fund_e},
    }

    final = (
        sub_s * WEIGHTS['subscription'] +
        sec_s * WEIGHTS['sector'] +
        buzz_s * WEIGHTS['buzz'] +
        val_s * WEIGHTS['valuation'] +
        gmp_s * WEIGHTS['gmp'] +
        fund_s * WEIGHTS['fundamentals']
    )
    final = _clip(final)
    factors['final_score'] = round(final, 1)

    # Confidence reflects how much real data backed the score.
    # Each non-default factor contributes; missing data lowers confidence.
    populated = 0
    if (ipo.get('sub_total') or 0) > 0 or (ipo.get('sub_qib') or 0) > 0:
        populated += 1
    if sector_signal_count >= 1:
        populated += 1
    if (ipo.get('news_count') or 0) > 0:
        populated += 1
    if ipo.get('issue_pe') and sector_pe:
        populated += 1
    if (ipo.get('gmp_pct') or 0) != 0:
        populated += 1
    if ipo.get('revenue_growth_pct') is not None or ipo.get('revenue_cr'):
        populated += 1
    confidence = round(0.4 + (populated / 6.0) * 0.55, 2)  # range ~0.4 to 0.95

    return final, confidence, factors


def factors_to_json(factors: Dict) -> str:
    """Serialize factors dict for DB storage."""
    try:
        return json.dumps(factors, default=str)
    except Exception:
        return '{}'


def factors_from_json(blob: Optional[str]) -> Dict:
    """Deserialize factors dict from DB."""
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except Exception:
        return {}


if __name__ == '__main__':
    # Smoke test
    sample = {
        'symbol': 'TESTIPO',
        'sub_total': 12.4, 'sub_qib': 25.0, 'sub_hni': 18.0, 'sub_retail': 4.5,
        'news_count': 22, 'gmp_pct': 18.0,
        'revenue_growth_pct': 35.0, 'revenue_cr': 1200.0, 'pat_cr': 140.0,
        'issue_pe': 22.0,
    }
    score, conf, factors = compute_ipo_alpha_score(
        sample, sector_avg_alpha=68.0, sector_signal_count=8, sector_pe=28.0,
    )
    print(f"IPO Alpha: {score:.1f} (confidence {conf:.2f})")
    for k, v in factors.items():
        print(f"  {k}: {v}")
