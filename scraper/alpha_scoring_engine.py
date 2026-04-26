"""
EventAlpha - Alpha Scoring Engine
Regime-aware quantitative scoring with dynamic event weighting and sentiment adjustments
"""

import math
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Tuple
import numpy as np

logger = logging.getLogger(__name__)

# Calibrated magnitude multipliers per horizon. Filled by calibration job via
# set_magnitude_multipliers(); defaults to 1.0 (uncalibrated).
_MAGNITUDE_MULT: Dict[str, float] = {"1D": 1.0, "3D": 1.0, "5D": 1.0, "20D": 1.0}


def set_magnitude_multipliers(mults: Dict[str, float]) -> None:
    """Install calibrated magnitude multipliers. Values outside [0.5, 3.0] are clipped."""
    for h, v in (mults or {}).items():
        try:
            _MAGNITUDE_MULT[h] = max(0.5, min(3.0, float(v)))
        except (TypeError, ValueError):
            continue
    logger.info("magnitude multipliers updated: %s", _MAGNITUDE_MULT)


def get_magnitude_multipliers() -> Dict[str, float]:
    return dict(_MAGNITUDE_MULT)


class MarketRegime(Enum):
    """9 Market Regime Types"""
    BULL_STRONG = "bull_strong"           # Vol<15%, trend up, strong momentum
    BULL_WEAK = "bull_weak"               # Vol<20%, trend up, weak momentum
    BEAR_STRONG = "bear_strong"           # Vol>20%, trend down, strong down-momentum
    BEAR_WEAK = "bear_weak"               # Vol<20%, trend down, weak momentum
    SIDEWAYS_CALM = "sideways_calm"       # Vol<10%, no clear trend
    SIDEWAYS_CHOPPY = "sideways_choppy"   # Vol 15-25%, no clear trend, noise
    SPIKE_UP = "spike_up"                 # Vol spike (>25%), upside surprise, >5% move
    SPIKE_DOWN = "spike_down"             # Vol spike (>25%), downside surprise
    CRISIS = "crisis"                     # Vol>30%, panic zone


# ============ REGIME WEIGHTS ============
# 7 event types × 9 regimes: importance of each event in each regime (%)
REGIME_WEIGHTS = {
    MarketRegime.BULL_STRONG: {
        'earnings': 0.20,
        'merger': 0.16,
        'policy': 0.08,
        'order_win': 0.22,
        'dividend': 0.08,
        'supply': 0.12,
        'insider': 0.14
    },
    MarketRegime.BULL_WEAK: {
        'earnings': 0.22,
        'merger': 0.14,
        'policy': 0.10,
        'order_win': 0.18,
        'dividend': 0.10,
        'supply': 0.14,
        'insider': 0.12
    },
    MarketRegime.BEAR_STRONG: {
        'earnings': 0.18,
        'merger': 0.10,
        'policy': 0.22,
        'order_win': 0.12,
        'dividend': 0.06,
        'supply': 0.20,
        'insider': 0.12
    },
    MarketRegime.BEAR_WEAK: {
        'earnings': 0.20,
        'merger': 0.12,
        'policy': 0.18,
        'order_win': 0.14,
        'dividend': 0.08,
        'supply': 0.16,
        'insider': 0.12
    },
    MarketRegime.SIDEWAYS_CALM: {
        'earnings': 0.18,
        'merger': 0.18,
        'policy': 0.12,
        'order_win': 0.16,
        'dividend': 0.12,
        'supply': 0.12,
        'insider': 0.12
    },
    MarketRegime.SIDEWAYS_CHOPPY: {
        'earnings': 0.20,
        'merger': 0.14,
        'policy': 0.14,
        'order_win': 0.16,
        'dividend': 0.10,
        'supply': 0.14,
        'insider': 0.12
    },
    MarketRegime.SPIKE_UP: {
        'earnings': 0.16,
        'merger': 0.20,
        'policy': 0.08,
        'order_win': 0.22,
        'dividend': 0.06,
        'supply': 0.10,
        'insider': 0.18
    },
    MarketRegime.SPIKE_DOWN: {
        'earnings': 0.18,
        'merger': 0.08,
        'policy': 0.18,
        'order_win': 0.10,
        'dividend': 0.06,
        'supply': 0.24,
        'insider': 0.16
    },
    MarketRegime.CRISIS: {
        'earnings': 0.16,
        'merger': 0.06,
        'policy': 0.24,
        'order_win': 0.08,
        'dividend': 0.04,
        'supply': 0.22,
        'insider': 0.20
    }
}

# ============ SENTIMENT MULTIPLIERS ============
# 9 regimes × 3 sentiments: how much to boost/penalize alpha
SENTIMENT_BOOST = {
    MarketRegime.BULL_STRONG: {
        'bullish': 1.35,    # Confirmation
        'bearish': 0.60,    # Contrarian discount
        'neutral': 1.00
    },
    MarketRegime.BULL_WEAK: {
        'bullish': 1.25,
        'bearish': 0.65,
        'neutral': 1.00
    },
    MarketRegime.BEAR_STRONG: {
        'bullish': 1.50,    # Contrarian value HIGH
        'bearish': 0.50,    # Already priced in
        'neutral': 0.90
    },
    MarketRegime.BEAR_WEAK: {
        'bullish': 1.40,
        'bearish': 0.55,
        'neutral': 0.95
    },
    MarketRegime.SIDEWAYS_CALM: {
        'bullish': 1.20,    # Catalyst for breakout
        'bearish': 0.85,
        'neutral': 1.00
    },
    MarketRegime.SIDEWAYS_CHOPPY: {
        'bullish': 1.10,
        'bearish': 0.90,
        'neutral': 1.00
    },
    MarketRegime.SPIKE_UP: {
        'bullish': 1.45,    # Ride the wave
        'bearish': 0.70,
        'neutral': 1.05
    },
    MarketRegime.SPIKE_DOWN: {
        'bullish': 1.40,    # Deep contrarian
        'bearish': 0.55,
        'neutral': 0.85
    },
    MarketRegime.CRISIS: {
        'bullish': 1.60,    # Extreme deep value
        'bearish': 0.40,
        'neutral': 0.70
    }
}

# ============ SIGNAL VALIDITY THRESHOLDS ============
# Minimum alpha score and confidence by regime (to avoid noise)
VALIDITY_THRESHOLDS = {
    MarketRegime.BULL_STRONG: {'min_alpha': 65, 'min_confidence': 0.70},    # Most permissive
    MarketRegime.BULL_WEAK: {'min_alpha': 70, 'min_confidence': 0.75},
    MarketRegime.BEAR_STRONG: {'min_alpha': 75, 'min_confidence': 0.80},   # Most strict
    MarketRegime.BEAR_WEAK: {'min_alpha': 72, 'min_confidence': 0.78},
    MarketRegime.SIDEWAYS_CALM: {'min_alpha': 80, 'min_confidence': 0.82}, # Need conviction
    MarketRegime.SIDEWAYS_CHOPPY: {'min_alpha': 75, 'min_confidence': 0.80},
    MarketRegime.SPIKE_UP: {'min_alpha': 70, 'min_confidence': 0.72},       # Ride wave
    MarketRegime.SPIKE_DOWN: {'min_alpha': 72, 'min_confidence': 0.75},
    MarketRegime.CRISIS: {'min_alpha': 80, 'min_confidence': 0.85}         # Ultra-strict
}


@dataclass
class MarketData:
    """Current market snapshot"""
    volatility: float           # σ of last 30 days (0-1)
    price_change_1d: float      # 1-day % change
    momentum_score: float       # -1 to +1 (from indicators)
    trend_strength: float       # 0-1 (how strong is trend)
    sector_momentum: float      # -1 to +1 (sector momentum)


@dataclass
class StockContext:
    """Per-stock context for smarter scoring"""
    ticker: str
    stock_change_pct: float = 0.0       # This stock's 30d return
    stock_volatility: float = 0.0       # This stock's volatility
    sector: str = ''                    # Sector name (IT, ENERGY, etc.)
    sector_momentum: float = 0.0        # Sector-level momentum (-1 to +1)
    relative_strength: float = 0.0      # Stock return minus sector return


@dataclass
class EventData:
    """Event extracted from news/scraper"""
    event_type: str             # earnings, merger, policy, order_win, dividend, supply, insider
    ticker: str                 # Stock symbol
    sentiment: str              # bullish, bearish, neutral
    magnitude: float            # 1-10 scale
    confidence: float           # 0-1 classification confidence
    description: str            # Event details
    # Optional enrichment fields (default None → multiplier = 1.0)
    source_count: int = 1       # How many independent sources corroborate this event
    entity_mentions: int = 1    # How many ticker-specific sentence matches
    total_sentences: int = 5    # Total article sentences (for narrative density calc)
    prior_magnitude: float = 0  # Prior event magnitude for same ticker (surprise calc)


class RegimeDetector:
    """Auto-detects market regime from market data"""
    
    @staticmethod
    def detect_regime(market_data: MarketData) -> MarketRegime:
        """
        Classify market into one of 9 regimes using volatility, price, momentum, trend
        
        Args:
            market_data: Current market snapshot
            
        Returns:
            MarketRegime enum value
        """
        vol = market_data.volatility
        price_change = market_data.price_change_1d
        momentum = market_data.momentum_score
        trend = market_data.trend_strength
        
        # Spike detection (>5% moves or vol spike)
        if abs(price_change) > 0.05 or vol > 0.25:
            if price_change > 0.05:
                return MarketRegime.SPIKE_UP
            else:
                return MarketRegime.SPIKE_DOWN
        
        # Crisis detection (extreme vol)
        if vol > 0.30:
            return MarketRegime.CRISIS
        
        # Trend detection
        if trend > 0.6:  # Strong trend
            if momentum > 0:  # Uptrend
                return MarketRegime.BULL_STRONG if vol < 0.15 else MarketRegime.BULL_WEAK
            else:  # Downtrend
                return MarketRegime.BEAR_STRONG if vol > 0.20 else MarketRegime.BEAR_WEAK
        else:  # Weak/no trend (sideways)
            if vol < 0.10:
                return MarketRegime.SIDEWAYS_CALM
            else:
                return MarketRegime.SIDEWAYS_CHOPPY
    
    @staticmethod
    def get_regime_strength(regime: MarketRegime, market_data: MarketData) -> float:
        """
        Get how "strong" the detected regime is (0-1)
        Used to calibrate confidence
        """
        if regime in [MarketRegime.CRISIS, MarketRegime.SPIKE_UP, MarketRegime.SPIKE_DOWN]:
            return 0.95  # Spikes and crises are unambiguous
        
        if regime in [MarketRegime.BULL_STRONG, MarketRegime.BEAR_STRONG]:
            return market_data.trend_strength  # Strength = trend strength
        
        # Sideways and weak markets are less clear
        return 0.6 + market_data.trend_strength * 0.2


class AlphaScoringEngine:
    """Core alpha score calculation with sector-aware and stock-level intelligence"""

    # ---- NEW MULTIPLIER HELPERS ----

    @staticmethod
    def narrative_strength(event: 'EventData') -> float:
        """
        Factor 9 — NarrativeStrength (0.85 – 1.20×)
        Measures how coherent and specific the event description is.
        - High entity density (many ticker-specific sentences) = strong narrative
        - Low entity density (generic market noise) = weak narrative
        - Corroborated by ≥2 independent sources = bonus
        Range is compressed to avoid outlier domination.
        """
        if event.total_sentences <= 0:
            return 1.0

        density = event.entity_mentions / max(event.total_sentences, 1)
        # density 0→0 maps to 0.85, density 1→1 maps to 1.15, cap at 1.20
        raw = 0.85 + (density * 0.35)
        raw = min(1.20, raw)

        # Source corroboration bonus: each extra source adds 0.03 (cap +0.10)
        source_bonus = min(0.10, (max(1, event.source_count) - 1) * 0.03)
        return round(min(1.20, raw + source_bonus), 3)

    @staticmethod
    def surprise_factor(event: 'EventData') -> float:
        """
        Factor 10 — SurpriseFactor (0.80 – 1.30×)
        How much does this event deviate from the recent baseline?
        - No prior event (new catalyst): neutral 1.0
        - Same magnitude as prior: slight discount (0.90) — already priced in
        - Much stronger than prior: big surprise bonus (up to 1.30)
        - Much weaker than prior: miss penalty (down to 0.80)
        """
        prior = event.prior_magnitude
        if prior <= 0:
            return 1.0  # No history → no adjustment

        delta = event.magnitude - prior  # positive = beat, negative = miss
        # Scale delta [-10, +10] → factor [-0.20, +0.30]
        if delta >= 0:
            factor = 1.0 + min(0.30, delta * 0.05)
        else:
            factor = 1.0 + max(-0.20, delta * 0.04)
        return round(factor, 3)

    @staticmethod
    def flow_score(event: 'EventData', market_cap_category: str = 'large') -> float:
        """
        Factor 11 — FlowScore (0.88 – 1.15×)
        Proxy for institutional attention and liquidity.
        - Large caps have high institutional coverage → signals more reliable → slight boost
        - Small caps have low coverage → signals less reliable but higher alpha when confirmed
        - Events with high confidence in small caps = big surprise bonus
        """
        if market_cap_category == 'large':
            # Large cap: well covered, moderate boost for high-confidence events
            return round(0.95 + event.confidence * 0.20, 3)   # 0.95–1.15
        elif market_cap_category == 'mid':
            # Mid cap: balanced
            return round(0.92 + event.confidence * 0.15, 3)   # 0.92–1.07
        else:
            # Small cap: low coverage, higher noise discount but surprise bonus
            base = 0.88 + event.confidence * 0.12             # 0.88–1.00
            surprise_adj = 0.05 if event.magnitude > 7 else 0
            return round(min(1.10, base + surprise_adj), 3)

    @staticmethod
    def calculate_alpha_score(
        event: EventData,
        market_data: MarketData,
        regime: MarketRegime,
        market_cap_category: str = 'large',
        days_old: int = 0,
        stock_context: 'StockContext | None' = None
    ) -> float:
        """
        Calculate alpha score (0-100) using 8-factor formula:
        α = Base × Weight × Sentiment × Timing × Quality × SectorMom × RelStrength × Conviction

        Factors 7-8 are new: they use per-stock and per-sector data to handle
        scenarios like "stock has good earnings but sector is down".

        Args:
            event: EventData object
            market_data: Current market conditions
            regime: Detected market regime
            market_cap_category: Size of company
            days_old: Age of event in days (for time decay)
            stock_context: Per-stock context (sector, relative strength, etc.)

        Returns:
            Alpha score 0-100
        """

        # Factor 1: Base Score
        # magnitude (0-10) and confidence (0-1) combine to a 0-65 base.
        # Bumped from 55 (which capped the realised distribution at ~73) so the
        # downstream 80+ "TOP PICK" bucket is reachable when all factors align.
        magnitude_component = event.magnitude * 5.0    # 0-10 → 0-50
        confidence_component = event.confidence * 18    # 0-1  → 0-18
        base_score = min(65, magnitude_component + confidence_component)

        # Factor 2: Event Weight Normalization (capped at 1.5x — was 1.3)
        all_weights = REGIME_WEIGHTS[regime]
        avg_weight = sum(all_weights.values()) / len(all_weights)
        event_weight = all_weights.get(event.event_type, avg_weight)
        weight_factor = min(1.5, event_weight / avg_weight)

        # Factor 3: Sentiment Adjustment (dampened — max ~1.30x instead of 1.6x raw)
        raw_sentiment = SENTIMENT_BOOST[regime].get(event.sentiment, 1.0)
        sentiment_adj = 1.0 + (raw_sentiment - 1.0) * 0.7  # Slightly less dampening

        # Factor 4: Exponential Timing Decay — exp(-λt)
        # λ = 0.35 calibrated so: day 0=1.0, day 1=0.70, day 3=0.35, day 7=0.09
        # Floor at 0.05 so very old events still carry minimal weight
        DECAY_LAMBDA = 0.35
        timing_factor = max(0.05, math.exp(-DECAY_LAMBDA * days_old))

        # Factor 5: Company Quality (market cap)
        co_quality_map = {'large': 1.0, 'mid': 1.1, 'small': 0.7}
        co_quality = co_quality_map.get(market_cap_category, 1.0)

        # Factor 6: Sector Momentum
        # Use per-sector momentum if available, otherwise fall back to market-wide
        if stock_context and stock_context.sector_momentum != 0:
            sector_mom = 1.0 + (stock_context.sector_momentum * 0.2)
        else:
            sector_mom = 1.0 + (market_data.sector_momentum * 0.2)

        # Factor 7: Relative Strength (NEW)
        # Stock outperforming its own sector = strong positive signal
        # Stock underperforming its sector = red flag even if news looks good
        # Range: 0.85x to 1.25x
        relative_strength_factor = 1.0
        if stock_context and stock_context.relative_strength != 0:
            rs = stock_context.relative_strength  # stock_return - sector_return
            # Clamp to [-20, +20] pct range, then normalize to [0.85, 1.25]
            rs_clamped = max(-20, min(20, rs))
            relative_strength_factor = 1.0 + (rs_clamped / 100.0)  # e.g., +10% RS → 1.10x

        # Factor 8: Sentiment-Sector Divergence Bonus (NEW)
        # If company sentiment is bullish but sector is DOWN, the company-level
        # catalyst is overcoming a headwind → this is EXTRA alpha.
        # Conversely, bullish sentiment in a soaring sector may just be sector-wide lift.
        divergence_bonus = 1.0
        if stock_context and stock_context.sector_momentum != 0:
            if event.sentiment == 'bullish' and stock_context.sector_momentum < -0.1:
                # Bullish company in bearish sector = strong stock-specific catalyst
                divergence_bonus = 1.10 + abs(stock_context.sector_momentum) * 0.15
                # Cap at 1.25x
                divergence_bonus = min(1.25, divergence_bonus)
            elif event.sentiment == 'bearish' and stock_context.sector_momentum > 0.1:
                # Bearish company in bullish sector = company-specific problem
                divergence_bonus = 1.08 + stock_context.sector_momentum * 0.1
                divergence_bonus = min(1.20, divergence_bonus)
            elif event.sentiment == 'bullish' and stock_context.sector_momentum > 0.3:
                # Bullish in very bullish sector = might just be sector lift, slight discount
                divergence_bonus = 0.95

        # Factor 9: NarrativeStrength — specificity and source corroboration
        narrative = AlphaScoringEngine.narrative_strength(event)

        # Factor 10: SurpriseFactor — deviation from prior event baseline
        surprise = AlphaScoringEngine.surprise_factor(event)

        # Factor 11: FlowScore — institutional attention / liquidity proxy
        flow = AlphaScoringEngine.flow_score(event, market_cap_category)

        # Final 11-factor calculation
        alpha = (base_score * weight_factor * sentiment_adj * timing_factor
                 * co_quality * sector_mom * relative_strength_factor * divergence_bonus
                 * narrative * surprise * flow)

        alpha = max(0, min(100, alpha))

        # Store factor breakdown for explanation (attached to class-level for retrieval)
        AlphaScoringEngine._last_breakdown = {
            'alpha': round(alpha, 1),
            'base_score': round(base_score, 1),
            'weight_factor': round(weight_factor, 2),
            'sentiment_adj': round(sentiment_adj, 2),
            'timing_factor': timing_factor,
            'co_quality': co_quality,
            'sector_mom': round(sector_mom, 2),
            'relative_strength': round(relative_strength_factor, 2),
            'divergence_bonus': round(divergence_bonus, 2),
            'narrative_strength': round(narrative, 3),
            'surprise_factor': round(surprise, 3),
            'flow_score': round(flow, 3),
        }

        return alpha


class SignalValidator:
    """Validates signals based on regime-specific thresholds"""
    
    @staticmethod
    def is_valid_signal(
        alpha_score: float,
        confidence: float,
        regime: MarketRegime
    ) -> Tuple[bool, Dict]:
        """
        Check if signal meets regime-specific validity thresholds
        
        Returns:
            (is_valid, metadata_dict)
        """
        thresholds = VALIDITY_THRESHOLDS[regime]
        min_alpha = thresholds['min_alpha']
        min_confidence = thresholds['min_confidence']
        
        is_valid = alpha_score >= min_alpha and confidence >= min_confidence
        
        return is_valid, {
            'alpha_score': alpha_score,
            'confidence': confidence,
            'regime': regime.value,
            'min_alpha_required': min_alpha,
            'min_confidence_required': min_confidence,
            'passed': is_valid,
            'alpha_margin': alpha_score - min_alpha,
            'confidence_margin': confidence - min_confidence
        }


class PredictionEngine:
    """Multi-horizon return prediction with realistic Indian market calibration.

    Base returns are calibrated from NSE historical data:
    - Average post-earnings move (1D): ±2-4% for large caps
    - Average post-merger announcement: ±3-8%
    - Policy impact: gradual, ±0.5-2% over days
    - The alpha_score acts as a quality multiplier — higher alpha = closer to base return

    Returns are DIRECTIONAL: positive base * bullish = positive prediction,
    positive base * bearish = negative prediction (inverted).
    """

    # Realistic base expected returns by event type (%)
    # These represent the TYPICAL move for an average event, before adjustments
    BASE_RETURNS = {
        'earnings': {'1D': 0.8, '3D': 1.5, '5D': 2.0, '20D': 3.5},
        'merger':   {'1D': 1.5, '3D': 3.0, '5D': 4.0, '20D': 6.0},
        'policy':   {'1D': 0.3, '3D': 0.8, '5D': 1.2, '20D': 2.5},
        'order_win':{'1D': 0.6, '3D': 1.2, '5D': 1.8, '20D': 3.0},
        'dividend': {'1D': 0.2, '3D': 0.4, '5D': 0.5, '20D': 1.0},
        'supply':   {'1D': 0.5, '3D': 1.0, '5D': 1.5, '20D': 2.8},
        'insider':  {'1D': 0.7, '3D': 1.5, '5D': 2.2, '20D': 4.0},
        'news':     {'1D': 0.2, '3D': 0.5, '5D': 0.8, '20D': 1.5},
    }

    # How much stock-level volatility amplifies the move
    VOLATILITY_MULTIPLIERS = {
        '1D': 1.0, '3D': 1.1, '5D': 1.15, '20D': 1.25
    }

    # Realistic hit probabilities (how often the direction is correct)
    HIT_PROBABILITIES = {
        '1D': 0.58, '3D': 0.55, '5D': 0.53, '20D': 0.50
    }
    
    @staticmethod
    def predict_return(
        event_type: str,
        alpha_score: float,
        volatility: float,
        regime: MarketRegime,
        sentiment: str,
        horizon: str = '20D'
    ) -> Dict:
        """
        Predict expected return for given horizon
        
        Args:
            event_type: Type of event
            alpha_score: Calculated alpha score (0-100)
            volatility: Market volatility (0-1)
            regime: Detected regime
            sentiment: bullish/bearish/neutral
            horizon: Time horizon ('1D', '3D', '5D', '20D')
            
        Returns:
            Dict with predicted return, confidence, target price info
        """
        
        base_return = PredictionEngine.BASE_RETURNS.get(event_type, PredictionEngine.BASE_RETURNS['news']).get(horizon, 0.3)
        vol_mult = PredictionEngine.VOLATILITY_MULTIPLIERS.get(horizon, 1.0)
        hit_prob = PredictionEngine.HIT_PROBABILITIES.get(horizon, 0.5)

        # Alpha quality factor (50 = baseline, higher = stronger signal)
        alpha_factor = 0.5 + (alpha_score / 100.0)  # Range: 0.5x to 1.5x

        # Regime adjustment — how the market environment affects the move
        regime_adjustments = {
            MarketRegime.BULL_STRONG: 1.2,     # Strong trend amplifies moves
            MarketRegime.BULL_WEAK: 1.05,
            MarketRegime.BEAR_STRONG: 0.8,     # Bear markets dampen positive expectations
            MarketRegime.BEAR_WEAK: 0.9,
            MarketRegime.SIDEWAYS_CALM: 0.7,   # Low volatility = smaller moves
            MarketRegime.SIDEWAYS_CHOPPY: 0.85,
            MarketRegime.SPIKE_UP: 1.3,        # Momentum amplifies
            MarketRegime.SPIKE_DOWN: 1.2,      # High vol = big moves either way
            MarketRegime.CRISIS: 1.4,          # Crisis = extreme moves
        }
        regime_adj = regime_adjustments.get(regime, 1.0)

        # Direction: STRICT — no positive bias for neutral
        # bullish=+1, bearish=-1, neutral=0 (genuinely no direction)
        # Neutral events with alpha still have value in cross-sectional ranking but zero predicted move
        if sentiment == 'bullish':
            direction = 1.0
        elif sentiment == 'bearish':
            direction = -1.0
        else:
            direction = 0.0  # FIXED: was 0.3, causing systematic long bias

        # Volatility scaling — higher vol stocks have bigger moves, direction-agnostic
        vol_scale = 1.0 + (volatility * 2.0)  # vol=0.1 → 1.2x, vol=0.3 → 1.6x

        # Regime adjustment for bearish: BEAR regimes amplify negative returns
        # Previously regime_adj was always > 0 and multiplied magnitude only.
        # Now we preserve its direction-agnostic role (adjusts magnitude of move, not direction)
        predicted_return = direction * base_return * alpha_factor * vol_mult * regime_adj * vol_scale
        # Apply data-driven magnitude calibration (learned weekly from resolved
        # predictions). Multiplier defaults to 1.0 until the first fit.
        predicted_return *= _MAGNITUDE_MULT.get(horizon, 1.0)

        # Confidence: base hit prob * alpha quality * regime clarity
        regime_clarity = {
            MarketRegime.BULL_STRONG: 1.1, MarketRegime.BEAR_STRONG: 1.1,
            MarketRegime.CRISIS: 0.7, MarketRegime.SIDEWAYS_CHOPPY: 0.8,
        }
        prediction_confidence = hit_prob * (alpha_score / 80.0) * regime_clarity.get(regime, 1.0)
        prediction_confidence = max(0.1, min(0.95, prediction_confidence))

        return {
            'horizon': horizon,
            'predicted_return_pct': round(predicted_return, 3),
            'confidence': round(prediction_confidence, 3),
            'base_return': base_return,
            'alpha_factor': round(alpha_factor, 2),
            'volatility_multiplier': round(vol_mult, 2),
            'regime_adjustment': round(regime_adj, 2),
            'direction': direction,
        }
    
    @staticmethod
    def predict_price_targets(
        entry_price: float,
        alpha_score: float,
        predicted_returns: Dict[str, float]  # horizon -> return %
    ) -> Dict:
        """
        Generate price targets for each horizon
        
        Returns:
            Dict with price targets and confidence ranges
        """
        targets = {}
        for horizon, ret_pct in predicted_returns.items():
            target_price = entry_price * (1 + ret_pct / 100.0)
            targets[horizon] = {
                'target_price': target_price,
                'return_pct': ret_pct,
                'upside': target_price - entry_price,
                'upside_pct': ((target_price - entry_price) / entry_price) * 100
            }
        
        return targets


# ============ EXAMPLE USAGE ============
if __name__ == "__main__":
    # Create sample market data
    market_data = MarketData(
        volatility=0.18,
        price_change_1d=0.02,
        momentum_score=0.65,
        trend_strength=0.75,
        sector_momentum=0.35
    )
    
    # Detect regime
    regime = RegimeDetector.detect_regime(market_data)
    regime_strength = RegimeDetector.get_regime_strength(regime, market_data)
    print(f"Detected Regime: {regime.value} (strength: {regime_strength:.2%})")
    
    # Create sample event
    event = EventData(
        event_type='earnings',
        ticker='NVDA',
        sentiment='bullish',
        magnitude=8.5,
        confidence=0.88,
        description='Q1 earnings beat expectations, raised FY guidance'
    )
    
    # Calculate alpha score
    alpha = AlphaScoringEngine.calculate_alpha_score(
        event=event,
        market_data=market_data,
        regime=regime,
        market_cap_category='large',
        days_old=0
    )
    print(f"Alpha Score: {alpha:.1f}/100")
    
    # Validate signal
    is_valid, validation = SignalValidator.is_valid_signal(
        alpha_score=alpha,
        confidence=0.88,
        regime=regime
    )
    print(f"Signal Valid: {is_valid}")
    print(f"Validation: {validation}")
    
    # Predict returns
    prediction_3d = PredictionEngine.predict_return(
        event_type='earnings',
        alpha_score=alpha,
        volatility=market_data.volatility,
        regime=regime,
        sentiment='bullish',
        horizon='3D'
    )
    print(f"3D Prediction: {prediction_3d['predicted_return_pct']:.2f}%")
    
    prediction_20d = PredictionEngine.predict_return(
        event_type='earnings',
        alpha_score=alpha,
        volatility=market_data.volatility,
        regime=regime,
        sentiment='bullish',
        horizon='20D'
    )
    print(f"20D Prediction: {prediction_20d['predicted_return_pct']:.2f}%")
    
    # Price targets
    targets = PredictionEngine.predict_price_targets(
        entry_price=800.00,
        alpha_score=alpha,
        predicted_returns={'3D': prediction_3d['predicted_return_pct'], '20D': prediction_20d['predicted_return_pct']}
    )
    print(f"Price Targets: {targets}")
