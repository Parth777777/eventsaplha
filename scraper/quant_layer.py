"""
Tickwave — Quant Layer
Cross-sectional ranking, long-short construction, Bayesian confidence,
abnormal return model, and volatility-adjusted positioning.

All modules are independent classes with clean interfaces.
Each module accepts raw signal dicts and returns enriched dicts.
"""

import math
import logging
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 1. CROSS-SECTIONAL RANKER
# ============================================================

class CrossSectionalRanker:
    """
    Computes Z-score of alpha across the signal universe.

    Z = (alpha_i - mean_alpha) / std_alpha

    Clipped to [-3, 3] then normalized to a 0–100 percentile rank.
    Stocks in the top decile (z > 1.28) are LONG candidates.
    Stocks in the bottom decile (z < -1.28) are SHORT candidates.
    """

    Z_CLIP = 3.0
    LONG_THRESHOLD  = 1.28   # ~top 10%
    SHORT_THRESHOLD = -1.28  # ~bottom 10%

    @staticmethod
    def rank(signals: List[Dict]) -> List[Dict]:
        """
        Add z_score, percentile, and long_short_flag to each signal.

        Args:
            signals: list of signal dicts, each must have 'alpha_score'

        Returns:
            Same list enriched with quant fields, sorted by z_score desc
        """
        if not signals:
            return signals

        alphas = [float(s.get('alpha_score', 0)) for s in signals]
        n = len(alphas)

        if n == 1:
            # Universe of one: z=0, percentile=50
            signals[0].update({'z_score': 0.0, 'percentile': 50.0, 'ls_flag': 'neutral'})
            return signals

        mean_a = sum(alphas) / n
        variance = sum((a - mean_a) ** 2 for a in alphas) / max(n - 1, 1)
        std_a = math.sqrt(variance) if variance > 0 else 1.0

        enriched = []
        for sig, a in zip(signals, alphas):
            raw_z = (a - mean_a) / std_a
            z = max(-CrossSectionalRanker.Z_CLIP, min(CrossSectionalRanker.Z_CLIP, raw_z))

            # Percentile: map z in [-3,3] → [0,100]
            # Use standard normal CDF approximation
            percentile = round(CrossSectionalRanker._normal_cdf(z) * 100, 1)

            if z >= CrossSectionalRanker.LONG_THRESHOLD:
                ls_flag = 'long'
            elif z <= CrossSectionalRanker.SHORT_THRESHOLD:
                ls_flag = 'short'
            else:
                ls_flag = 'neutral'

            enriched.append({
                **sig,
                'z_score': round(z, 3),
                'percentile': percentile,
                'ls_flag': ls_flag,
            })

        enriched.sort(key=lambda x: x['z_score'], reverse=True)
        return enriched

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """
        Abramowitz & Stegun approximation for Φ(z).
        Max error ~7.5e-8, sufficient for display purposes.
        """
        t = 1.0 / (1.0 + 0.2316419 * abs(z))
        poly = (0.319381530 * t
                - 0.356563782 * t**2
                + 1.781477937 * t**3
                - 1.821255978 * t**4
                + 1.330274429 * t**5)
        p = 1.0 - (1 / math.sqrt(2 * math.pi)) * math.exp(-z * z / 2) * poly
        return p if z >= 0 else 1.0 - p


# ============================================================
# 2. LONG-SHORT PORTFOLIO CONSTRUCTOR
# ============================================================

class LongShortPortfolio:
    """
    Constructs a market-neutral long-short portfolio from ranked signals.

    Allocation is proportional to Z-score magnitude.
    Net exposure target: 0 (dollar-neutral).
    Gross leverage cap: 2.0x.
    """

    GROSS_LEVERAGE_CAP = 2.0
    TOP_N = 10    # max positions per leg
    MIN_Z = 0.5   # minimum |z| to include

    @classmethod
    def construct(cls, ranked: List[Dict]) -> Dict:
        """
        Build long and short legs from cross-sectionally ranked signals.

        Args:
            ranked: signals already enriched by CrossSectionalRanker.rank()

        Returns:
            {
              'long_leg':  [...],
              'short_leg': [...],
              'stats': {...}
            }
        """
        long_candidates  = [s for s in ranked if s.get('ls_flag') == 'long'
                            and abs(s.get('z_score', 0)) >= cls.MIN_Z]
        short_candidates = [s for s in ranked if s.get('ls_flag') == 'short'
                            and abs(s.get('z_score', 0)) >= cls.MIN_Z]

        # Sort: long by z_score desc, short by z_score asc (most negative first)
        long_candidates.sort(key=lambda x: x.get('z_score', 0), reverse=True)
        short_candidates.sort(key=lambda x: x.get('z_score', 0))

        long_leg  = long_candidates[:cls.TOP_N]
        short_leg = short_candidates[:cls.TOP_N]

        # Proportional allocation: weight_i = |z_i| / sum(|z|) per leg
        long_leg  = cls._allocate(long_leg,  direction='long')
        short_leg = cls._allocate(short_leg, direction='short')

        # Portfolio stats
        expected_long  = sum(s.get('predicted_return_3d', 0) * s.get('weight', 0)
                             for s in long_leg)
        expected_short = sum(-s.get('predicted_return_3d', 0) * s.get('weight', 0)
                             for s in short_leg)
        net_exposure   = len(long_leg) - len(short_leg)
        gross_exposure = len(long_leg) + len(short_leg)

        stats = {
            'long_count': len(long_leg),
            'short_count': len(short_leg),
            'net_exposure': net_exposure,
            'gross_exposure': gross_exposure,
            'expected_long_return_pct': round(expected_long, 2),
            'expected_short_pnl_pct': round(expected_short, 2),
            'expected_net_return_pct': round(expected_long + expected_short, 2),
            'avg_long_alpha': round(
                sum(s.get('alpha_score', 0) for s in long_leg) / max(len(long_leg), 1), 1),
            'avg_short_alpha': round(
                sum(s.get('alpha_score', 0) for s in short_leg) / max(len(short_leg), 1), 1),
        }

        return {
            'long_leg': long_leg,
            'short_leg': short_leg,
            'stats': stats,
        }

    @classmethod
    def _allocate(cls, leg: List[Dict], direction: str) -> List[Dict]:
        """Assign proportional weight_pct to each position in a leg."""
        if not leg:
            return leg
        total_z = sum(abs(s.get('z_score', 0)) for s in leg)
        if total_z == 0:
            total_z = 1
        result = []
        for i, sig in enumerate(leg):
            w = abs(sig.get('z_score', 0)) / total_z
            result.append({
                **sig,
                'leg': direction,
                'weight': round(w, 4),
                'weight_pct': round(w * 100, 1),
                'rank_in_leg': i + 1,
            })
        return result


# ============================================================
# 3. ABNORMAL RETURN MODEL
# ============================================================

class AbnormalReturnModel:
    """
    AR = actual_return - expected_return

    Expected return estimated as:
      E[R] = risk_free_rate + beta * market_return

    For Indian markets:
      risk_free ≈ 0.025% / day  (6.5% p.a. / 260)
      beta estimated from 30-day correlation with NIFTY
    """

    DAILY_RISK_FREE = 0.025 / 100   # 6.5% p.a. in daily %
    DEFAULT_BETA = 1.0

    @staticmethod
    def compute(actual_return: float,
                market_return: float,
                beta: float = 1.0,
                days: int = 1) -> Dict:
        """
        Compute abnormal return over horizon.

        Args:
            actual_return: actual % return (e.g., 2.5 for +2.5%)
            market_return: NIFTY % return over same period
            beta: stock's beta to NIFTY
            days: horizon in days

        Returns:
            {ar, expected_return, car (cumulative), t_stat_proxy}
        """
        rf = AbnormalReturnModel.DAILY_RISK_FREE * days * 100  # % over period
        expected = rf + beta * market_return
        ar = actual_return - expected

        # Rough t-stat proxy: AR / (expected_std * sqrt(days))
        expected_std = 1.5 * beta  # rough daily std estimate (%)
        t_stat = ar / (expected_std * math.sqrt(max(days, 1)) + 1e-9)

        return {
            'actual_return': round(actual_return, 3),
            'expected_return': round(expected, 3),
            'abnormal_return': round(ar, 3),
            'cumulative_ar': round(ar, 3),   # Extend to array if tracking daily
            't_stat': round(t_stat, 2),
            'significant': abs(t_stat) > 1.96,  # 95% confidence
        }


# ============================================================
# 4. BAYESIAN CONFIDENCE UPDATER
# ============================================================

class BayesianConfidenceUpdater:
    """
    Updates signal confidence using Bayesian inference.

    Prior: base NLP confidence (0–1)
    Likelihood updates:
      - Additional corroborating source  → boost
      - Conflicting source               → decay
      - High-quality source (BSE/NSE)    → boost
      - LLM cross-verification pass      → boost
      - Technical confirmation           → boost

    P(correct | evidence) ∝ P(evidence | correct) × P(correct)
    """

    # Log-odds update magnitudes per evidence type
    LOG_ODDS_UPDATES = {
        'corroborating_source': +0.5,    # Each extra source confirming
        'conflicting_source':   -0.6,    # Contradictory signal
        'official_source':      +0.8,    # BSE/NSE/RBI filing
        'llm_verified':         +0.4,    # Groq LLM cross-check passed
        'technical_confirm':    +0.3,    # Price action confirms sentiment
        'social_amplification': +0.2,    # Trending on financial social
        'stale_event':          -0.3,    # Event is > 3 days old
        'low_volume_stock':     -0.4,    # Thin liquidity reduces reliability
        'analyst_consensus':    +0.6,    # Matches analyst consensus
    }

    @staticmethod
    def update(prior_confidence: float, evidence: List[str]) -> float:
        """
        Apply Bayesian log-odds update.

        Args:
            prior_confidence: NLP confidence (0–1)
            evidence: list of evidence type strings from LOG_ODDS_UPDATES

        Returns:
            Updated posterior confidence (0–1)
        """
        if prior_confidence <= 0:
            prior_confidence = 0.01
        if prior_confidence >= 1:
            prior_confidence = 0.99

        # Prior log-odds
        log_odds = math.log(prior_confidence / (1 - prior_confidence))

        # Apply each piece of evidence
        for ev in evidence:
            update = BayesianConfidenceUpdater.LOG_ODDS_UPDATES.get(ev, 0)
            log_odds += update

        # Convert back to probability
        posterior = 1.0 / (1.0 + math.exp(-log_odds))
        return round(max(0.05, min(0.97, posterior)), 4)

    @staticmethod
    def evidence_from_signal(signal: Dict) -> List[str]:
        """
        Auto-detect applicable evidence types from a signal dict.
        Called during scraper pipeline to auto-update confidence.
        """
        evidence = []
        source_count = signal.get('source_count', 1)
        if source_count >= 2:
            for _ in range(min(source_count - 1, 3)):
                evidence.append('corroborating_source')
        if signal.get('llm_verified'):
            evidence.append('llm_verified')
        if signal.get('is_official_source'):
            evidence.append('official_source')
        days_old = signal.get('days_old', 0)
        if days_old > 3:
            evidence.append('stale_event')
        market_cap = signal.get('market_cap_category', 'large')
        if market_cap == 'small':
            evidence.append('low_volume_stock')
        return evidence


# ============================================================
# 5. VOLATILITY-ADJUSTED POSITION SIZER
# ============================================================

class VolatilityAdjustedSizer:
    """
    Kelly-inspired position sizing: position_size = alpha / volatility

    Constraints:
      - Max single position: 15% of portfolio
      - Min position:  1%
      - Total long:   ≤ 100%
      - Total short:  ≤ 50% (asymmetric due to regulatory limits on short selling in India)

    Volatility is annualised %. Converts to daily before sizing.
    """

    MAX_POSITION = 0.15   # 15% cap
    MIN_POSITION = 0.01   # 1% floor
    MAX_LONG_TOTAL  = 1.0
    MAX_SHORT_TOTAL = 0.5

    @classmethod
    def size_leg(cls, leg: List[Dict], max_total: float) -> List[Dict]:
        """
        Compute volatility-adjusted position sizes for one leg.

        Each signal should have: alpha_score, volatility (0–1 or annualized %)

        Returns:
            Leg with 'position_size' (fraction of portfolio) and 'position_pct' added
        """
        if not leg:
            return leg

        raw_sizes = []
        for sig in leg:
            alpha = sig.get('alpha_score', 50)
            vol = sig.get('volatility', 0.2)
            if vol <= 0:
                vol = 0.20  # default 20% annualized vol

            # Kelly: f = alpha_edge / variance
            # Simplified: size ∝ alpha_score / (vol * 100)
            raw = alpha / (vol * 100 + 1)
            raw_sizes.append(max(0, raw))

        total_raw = sum(raw_sizes) or 1.0

        # Scale so total = max_total, then cap each at MAX_POSITION
        sized = []
        for sig, raw in zip(leg, raw_sizes):
            size = (raw / total_raw) * max_total
            size = max(cls.MIN_POSITION, min(cls.MAX_POSITION, size))
            sized.append({
                **sig,
                'position_size': round(size, 4),
                'position_pct': round(size * 100, 1),
            })

        # Re-normalise after capping
        total_after = sum(s['position_size'] for s in sized)
        if total_after > max_total:
            scale = max_total / total_after
            for s in sized:
                s['position_size'] = round(s['position_size'] * scale, 4)
                s['position_pct'] = round(s['position_size'] * 100, 1)

        return sized


# ============================================================
# 6. FACTOR OVERLAY
# ============================================================

class FactorOverlay:
    """
    Combines multiple factor scores into a single overlay multiplier.

    Factors:
      - event_alpha: from alpha scoring engine (normalised 0–1)
      - momentum:    price momentum signal (-1 to +1)
      - volatility:  vol regime (high vol → lower weight)
      - rel_strength: stock vs sector (-1 to +1)

    Factor weights are regime-aware.
    """

    # Factor weights per regime (must sum to 1)
    WEIGHTS = {
        'bull':    {'event': 0.35, 'momentum': 0.30, 'vol': 0.10, 'rs': 0.25},
        'bear':    {'event': 0.40, 'momentum': 0.20, 'vol': 0.20, 'rs': 0.20},
        'sideways':{'event': 0.45, 'momentum': 0.15, 'vol': 0.15, 'rs': 0.25},
        'crisis':  {'event': 0.50, 'momentum': 0.10, 'vol': 0.25, 'rs': 0.15},
        'spike':   {'event': 0.30, 'momentum': 0.40, 'vol': 0.15, 'rs': 0.15},
    }

    @staticmethod
    def _regime_bucket(regime_str: str) -> str:
        r = (regime_str or '').lower()
        if 'crisis' in r:   return 'crisis'
        if 'spike' in r:    return 'spike'
        if 'bull' in r:     return 'bull'
        if 'bear' in r:     return 'bear'
        return 'sideways'

    @classmethod
    def compute(cls,
                event_alpha: float,
                momentum: float,
                volatility: float,
                rel_strength: float,
                regime: str = '') -> float:
        """
        Returns overlay multiplier (0.70 – 1.30).

        Args:
            event_alpha:  normalised (0–100 → 0–1)
            momentum:     -1 to +1
            volatility:   0–1 (higher = more uncertain, lower weight)
            rel_strength: -1 to +1 (stock vs sector)
            regime:       regime string from alpha scoring engine
        """
        bucket = cls._regime_bucket(regime)
        w = cls.WEIGHTS.get(bucket, cls.WEIGHTS['sideways'])

        # Normalise event_alpha to 0–1
        ea = min(1.0, max(0.0, event_alpha / 100.0))

        # Vol factor: high vol REDUCES confidence in any single signal
        vol_factor = max(0.0, 1.0 - volatility)  # low vol → 1.0, high vol → ~0.5

        # Momentum: −1→0, 0→0.5, +1→1.0
        mom = (momentum + 1.0) / 2.0

        # Rel strength: same normalisation
        rs = (rel_strength + 1.0) / 2.0

        composite = (w['event'] * ea
                     + w['momentum'] * mom
                     + w['vol'] * vol_factor
                     + w['rs'] * rs)

        # Map composite [0,1] → overlay [0.70, 1.30]
        overlay = 0.70 + composite * 0.60
        return round(max(0.70, min(1.30, overlay)), 4)


# ============================================================
# 7. GEO-INTELLIGENCE IMPACT SCORER
# ============================================================

class GeoIntelligenceScorer:
    """
    Maps geo-political events to sector/stock impact.

    Event → Economic Variable → Sector → Alpha Contribution

    Returns a geo_impact multiplier (0.80–1.40) that boosts or
    suppresses alpha for stocks affected by the geo event.
    """

    # Geo event type → {sector: direction}
    GEO_SECTOR_MAP = {
        'war_conflict': {
            'ENERGY': +1, 'DEFENCE': +1, 'METALS': +1,
            'AVIATION': -1, 'TOURISM': -1, 'FMCG': -0.5,
        },
        'oil_surge': {
            'ENERGY': +1, 'CHEMICALS': -0.5, 'AVIATION': -1,
            'AUTO': -0.5, 'BFSI': -0.3,
        },
        'oil_crash': {
            'ENERGY': -1, 'AVIATION': +1, 'CHEMICALS': +0.5,
            'AUTO': +0.3, 'FMCG': +0.3,
        },
        'rate_hike': {
            'BFSI': +0.5, 'REALESTATE': -1, 'NBFC': -1,
            'METALS': -0.3, 'IT': -0.2,
        },
        'rate_cut': {
            'BFSI': -0.3, 'REALESTATE': +1, 'NBFC': +1,
            'AUTO': +0.5, 'FMCG': +0.3,
        },
        'china_risk': {
            'METALS': -0.5, 'CHEMICALS': +0.5, 'IT': -0.3,
            'ELECTRONICS': -0.4, 'PHARMA': +0.3,
        },
        'rupee_weakness': {
            'IT': +1, 'PHARMA': +0.5,
            'OIL': -0.5, 'FMCG': -0.3, 'AUTO': -0.3,
        },
        'inflation_spike': {
            'FMCG': -0.5, 'METALS': +0.5,
            'REALESTATE': -0.3, 'AUTO': -0.3,
        },
        'infra_push': {
            'CEMENT': +1, 'INFRA': +1, 'STEEL': +0.5,
            'ENERGY': +0.3,
        },
    }

    @classmethod
    def compute(cls,
                geo_event_type: str,
                stock_sector: str,
                geo_magnitude: float = 5.0,
                is_domestic: bool = False) -> float:
        """
        Returns geo_impact multiplier for a stock given a geo event.

        Args:
            geo_event_type: one of GEO_SECTOR_MAP keys
            stock_sector:   sector of the stock (ENERGY, IT, etc.)
            geo_magnitude:  1–10 severity
            is_domestic:    domestic events have double impact on Indian stocks

        Returns:
            multiplier in [0.80, 1.40]
        """
        sector_map = cls.GEO_SECTOR_MAP.get(geo_event_type, {})
        direction = sector_map.get(stock_sector, 0)

        if direction == 0:
            return 1.0  # No geo exposure for this sector

        # Scale impact: magnitude/10 × direction × domestic_boost
        domestic_boost = 1.5 if is_domestic else 1.0
        impact = (geo_magnitude / 10.0) * direction * domestic_boost

        # Map to multiplier: +1 impact → 1.40, -1 impact → 0.80
        multiplier = 1.0 + impact * 0.40
        return round(max(0.80, min(1.40, multiplier)), 4)

    @classmethod
    def get_sector_impacts(cls, geo_event_type: str, geo_magnitude: float = 5.0) -> Dict:
        """Return impact scores for all sectors for a given geo event."""
        sector_map = cls.GEO_SECTOR_MAP.get(geo_event_type, {})
        return {
            sector: round(1.0 + (direction * geo_magnitude / 10.0) * 0.40, 4)
            for sector, direction in sector_map.items()
        }


# ============================================================
# PIPELINE CONVENIENCE FUNCTION
# ============================================================

def run_quant_pipeline(signals: List[Dict],
                       market_volatility: float = 0.18) -> Dict:
    """
    Run the full quant pipeline on a list of raw signals.

    Returns enriched signals with: z_score, percentile, ls_flag,
    weight, position_size, bayesian_confidence, factor_overlay.

    Args:
        signals: list of signal dicts (from DB or scraper)
        market_volatility: current market vol (0–1)

    Returns:
        {
          'ranked': [...],           # All signals with z_scores
          'portfolio': {...},        # Long-short portfolio
          'universe_stats': {...}    # Alpha distribution stats
        }
    """
    if not signals:
        return {'ranked': [], 'portfolio': {'long_leg': [], 'short_leg': [], 'stats': {}},
                'universe_stats': {}}

    # Step 1: Add predicted_return_3d for each signal (for portfolio EV)
    for s in signals:
        pred = s.get('predicted_return_3d') or s.get('predicted_return_pct', 0)
        s['predicted_return_3d'] = float(pred)
        # Propagate volatility estimate
        if 'volatility' not in s:
            s['volatility'] = market_volatility

    # Step 2: Cross-sectional ranking
    ranked = CrossSectionalRanker.rank(signals)

    # Step 3: Bayesian confidence update
    for sig in ranked:
        evidence = BayesianConfidenceUpdater.evidence_from_signal(sig)
        prior_conf = float(sig.get('confidence', 0.5))
        sig['bayesian_confidence'] = BayesianConfidenceUpdater.update(prior_conf, evidence)

    # Step 4: Factor overlay
    for sig in ranked:
        sig['factor_overlay'] = FactorOverlay.compute(
            event_alpha=sig.get('alpha_score', 50),
            momentum=sig.get('momentum', 0),
            volatility=sig.get('volatility', market_volatility),
            rel_strength=sig.get('relative_strength', 0),
            regime=sig.get('regime', ''),
        )

    # Step 5: Long-short portfolio
    portfolio = LongShortPortfolio.construct(ranked)

    # Step 6: Volatility-adjusted sizing
    portfolio['long_leg']  = VolatilityAdjustedSizer.size_leg(
        portfolio['long_leg'],  VolatilityAdjustedSizer.MAX_LONG_TOTAL)
    portfolio['short_leg'] = VolatilityAdjustedSizer.size_leg(
        portfolio['short_leg'], VolatilityAdjustedSizer.MAX_SHORT_TOTAL)

    # Universe stats
    alphas = [s.get('alpha_score', 0) for s in ranked]
    n = len(alphas)
    mean_a = sum(alphas) / n if n else 0
    universe_stats = {
        'count': n,
        'mean_alpha': round(mean_a, 1),
        'max_alpha': round(max(alphas), 1) if alphas else 0,
        'min_alpha': round(min(alphas), 1) if alphas else 0,
        'long_count': sum(1 for s in ranked if s.get('ls_flag') == 'long'),
        'short_count': sum(1 for s in ranked if s.get('ls_flag') == 'short'),
        'neutral_count': sum(1 for s in ranked if s.get('ls_flag') == 'neutral'),
    }

    return {
        'ranked': ranked,
        'portfolio': portfolio,
        'universe_stats': universe_stats,
    }


# ============================================================
# 8. VOLUME ANALYZER (OBV + surge + profile + delivery %)
# ============================================================

class VolumeAnalyzer:
    """
    Volume-based confirmation signals.

    Consumes daily OHLCV from yfinance and optional delivery % from
    NSE bhavcopy. Emits per-ticker flags that feed AlphaScoringEngine
    as a multiplier (no standalone signals — only reweights existing ones).
    """

    SURGE_THRESHOLD = 2.5        # vol_today / avg_20d
    UNEXPLAINED_LOOKBACK_DAYS = 3  # days back to look for news before flagging "unexplained"
    OBV_SLOPE_WINDOW = 20
    OBV_HISTORY = 60

    @staticmethod
    def compute_obv(closes: List[float], volumes: List[float]) -> List[float]:
        """Classic On-Balance Volume: +vol on up-day, -vol on down-day, 0 on unchanged."""
        if not closes or len(closes) != len(volumes):
            return []
        obv = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])
        return obv

    @staticmethod
    def _linear_slope(series: List[float]) -> float:
        """OLS slope of a series vs. its index."""
        n = len(series)
        if n < 2:
            return 0.0
        mean_x = (n - 1) / 2.0
        mean_y = sum(series) / n
        num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(series))
        den = sum((i - mean_x) ** 2 for i in range(n)) or 1.0
        return num / den

    @classmethod
    def analyze(cls, ticker: str, closes: List[float], volumes: List[float],
                delivery_pcts: Optional[List[float]] = None,
                had_news_last_3d: bool = False) -> Dict:
        """Run the full volume analysis for one ticker.

        Args:
            closes:   chronological list of daily closes (oldest first)
            volumes:  daily traded volumes, aligned with closes
            delivery_pcts: optional NSE delivery % (aligned)
            had_news_last_3d: whether any news event was recorded for this ticker recently
        """
        if len(closes) < cls.OBV_SLOPE_WINDOW + 1 or len(closes) != len(volumes):
            return {"ticker": ticker, "has_data": False}

        obv = cls.compute_obv(closes, volumes)
        obv_slope = cls._linear_slope(obv[-cls.OBV_SLOPE_WINDOW:])

        # Divergence: compare price trend vs OBV trend on same window
        price_slope = cls._linear_slope(closes[-cls.OBV_SLOPE_WINDOW:])
        divergence = None
        if price_slope > 0 and obv_slope <= 0:
            divergence = "bearish"   # price up, OBV flat/down — distribution
        elif price_slope < 0 and obv_slope >= 0:
            divergence = "bullish"   # price down, OBV flat/up — accumulation

        # Volume surge
        vol_today = volumes[-1]
        avg_20 = sum(volumes[-21:-1]) / 20.0 if len(volumes) > 21 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        surge_ratio = vol_today / avg_20 if avg_20 > 0 else 0.0
        surge = surge_ratio >= cls.SURGE_THRESHOLD
        unexplained = surge and not had_news_last_3d

        # OBV z-score on 20-day OBV deltas
        obv_deltas = [obv[i] - obv[i - 1] for i in range(max(len(obv) - 20, 1), len(obv))]
        if len(obv_deltas) > 1:
            mu = sum(obv_deltas) / len(obv_deltas)
            var = sum((d - mu) ** 2 for d in obv_deltas) / max(len(obv_deltas) - 1, 1)
            sd = math.sqrt(var) if var > 0 else 1.0
            obv_z = (obv_deltas[-1] - mu) / sd
        else:
            obv_z = 0.0

        # Delivery %
        delivery_analysis: Dict = {"available": False}
        if delivery_pcts and len(delivery_pcts) >= 21:
            today_del = delivery_pcts[-1]
            recent = delivery_pcts[-21:-1]
            mu = sum(recent) / len(recent)
            var = sum((d - mu) ** 2 for d in recent) / max(len(recent) - 1, 1)
            sd = math.sqrt(var) if var > 0 else 1.0
            delivery_analysis = {
                "available": True,
                "delivery_pct": round(today_del, 2),
                "delivery_z_20d": round((today_del - mu) / sd, 2),
            }

        return {
            "ticker": ticker,
            "has_data": True,
            "obv_slope": round(obv_slope, 2),
            "obv_z_20d": round(obv_z, 2),
            "obv_divergence_flag": divergence,
            "price_slope_20d": round(price_slope, 4),
            "vol_surge_ratio": round(surge_ratio, 2),
            "volume_surge": surge,
            "unexplained_volume": unexplained,
            "delivery": delivery_analysis,
        }

    @staticmethod
    def volume_confirmation_multiplier(analysis: Dict, sentiment: str) -> float:
        """Map volume analysis + signal sentiment to a scoring multiplier.

        1.15×  — OBV + (delivery) confirm direction
        0.85×  — OBV diverges from direction
        1.00×  — neutral / no data
        """
        if not analysis or not analysis.get("has_data"):
            return 1.0
        div = analysis.get("obv_divergence_flag")
        sent = (sentiment or "").lower()
        # Explicit divergence against the direction → penalty
        if sent == "bullish" and div == "bearish":
            return 0.85
        if sent == "bearish" and div == "bullish":
            return 0.85
        # OBV slope confirms direction + volume surge → boost
        if analysis.get("volume_surge"):
            if sent == "bullish" and analysis.get("obv_slope", 0) > 0:
                return 1.15
            if sent == "bearish" and analysis.get("obv_slope", 0) < 0:
                return 1.15
        return 1.0

    @staticmethod
    def intraday_profile(buckets: List[Tuple[str, float]]) -> Dict:
        """Given 15-min (time, volume) buckets for a single session, flag distribution patterns.

        Returns close_heavy/open_heavy flags. Close-heavy on an up-day is a
        textbook distribution tell; open-heavy accumulation pattern is the mirror.
        """
        if len(buckets) < 10:
            return {"has_data": False}
        total = sum(v for _, v in buckets) or 1.0
        first_q = sum(v for _, v in buckets[: len(buckets) // 4]) / total
        last_q = sum(v for _, v in buckets[-len(buckets) // 4:]) / total
        return {
            "has_data": True,
            "open_weight": round(first_q, 3),
            "close_weight": round(last_q, 3),
            "close_heavy": last_q > 0.35,
            "open_heavy": first_q > 0.35,
        }


if __name__ == '__main__':
    # Smoke test
    import random
    random.seed(42)
    test_signals = [
        {
            'ticker': t,
            'alpha_score': random.uniform(20, 95),
            'sentiment': random.choice(['bullish', 'bearish', 'neutral']),
            'confidence': random.uniform(0.4, 0.9),
            'regime': random.choice(['bull_strong', 'sideways_calm', 'bear_weak']),
            'predicted_return_pct': random.uniform(-5, 8),
            'volatility': random.uniform(0.12, 0.35),
        }
        for t in ['TCS', 'RELIANCE', 'HDFCBANK', 'INFY', 'WIPRO',
                  'ONGC', 'BPCL', 'TATAMOTORS', 'MARUTI', 'BAJAJ-AUTO',
                  'ICICIBANK', 'SBIN', 'AXISBANK', 'TITAN', 'NESTLEIND']
    ]

    result = run_quant_pipeline(test_signals, market_volatility=0.18)

    print(f"\n=== Universe: {result['universe_stats']} ===")
    print(f"\n=== LONG LEG ({len(result['portfolio']['long_leg'])} positions) ===")
    for s in result['portfolio']['long_leg']:
        print(f"  {s['ticker']:12} z={s['z_score']:+.2f}  alpha={s['alpha_score']:.0f}  wt={s['weight_pct']:.1f}%  pos={s['position_pct']:.1f}%")
    print(f"\n=== SHORT LEG ({len(result['portfolio']['short_leg'])} positions) ===")
    for s in result['portfolio']['short_leg']:
        print(f"  {s['ticker']:12} z={s['z_score']:+.2f}  alpha={s['alpha_score']:.0f}  wt={s['weight_pct']:.1f}%  pos={s['position_pct']:.1f}%")
    print(f"\n=== Portfolio Stats ===")
    for k, v in result['portfolio']['stats'].items():
        print(f"  {k}: {v}")
