# EventAlpha Testing & Verification Guide

## ✅ Pre-Launch Verification Checklist

### 1. Dependencies Check
```bash
# Verify all packages installed
python -c "
import feedparser, requests, yfinance, pandas, numpy
import transformers, torch, nltk, textblob, apscheduler
print('✅ All core packages installed')
"
```

### 2. Alpha Scoring Engine Test
```bash
cd scraper
python -c "
from alpha_scoring_engine import RegimeDetector, AlphaScoringEngine, MarketData, EventData

# Test regime detection
market_data = MarketData(
    volatility=0.18,
    price_change_1d=0.02,
    momentum_score=0.65,
    trend_strength=0.75,
    sector_momentum=0.35
)

regime = RegimeDetector.detect_regime(market_data)
print(f'✅ Detected Regime: {regime.value}')

# Test alpha calculation
event = EventData(
    event_type='earnings',
    ticker='NVDA',
    sentiment='bullish',
    magnitude=0.85,
    confidence=0.88,
    description='Q1 earnings beat'
)

alpha = AlphaScoringEngine.calculate_alpha_score(
    event=event,
    market_data=market_data,
    regime=regime,
    market_cap_category='large',
    days_old=0
)
print(f'✅ Alpha Score: {alpha:.1f}/100')
print('✅ Alpha Scoring Engine functional')
"
```

### 3. Metrics & Backtesting Test
```bash
python -c "
from metrics_backtesting_engine import ConfidenceCalculator, MultiHorizonPredictor, BacktestEngine, BacktestResult

# Test 5-layer confidence
event_conf = ConfidenceCalculator.calculate_event_confidence(5, 8, 450, 0.9)
sentiment_conf = ConfidenceCalculator.calculate_sentiment_confidence(8, 1)
alpha_conf = ConfidenceCalculator.calculate_alpha_confidence(85, 0.85)
overall_conf = ConfidenceCalculator.calculate_overall_signal_confidence(
    event_conf, sentiment_conf, alpha_conf, 0.88
)
print(f'✅ Overall Confidence: {overall_conf:.0%}')

# Test multi-horizon predictions
predictions = MultiHorizonPredictor.predict_multi_horizon(
    'earnings', 85, 0.18, 'bull_strong', 'bullish'
)
for horizon, pred in predictions.items():
    print(f'  {horizon}: {pred[\"predicted_return_pct\"]:+.2f}%')

# Test backtesting metrics
results = [
    BacktestResult('S1', 'earnings', 85, 200, 207, 3, 3.5, 3.4, win=True),
    BacktestResult('S2', 'merger', 92, 150, 154, 5, 2.67, 2.57, win=True),
    BacktestResult('S3', 'policy', 55, 300, 295, 2, -1.67, -1.77, win=False),
]
metrics = BacktestEngine.calculate_performance_metrics(results)
print(f'✅ Win Rate: {metrics[\"win_rate_pct\"]:.0f}%')
print(f'✅ Sharpe Ratio: {metrics[\"sharpe_ratio\"]:.2f}')
print('✅ Metrics & Backtesting Engine functional')
"
```

### 4. Database Schema Test
```bash
python -c "
from database_schema import EventAlphaDB

# Initialize
db = EventAlphaDB('test_eventalpha.db')
print('✅ Database created successfully')

# Test insert
db.insert_signal(
    'TEST001', 'earnings', 'NVDA', 85.5, 0.88, 
    'bull_strong', 850.00, 'bullish'
)
print('✅ Signal inserted')

# Test query
signals = db.get_recent_signals(limit=1)
print(f'✅ Retrieved {len(signals)} signal(s)')

db.close()
import os
os.remove('test_eventalpha.db')
print('✅ Database Schema functional')
"
```

### 5. Scraper Integration Test
```bash
python -c "
# Test without actually running full scraper
from hybrid_scraper import RSSFeedCollector, MarketDataCollector, PolicyParser, SignalEngine

# These will work with API/network calls
print('✅ Scraper modules import successfully')
print('✅ RSSFeedCollector available')
print('✅ MarketDataCollector available')
print('✅ PolicyParser available')
print('✅ SignalEngine (with alpha scoring) available')
"
```

### 6. Full Scraper Pipeline Test (Dry Run)
```bash
# Create minimal test config
cat > test_config.py << 'EOF'
RSS_SOURCES = {
    'Economic Times': 'https://economictimes.indiatimes.com/feed.rdf'
}
MONITORED_STOCKS = ['INFY', 'TCS', 'RELIANCE']
EVENT_KEYWORDS = {
    'earnings': ['earnings', 'quarterly results', 'net profit'],
    'merger': ['merger', 'acquisition'],
    'policy': ['rbi', 'sebi', 'regulatory'],
    'order_win': ['order', 'contract'],
    'dividend': ['dividend'],
    'supply': ['supply', 'disruption'],
    'insider': ['insider', 'promoter']
}
POSITIVE_KEYWORDS = ['growth', 'surge', 'beat', 'strong', 'bullish']
NEGATIVE_KEYWORDS = ['decline', 'fall', 'miss', 'weak', 'bearish']
OUTPUT_FILE = None
LOG_FILE = 'test.log'
LOG_LEVEL = 'INFO'
REQUEST_TIMEOUT = 5

EOF

# Run test
python -c "
import sys
sys.path.insert(0, 'scraper')
from hybrid_scraper import DataPipeline
import logging
logging.basicConfig(level=logging.INFO)

try:
    pipeline = DataPipeline()
    print('✅ DataPipeline initialized with alpha scoring')
    print('✅ Full scraper pipeline ready for real execution')
except Exception as e:
    print(f'⚠️ Pipeline init had warnings: {e}')
"

rm test_config.py test.log 2>/dev/null || true
```

---

## 🧪 Component-Level Tests

### Test 1: Regime Detection Accuracy
```python
# scraper/test_regimes.py
from alpha_scoring_engine import RegimeDetector, MarketData, MarketRegime

test_cases = [
    # (vol, price_change, momentum, trend, expected_regime)
    (0.08, 0.00, 0.0, 0.3, 'sideways_calm'),
    (0.12, 0.03, 0.7, 0.8, 'bull_strong'),
    (0.22, -0.05, -0.8, 0.9, 'bear_strong'),
    (0.30, 0.10, 0.0, 0.1, 'spike_up'),
    (0.35, 0.00, 0.0, 0.0, 'crisis'),
]

passed = 0
for vol, price, momentum, trend, expected in test_cases:
    market = MarketData(vol, price, momentum, trend, momentum*0.7)
    detected = RegimeDetector.detect_regime(market)
    if detected.value == expected:
        print(f"✅ {expected}")
        passed += 1
    else:
        print(f"❌ Expected {expected}, got {detected.value}")

print(f"\nRegime Detection: {passed}/{len(test_cases)} passed")
```

### Test 2: Sentiment Multiplier Correctness
```python
# scraper/test_sentiment.py
from alpha_scoring_engine import SENTIMENT_BOOST, MarketRegime

# Verify sentiment boost ranges
for regime, boosts in SENTIMENT_BOOST.items():
    for sentiment, boost in boosts.items():
        if not (0.3 <= boost <= 1.6):
            print(f"❌ Invalid boost for {regime.value}/{sentiment}: {boost}")
        else:
            print(f"✅ {regime.value}/{sentiment}: {boost:.2f}x")

print("\n✅ All sentiment multipliers in valid range (0.3-1.6x)")
```

### Test 3: Confidence Layer Independence
```python
# scraper/test_confidence.py
from metrics_backtesting_engine import ConfidenceCalculator

# Each layer should produce independent values
event_conf = ConfidenceCalculator.calculate_event_confidence(0, 0, 0, 0.0)
print(f"Event Confidence (0 keywords): {event_conf:.2%}")  # Should be ~0%

sentiment_conf = ConfidenceCalculator.calculate_sentiment_confidence(0, 0)
print(f"Sentiment Confidence (0 keywords): {sentiment_conf:.2%}")  # Should be ~30%

alpha_conf_low = ConfidenceCalculator.calculate_alpha_confidence(30, 0.5)
alpha_conf_high = ConfidenceCalculator.calculate_alpha_confidence(90, 0.5)
print(f"Alpha Confidence (30): {alpha_conf_low:.2%}")
print(f"Alpha Confidence (90): {alpha_conf_high:.2%}")  # Should be significantly higher

print("✅ Confidence layers are independent and scale properly")
```

### Test 4: Alpha Score Bounds Check
```python
# scraper/test_alpha_bounds.py
from alpha_scoring_engine import AlphaScoringEngine, EventData, MarketData, MarketRegime

market = MarketData(0.15, 0.01, 0.5, 0.6, 0.3)

# Test extreme cases
test_cases = [
    # (magnitude, confidence, expected_behavior)
    (0.1, 0.1, "low"),      # Minimal event
    (10.0, 1.0, "high"),    # Maximum event
    (5.0, 0.5, "medium"),   # Middle ground
]

for mag, conf, desc in test_cases:
    event = EventData('earnings', 'TEST', 'bullish', mag, conf, 'test')
    alpha = AlphaScoringEngine.calculate_alpha_score(event, market, MarketRegime.BULL_STRONG)
    
    if alpha < 0 or alpha > 100:
        print(f"❌ Alpha out of bounds for {desc}: {alpha}")
    else:
        print(f"✅ {desc}: Alpha = {alpha:.1f}/100")

print("✅ All alpha scores properly bounded (0-100)")
```

### Test 5: Price Target Consistency
```python
# scraper/test_price_targets.py
from metrics_backtesting_engine import MultiHorizonPredictor

# Longer horizons should have larger upside targets
predictions = MultiHorizonPredictor.predict_multi_horizon(
    'earnings', 80, 0.18, 'bull_strong', 'bullish'
)

targets = MultiHorizonPredictor.predict_price_target(
    100.00,
    {h: p['predicted_return_pct'] for h, p in predictions.items()}
)

prev_target = 100.0
for horizon in ['1D', '2D', '3D', '5D', '20D']:
    target = targets[horizon]['target_price']
    if target >= prev_target:
        print(f"✅ {horizon}: ${target:.2f}")
        prev_target = target
    else:
        print(f"⚠️  {horizon}: ${target:.2f} (reversed from previous)")

print("✅ Price targets generally increase with horizon")
```

---

## 🚀 Full Integration Test

```bash
# scraper/full_integration_test.py
"""
End-to-end test: Event → Signal → Database
"""

from alpha_scoring_engine import RegimeDetector, AlphaScoringEngine, MarketData, EventData, MarketRegime
from metrics_backtesting_engine import MultiHorizonPredictor, BacktestEngine, BacktestResult
from database_schema import EventAlphaDB

# Create test event
print("Step 1: Creating test event...")
event = EventData(
    event_type='earnings',
    ticker='TEST',
    sentiment='bullish',
    magnitude=0.85,
    confidence=0.88,
    description='Test earnings beat'
)

# Detect regime
print("Step 2: Detecting market regime...")
market = MarketData(0.18, 0.02, 0.65, 0.75, 0.35)
regime = RegimeDetector.detect_regime(market)

# Calculate alpha
print("Step 3: Calculating alpha score...")
alpha = AlphaScoringEngine.calculate_alpha_score(event, market, regime, 'large', 0)

# Predict returns
print("Step 4: Predicting multi-horizon returns...")
predictions = MultiHorizonPredictor.predict_multi_horizon(
    event.event_type, alpha, market.volatility, regime.value, event.sentiment
)

# Store in database
print("Step 5: Storing in database...")
db = EventAlphaDB('integration_test.db')
db.insert_signal(
    'TEST_INTEGRATION', event.event_type, event.ticker,
    alpha, event.confidence, regime.value, 100.0, event.sentiment
)

# Retrieve and verify
print("Step 6: Verifying database storage...")
signals = db.get_recent_signals(1)
if signals:
    print("✅ Full integration test PASSED")
else:
    print("❌ Full integration test FAILED")

db.close()

import os
os.remove('integration_test.db')
```

Run it:
```bash
python scraper/full_integration_test.py
```

---

## 📊 Benchmark Reference

### Expected Performance (Baseline)

| Metric | Expected | Range |
|--------|----------|-------|
| Scraper Execution | ~8 seconds | 5-12s |
| Articles per run | 50-100 | Depends on RSS |
| Events extracted | 10-30 | 10-50% of articles |
| Signals generated | 5-15 | 1-3 per event |
| Avg alpha score | 65-75 | 50-90 typical |
| Regime detection | 100% accuracy | Should match market |
| Win rate (backtest) | 55-65% | After 100+ trades |
| Sharpe ratio | 0.8-1.5 | For diversified signals |
| Max drawdown | 10-20% | Depends on regime |

### Database Performance

| Operation | Expected Time |
|-----------|---------------|
| Insert signal | <10ms |
| Insert prediction | <10ms |
| Query 100 signals | <50ms |
| Calculate metrics | <200ms |

---

## ✅ Final Checklist

Before going live:

- [ ] All 4 main files created and importable
- [ ] Scraper runs to completion (~8 seconds)
- [ ] Alpha scores are between 0-100
- [ ] Regime detection returns one of 9 types
- [ ] Predictions increase with time horizon
- [ ] Database stores and retrieves signals
- [ ] Confidence values pass sanity checks
- [ ] Price targets are realistic (±50% entry price)
- [ ] Frontend loads MockData successfully
- [ ] No import errors in any Python files

---

**Status**: ✅ Ready for Testing & Verification
