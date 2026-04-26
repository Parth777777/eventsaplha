"""
EventAlpha Prediction Tracker
Checks expired predictions against actual market prices to validate accuracy.
Runs daily after NSE close (4 PM IST) via scheduler, or on-demand via API.
"""

import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

from database_schema import EventAlphaDB

logger = logging.getLogger(__name__)

HORIZON_DAYS = {'1D': 1, '3D': 3, '5D': 5, '20D': 20}


class PredictionTracker:
    """Validates predictions by comparing predicted returns to actual prices."""

    def __init__(self, db: EventAlphaDB):
        self.db = db

    def fetch_actual_price(self, ticker, target_date):
        """Get NSE closing price for ticker on or near target_date.
        Uses a 5-day window to handle weekends and holidays."""
        symbol = f"{ticker}.NS"
        try:
            start = (target_date - timedelta(days=2)).strftime('%Y-%m-%d')
            end = (target_date + timedelta(days=3)).strftime('%Y-%m-%d')
            data = yf.download(symbol, start=start, end=end, progress=False)
            if data.empty:
                return None
            # Get price closest to (but not after) target date
            data.index = pd.to_datetime(data.index)
            target_ts = pd.Timestamp(target_date)
            valid = data.index[data.index <= target_ts]
            if valid.empty:
                valid = data.index  # take whatever is available
            close_col = 'Close'
            if isinstance(data.columns, pd.MultiIndex):
                close_col = ('Close', symbol)
            price = data.loc[valid[-1], close_col]
            return float(price)
        except Exception as e:
            logger.warning(f"Failed to fetch price for {ticker} on {target_date}: {e}")
            return None

    def check_prediction(self, prediction):
        """Evaluate a single prediction. Returns dict with results or None."""
        ticker = prediction.get('ticker')
        entry_price = prediction.get('entry_price', 0)
        predicted_return = prediction.get('predicted_return_pct', 0)
        horizon = prediction.get('horizon', '1D')

        if not ticker or entry_price <= 0:
            return None

        # Calculate target date
        days = HORIZON_DAYS.get(horizon, 1)
        created_str = prediction.get('created_at', '')
        try:
            if isinstance(created_str, str):
                created = datetime.fromisoformat(created_str.replace('Z', ''))
            else:
                created = created_str
            target_date = created + timedelta(days=days)
        except Exception:
            return None

        # Don't check if target is in the future
        if target_date > datetime.now():
            return None

        actual_price = self.fetch_actual_price(ticker, target_date)
        if actual_price is None:
            return None

        actual_return = ((actual_price - entry_price) / entry_price) * 100

        # Hit = same direction (bullish prediction + positive return, or bearish + negative)
        if predicted_return >= 0:
            hit = actual_return > 0
        else:
            hit = actual_return < 0

        return {
            'prediction_id': prediction['id'],
            'actual_price': actual_price,
            'actual_return_pct': round(actual_return, 4),
            'hit_target': hit
        }

    def run(self):
        """Check all expired predictions. Returns summary dict."""
        logger.info("Prediction Tracker: checking expired predictions...")

        expired = self.db.get_expired_predictions(limit=100)
        if not expired:
            logger.info("No expired predictions to check")
            return {'checked': 0, 'updated': 0, 'hit': 0, 'miss': 0}

        logger.info(f"Found {len(expired)} expired predictions to verify")

        updated = 0
        hits = 0
        misses = 0

        for pred in expired:
            result = self.check_prediction(pred)
            if result:
                success = self.db.update_prediction_actual(
                    prediction_id=result['prediction_id'],
                    actual_price=result['actual_price'],
                    actual_return_pct=result['actual_return_pct'],
                    hit_target=result['hit_target']
                )
                if success:
                    updated += 1
                    if result['hit_target']:
                        hits += 1
                    else:
                        misses += 1

        summary = {
            'checked': len(expired),
            'updated': updated,
            'hit': hits,
            'miss': misses,
            'hit_rate': round(hits / max(updated, 1), 3)
        }
        logger.info(f"Prediction Tracker: {summary}")
        return summary


if __name__ == '__main__':
    from dotenv import load_dotenv
    import os
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

    db = EventAlphaDB()
    tracker = PredictionTracker(db)
    result = tracker.run()
    print(f"Results: {result}")
    db.close()
