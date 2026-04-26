"""
EventAlpha - Metrics & Backtesting Engine
5-layer confidence system, multi-horizon predictions, backtesting analytics
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from datetime import datetime
import statistics
import math


@dataclass
class Signal:
    """Complete signal with all metadata"""
    signal_id: str
    event_type: str
    ticker: str
    alpha_score: float
    regime: str
    entry_price: float
    entry_timestamp: datetime
    sentiment: str
    confidence: float
    expected_return_1d: float
    expected_return_3d: float
    expected_return_20d: float
    description: str


@dataclass
class PredictionResult:
    """Prediction outcome after horizon completes"""
    signal_id: str
    horizon: str  # '1D', '3D', '5D', '20D'
    predicted_return_pct: float
    actual_return_pct: float
    exit_price: float
    actual_timestamp: datetime
    hit_target: bool  # Did it go the right direction?


@dataclass
class BacktestResult:
    """Single trade result"""
    signal_id: str
    event_type: str
    alpha_score: float
    entry_price: float
    exit_price: float
    holding_days: int
    gross_return_pct: float
    net_return_pct: float
    fees_pct: float = 0.1  # Assume 10 bps round-trip
    win: bool = False
    prediction_accuracy: float = 0.0  # % of horizons hit target


class ConfidenceCalculator:
    """5-layer confidence scoring system"""
    
    @staticmethod
    def calculate_event_confidence(
        keywords_matched: int,
        total_keywords: int,
        text_length: int,
        source_reliability: float = 0.8
    ) -> float:
        """
        Layer 1: Event Classification Confidence
        
        Args:
            keywords_matched: Number of relevant keywords found
            total_keywords: Total keywords in classifier
            text_length: Article/headline length (longer = more context)
            source_reliability: Trust score of source (0-1)
            
        Returns:
            Event confidence 0-1
        """
        keyword_match_rate = keywords_matched / max(total_keywords, 1)
        length_factor = min(1.0, text_length / 500)  # Normalize by typical length
        
        confidence = keyword_match_rate * 0.6 + length_factor * 0.2 + source_reliability * 0.2
        return min(1.0, confidence)
    
    @staticmethod
    def calculate_sentiment_confidence(
        positive_keywords: int,
        negative_keywords: int
    ) -> float:
        """
        Layer 2: Sentiment Confidence
        High when strong sentiment signal, low when mixed
        
        Returns:
            Sentiment confidence 0-1
        """
        total_keywords = positive_keywords + negative_keywords
        if total_keywords == 0:
            return 0.3  # Uncertain if no keywords
        
        # Confidence is how dominant the primary sentiment is
        dominance = max(positive_keywords, negative_keywords) / total_keywords
        
        # Apply dampening for weak signals
        if total_keywords < 3:
            dominance *= 0.6  # Low keyword count = less reliable
        
        return dominance
    
    @staticmethod
    def calculate_alpha_confidence(
        alpha_score: float,
        regime_strength: float = 0.8
    ) -> float:
        """
        Layer 3: Alpha Score Confidence
        Higher alpha scores = higher confidence
        
        Returns:
            Alpha confidence 0-1
        """
        # Normalize alpha score (70 = baseline 0.5, 100 = 1.0)
        base_alpha_conf = min(1.0, alpha_score / 100.0)
        
        # Apply regime strength adjustment
        alpha_confidence = base_alpha_conf * (0.5 + regime_strength * 0.5)
        
        return alpha_confidence
    
    @staticmethod
    def calculate_overall_signal_confidence(
        event_confidence: float,
        sentiment_confidence: float,
        alpha_confidence: float,
        regime_alignment: float = 0.8  # How well event type fits regime
    ) -> float:
        """
        Layer 4: Overall Composite Confidence
        Weighted combination of all 3 layers
        
        Weights:
            - Event: 25% (classification accuracy)
            - Sentiment: 30% (directional clarity)
            - Alpha: 30% (quantitative quality)
            - Regime: 15% (market context fitness)
        
        Returns:
            Overall signal confidence 0-1
        """
        composite = (
            event_confidence * 0.25 +
            sentiment_confidence * 0.30 +
            alpha_confidence * 0.30 +
            regime_alignment * 0.15
        )
        
        return min(1.0, composite)
    
    @staticmethod
    def calculate_backtested_confidence(
        historical_correct_predictions: int,
        total_historical_predictions: int,
        win_rate: float,
        event_type: str = None
    ) -> float:
        """
        Layer 5: Backtested Confidence
        Empirical success rate from past signals of this type
        
        Returns:
            Backtested confidence 0-1
        """
        if total_historical_predictions == 0:
            return 0.5  # Default to neutral if no history
        
        raw_accuracy = historical_correct_predictions / total_historical_predictions
        
        # Adjust by win rate quality (avoid "lucky" predictions)
        quality_adjustment = win_rate
        
        backtested_conf = raw_accuracy * 0.7 + quality_adjustment * 0.3
        
        return min(1.0, backtested_conf)


class MultiHorizonPredictor:
    """Multi-horizon return predictions (1D-20D)"""
    
    # Base return expectations by event type and horizon
    BASE_RETURNS = {
        'earnings': {'1D': 1.2, '2D': 2.8, '3D': 5.2, '5D': 8.5, '20D': 18.2},
        'merger': {'1D': 2.5, '2D': 4.2, '3D': 8.5, '5D': 12.0, '20D': 22.5},
        'policy': {'1D': 0.8, '2D': 1.5, '3D': 3.5, '5D': 5.2, '20D': 10.8},
        'order_win': {'1D': 1.5, '2D': 2.8, '3D': 4.2, '5D': 6.5, '20D': 14.2},
        'dividend': {'1D': 0.5, '2D': 0.8, '3D': 1.2, '5D': 1.8, '20D': 3.5},
        'supply': {'1D': -2.2, '2D': -3.8, '3D': -5.5, '5D': -8.2, '20D': -15.0},
        'insider': {'1D': 1.8, '2D': 3.2, '3D': 6.0, '5D': 9.0, '20D': 16.5}
    }
    
    # How much volatility scales returns over time
    VOLATILITY_MULTIPLIERS = {
        '1D': 1.0,
        '2D': 1.15,
        '3D': 1.25,
        '5D': 1.35,
        '20D': 1.42
    }
    
    # Probability of hitting target by horizon
    HIT_PROBABILITIES = {
        '1D': 0.62,
        '2D': 0.60,
        '3D': 0.57,
        '5D': 0.54,
        '20D': 0.50
    }
    
    @staticmethod
    def predict_multi_horizon(
        event_type: str,
        alpha_score: float,
        volatility: float,
        regime: str,
        sentiment: str
    ) -> Dict[str, Dict]:
        """
        Generate predictions for all horizons (1D, 2D, 3D, 5D, 20D)
        
        Returns:
            Dict: {
                '1D': {'predicted_return': X, 'confidence': Y, ...},
                '3D': {...},
                ...
            }
        """
        horizons = ['1D', '2D', '3D', '5D', '20D']
        predictions = {}
        
        for horizon in horizons:
            base_return = MultiHorizonPredictor.BASE_RETURNS.get(event_type, {}).get(horizon, 0)
            vol_mult = MultiHorizonPredictor.VOLATILITY_MULTIPLIERS[horizon]
            hit_prob = MultiHorizonPredictor.HIT_PROBABILITIES[horizon]
            
            # Alpha normalization
            alpha_factor = alpha_score / 70.0
            
            # Regime adjustments
            regime_adj_map = {
                'bull_strong': 1.15,
                'bull_weak': 1.05,
                'bear_strong': 0.75,
                'bear_weak': 0.85,
                'sideways_calm': 1.00,
                'sideways_choppy': 0.95,
                'spike_up': 1.30,
                'spike_down': 0.40,
                'crisis': 0.50
            }
            regime_adj = regime_adj_map.get(regime, 1.0)
            
            # Sentiment boost
            sentiment_boost = 1.0
            if sentiment == 'bullish':
                sentiment_boost = 1.2
            elif sentiment == 'bearish':
                sentiment_boost = 0.8
            
            # Calculate prediction
            predicted_return = base_return * alpha_factor * vol_mult * regime_adj * sentiment_boost
            
            # Confidence decreases with time horizon (more uncertainty)
            prediction_confidence = hit_prob * (alpha_score / 100.0)
            
            predictions[horizon] = {
                'predicted_return_pct': predicted_return,
                'confidence': prediction_confidence,
                'base_return': base_return,
                'alpha_factor': alpha_factor,
                'vol_multiplier': vol_mult,
                'regime_adj': regime_adj,
                'sentiment_boost': sentiment_boost,
                'hit_probability': hit_prob
            }
        
        return predictions
    
    @staticmethod
    def predict_price_target(
        entry_price: float,
        predicted_returns: Dict[str, float]
    ) -> Dict[str, Dict]:
        """
        Generate price targets for each horizon
        
        Returns:
            Dict: {
                '1D': {'target_price': X, 'upside': Y, ...},
                ...
            }
        """
        targets = {}
        for horizon, ret_pct in predicted_returns.items():
            target_price = entry_price * (1 + ret_pct / 100.0)
            targets[horizon] = {
                'target_price': round(target_price, 2),
                'return_pct': round(ret_pct, 2),
                'upside': round(target_price - entry_price, 2),
                'upside_pct': round((target_price - entry_price) / entry_price * 100, 2)
            }
        
        return targets


class BacktestEngine:
    """Backtesting and performance analytics"""
    
    @staticmethod
    def calculate_return(entry_price: float, exit_price: float, fees_pct: float = 0.1) -> Tuple[float, float]:
        """
        Calculate gross and net returns
        
        Returns:
            (gross_return_pct, net_return_pct)
        """
        gross_return_pct = ((exit_price - entry_price) / entry_price) * 100
        fee_amount = (entry_price + exit_price) / 2 * (fees_pct / 100)
        net_return_pct = ((exit_price - entry_price - fee_amount) / entry_price) * 100
        
        return gross_return_pct, net_return_pct
    
    @staticmethod
    def calculate_performance_metrics(results: List[BacktestResult]) -> Dict:
        """
        Calculate comprehensive performance metrics from backtest results
        
        Returns:
            Dict with: accuracy, win_rate, avg_win, avg_loss, sharpe_ratio, max_drawdown, etc.
        """
        if not results:
            return {'error': 'No backtest results'}
        
        winning_trades = [r for r in results if r.win]
        losing_trades = [r for r in results if not r.win]
        all_returns = [r.net_return_pct for r in results]
        
        # Basic stats
        num_trades = len(results)
        wins = len(winning_trades)
        losses = len(losing_trades)
        
        accuracy = wins / num_trades if num_trades > 0 else 0
        win_rate = accuracy
        
        # Return stats
        avg_win = sum([r.net_return_pct for r in winning_trades]) / len(winning_trades) if winning_trades else 0
        avg_loss = sum([r.net_return_pct for r in losing_trades]) / len(losing_trades) if losing_trades else 0
        
        total_profit = sum([r.net_return_pct for r in winning_trades])
        total_loss = abs(sum([r.net_return_pct for r in losing_trades]))
        
        profit_factor = total_profit / total_loss if total_loss > 0 else (1.0 if total_profit > 0 else 0)
        
        # Sharpe ratio (assuming 252 trading days, 0% risk-free rate)
        avg_return = sum(all_returns) / len(all_returns)
        std_dev = statistics.stdev(all_returns) if len(all_returns) > 1 else 0
        sharpe_ratio = (avg_return / std_dev * math.sqrt(252)) if std_dev > 0 else 0
        
        # Max drawdown
        cumulative_returns = []
        cumulative = 0
        for ret in all_returns:
            cumulative += ret
            cumulative_returns.append(cumulative)
        
        max_drawdown = 0
        if cumulative_returns:
            running_max = cumulative_returns[0]
            for ret in cumulative_returns:
                if ret < running_max:
                    max_drawdown = min(max_drawdown, ret - running_max)
                running_max = max(running_max, ret)
        
        return {
            'total_trades': num_trades,
            'winning_trades': wins,
            'losing_trades': losses,
            'accuracy_pct': round(accuracy * 100, 2),
            'win_rate_pct': round(win_rate * 100, 2),
            'avg_win_pct': round(avg_win, 2),
            'avg_loss_pct': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'avg_return_pct': round(avg_return, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'max_drawdown_pct': round(max_drawdown, 2),
            'total_return_pct': round(sum(all_returns), 2)
        }
    
    @staticmethod
    def analyze_alpha_buckets(results: List[BacktestResult]) -> Dict:
        """
        Stratify performance by alpha score bands
        
        Returns:
            Dict: {
                '80-100': {'accuracy': 72%, 'avg_return': 4.8%, ...},
                '70-79': {...},
                ...
            }
        """
        buckets = {
            '80-100': [],
            '70-79': [],
            '60-69': [],
            '50-59': [],
            '<50': []
        }
        
        for result in results:
            alpha = result.alpha_score
            if alpha >= 80:
                buckets['80-100'].append(result)
            elif alpha >= 70:
                buckets['70-79'].append(result)
            elif alpha >= 60:
                buckets['60-69'].append(result)
            elif alpha >= 50:
                buckets['50-59'].append(result)
            else:
                buckets['<50'].append(result)
        
        bucket_analysis = {}
        for bucket_name, results_in_bucket in buckets.items():
            if results_in_bucket:
                metrics = BacktestEngine.calculate_performance_metrics(results_in_bucket)
                bucket_analysis[bucket_name] = metrics
            else:
                bucket_analysis[bucket_name] = {'count': 0, 'accuracy': 0}
        
        return bucket_analysis


# ============ DATABASE SCHEMA ============
"""
PostgreSQL/SQLite schema for storing signals, predictions, and backtest results

CREATE TABLE signals (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(50) UNIQUE,
    event_type VARCHAR(20),
    ticker VARCHAR(10),
    alpha_score FLOAT,
    confidence FLOAT,
    entry_price FLOAT,
    entry_timestamp TIMESTAMP,
    regime VARCHAR(20),
    sentiment VARCHAR(10),
    status VARCHAR(20),  -- active, completed, expired
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_entry_timestamp (entry_timestamp),
    INDEX idx_alpha_score (alpha_score)
);

CREATE TABLE predictions (
    id SERIAL PRIMARY KEY,
    signal_id VARCHAR(50) FOREIGN KEY,
    horizon VARCHAR(5),  -- 1D, 3D, 5D, 20D
    predicted_return_pct FLOAT,
    target_price FLOAT,
    actual_price FLOAT,
    actual_return_pct FLOAT,
    exit_timestamp TIMESTAMP,
    hit_target BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_signal_id (signal_id),
    INDEX idx_horizon (horizon)
);

CREATE TABLE backtest_results (
    id SERIAL PRIMARY KEY,
    signal_id VARCHAR(50) FOREIGN KEY,
    event_type VARCHAR(20),
    alpha_score FLOAT,
    entry_price FLOAT,
    exit_price FLOAT,
    entry_timestamp TIMESTAMP,
    exit_timestamp TIMESTAMP,
    holding_days INTEGER,
    gross_return_pct FLOAT,
    net_return_pct FLOAT,
    fees_pct FLOAT DEFAULT 0.1,
    win BOOLEAN,
    prediction_accuracy_pct FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_alpha_score (alpha_score),
    INDEX idx_event_type (event_type),
    INDEX idx_exit_timestamp (exit_timestamp)
);

CREATE TABLE performance_summary (
    id SERIAL PRIMARY KEY,
    alpha_bucket VARCHAR(20),  -- 80-100, 70-79, 60-69, 50-59, <50
    total_signals INTEGER,
    winning_signals INTEGER,
    accuracy_pct FLOAT,
    avg_return_pct FLOAT,
    sharpe_ratio FLOAT,
    max_drawdown_pct FLOAT,
    profit_factor FLOAT,
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE market_regimes (
    id SERIAL PRIMARY KEY,
    date DATE,
    regime VARCHAR(20),
    volatility FLOAT,
    momentum FLOAT,
    trend_strength FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_date (date)
);
"""


# ============ EXAMPLE USAGE ============
if __name__ == "__main__":
    # Test confidence calculation
    print("=== 5-Layer Confidence System ===")
    
    event_conf = ConfidenceCalculator.calculate_event_confidence(
        keywords_matched=5, total_keywords=8, text_length=450, source_reliability=0.9
    )
    print(f"1. Event Confidence: {event_conf:.2%}")
    
    sentiment_conf = ConfidenceCalculator.calculate_sentiment_confidence(
        positive_keywords=8, negative_keywords=1
    )
    print(f"2. Sentiment Confidence: {sentiment_conf:.2%}")
    
    alpha_conf = ConfidenceCalculator.calculate_alpha_confidence(
        alpha_score=85, regime_strength=0.85
    )
    print(f"3. Alpha Confidence: {alpha_conf:.2%}")
    
    overall_conf = ConfidenceCalculator.calculate_overall_signal_confidence(
        event_confidence=event_conf,
        sentiment_confidence=sentiment_conf,
        alpha_confidence=alpha_conf,
        regime_alignment=0.88
    )
    print(f"4. Overall Confidence: {overall_conf:.2%}")
    
    # Test multi-horizon prediction
    print("\n=== Multi-Horizon Predictions ===")
    predictions = MultiHorizonPredictor.predict_multi_horizon(
        event_type='earnings',
        alpha_score=85,
        volatility=0.18,
        regime='bull_strong',
        sentiment='bullish'
    )
    
    for horizon, pred in predictions.items():
        print(f"{horizon}: {pred['predicted_return_pct']:.2f}% (conf: {pred['confidence']:.2%})")
    
    # Test price targets
    print("\n=== Price Targets ===")
    targets = MultiHorizonPredictor.predict_price_target(
        entry_price=200.0,
        predicted_returns={h: p['predicted_return_pct'] for h, p in predictions.items()}
    )
    for horizon, target in targets.items():
        print(f"{horizon}: ${target['target_price']} ({target['upside_pct']:+.2f}%)")
    
    # Test backtest metrics
    print("\n=== Backtest Analysis ===")
    sample_results = [
        BacktestResult('S1', 'earnings', 85, 200, 207, 3, 3.5, 3.4, win=True),
        BacktestResult('S2', 'merger', 92, 150, 154, 5, 2.67, 2.57, win=True),
        BacktestResult('S3', 'policy', 55, 300, 295, 2, -1.67, -1.77, win=False),
        BacktestResult('S4', 'order_win', 78, 120, 127, 4, 5.83, 5.73, win=True),
    ]
    
    metrics = BacktestEngine.calculate_performance_metrics(sample_results)
    print(f"Accuracy: {metrics['accuracy_pct']:.1f}%")
    print(f"Sharpe Ratio: {metrics['sharpe_ratio']:.2f}")
    print(f"Profit Factor: {metrics['profit_factor']:.2f}x")
    
    # Alpha bucket analysis
    bucket_analysis = BacktestEngine.analyze_alpha_buckets(sample_results)
    print("\nAlpha Bucket Performance:")
    for bucket, metrics in bucket_analysis.items():
        if metrics.get('total_trades', 0) > 0:
            print(f"  {bucket}: {metrics['accuracy_pct']:.1f}% accuracy, {metrics['avg_return_pct']:+.2f}% avg return")
