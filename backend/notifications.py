"""
Tickwave Notification System
Discord webhook + Telegram bot integration for trading signal alerts
"""

import os
import sys
import logging
import requests
from datetime import datetime, timedelta

# Add scraper to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
from database_schema import TickwaveDB

logger = logging.getLogger(__name__)


# ============ DISCORD ============

def send_discord_alert(webhook_url: str, signal: dict) -> bool:
    """Send a trading signal alert to Discord via webhook"""
    if not webhook_url:
        return False

    try:
        # Color based on sentiment
        color_map = {'bullish': 0x4EDE8A, 'bearish': 0xFFB2B7, 'neutral': 0xADC6FF}
        color = color_map.get(signal.get('sentiment', 'neutral'), 0xADC6FF)

        sentiment_emoji = {'bullish': '🟢', 'bearish': '🔴', 'neutral': '⚪'}.get(
            signal.get('sentiment', ''), '⚪')

        # Build prediction text
        predictions = signal.get('predictions', {})
        pred_lines = []
        for horizon in ['1D', '3D', '20D']:
            p = predictions.get(horizon, {})
            ret = p.get('return_pct', 0)
            sign = '+' if ret >= 0 else ''
            pred_lines.append(f"**{horizon}**: {sign}{ret:.2f}%")

        embed = {
            "title": f"{sentiment_emoji} {signal.get('ticker', '?')} — Alpha {signal.get('alpha_score', 0):.0f}",
            "description": signal.get('headline', signal.get('event_type', '')),
            "color": color,
            "fields": [
                {"name": "Event Type", "value": signal.get('event_type', 'N/A').replace('_', ' ').title(), "inline": True},
                {"name": "Sentiment", "value": signal.get('sentiment', 'N/A').title(), "inline": True},
                {"name": "Confidence", "value": f"{signal.get('confidence', 0):.0%}", "inline": True},
                {"name": "Entry Price", "value": f"₹{signal.get('entry_price', 0):,.2f}", "inline": True},
                {"name": "Regime", "value": signal.get('regime', 'N/A').replace('_', ' ').title(), "inline": True},
                {"name": "Impact", "value": f"{signal.get('impact_score', 0)}/100", "inline": True},
                {"name": "Predicted Returns", "value": "\n".join(pred_lines) if pred_lines else "N/A", "inline": False},
            ],
            "footer": {"text": f"Tickwave Signal Engine • {datetime.now().strftime('%H:%M IST')}"},
            "timestamp": datetime.utcnow().isoformat()
        }

        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)

        if resp.status_code in (200, 204):
            logger.info(f"Discord alert sent for {signal.get('ticker')}")
            return True
        else:
            logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
            return False

    except Exception as e:
        logger.error(f"Discord alert failed: {e}")
        return False


# ============ TELEGRAM ============

def send_telegram_alert(bot_token: str, chat_id: str, signal: dict, app_url: str = '') -> bool:
    """Send a trading signal alert to Telegram"""
    if not bot_token or not chat_id:
        return False

    try:
        sentiment_emoji = {'bullish': '🟢', 'bearish': '🔴', 'neutral': '⚪'}.get(
            signal.get('sentiment', ''), '⚪')

        predictions = signal.get('predictions', {})
        pred_lines = []
        for horizon in ['1D', '3D', '20D']:
            p = predictions.get(horizon, {})
            ret = p.get('return_pct', 0)
            sign = '+' if ret >= 0 else ''
            pred_lines.append(f"  {horizon}: {sign}{ret:.2f}%")

        message = (
            f"{sentiment_emoji} <b>{signal.get('ticker', '?')}</b> — "
            f"Alpha <b>{signal.get('alpha_score', 0):.0f}</b>\n\n"
            f"<i>{signal.get('headline', signal.get('event_type', ''))}</i>\n\n"
            f"📊 <b>Event:</b> {signal.get('event_type', 'N/A').replace('_', ' ').title()}\n"
            f"💡 <b>Sentiment:</b> {signal.get('sentiment', 'N/A').title()}\n"
            f"🎯 <b>Confidence:</b> {signal.get('confidence', 0):.0%}\n"
            f"💰 <b>Entry:</b> ₹{signal.get('entry_price', 0):,.2f}\n"
            f"📈 <b>Regime:</b> {signal.get('regime', 'N/A').replace('_', ' ').title()}\n\n"
            f"<b>Predicted Returns:</b>\n"
            f"{'chr(10)'.join(pred_lines) if pred_lines else 'N/A'}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M IST')}"
        )

        # Fix the chr(10) join (can't use \n in f-string)
        pred_text = "\n".join(pred_lines) if pred_lines else "N/A"
        message = (
            f"{sentiment_emoji} <b>{signal.get('ticker', '?')}</b> — "
            f"Alpha <b>{signal.get('alpha_score', 0):.0f}</b>\n\n"
            f"<i>{signal.get('headline', signal.get('event_type', ''))}</i>\n\n"
            f"Event: {signal.get('event_type', 'N/A').replace('_', ' ').title()}\n"
            f"Sentiment: {signal.get('sentiment', 'N/A').title()}\n"
            f"Confidence: {signal.get('confidence', 0):.0%}\n"
            f"Entry: ₹{signal.get('entry_price', 0):,.2f}\n"
            f"Regime: {signal.get('regime', 'N/A').replace('_', ' ').title()}\n\n"
            f"<b>Predictions:</b>\n{pred_text}\n\n"
            f"<i>Tickwave • {datetime.now().strftime('%H:%M IST')}</i>"
        )

        if app_url:
            message += f"\n\n<a href='{app_url}'>Open Tickwave</a>"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)

        if resp.status_code == 200 and resp.json().get('ok'):
            logger.info(f"Telegram alert sent for {signal.get('ticker')}")
            return True
        else:
            logger.warning(f"Telegram API returned: {resp.text[:200]}")
            return False

    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
        return False


# ============ ALERT FILTERING ============

def should_notify(signal: dict, config: dict) -> bool:
    """Check if a signal should trigger a notification based on user config"""
    if not config or not config.get('enabled', True):
        return False

    # Check minimum alpha score
    min_alpha = config.get('min_alpha_score', 70)
    if signal.get('alpha_score', 0) < min_alpha:
        return False

    # Check minimum confidence
    min_conf = config.get('min_confidence', 0.7)
    if signal.get('confidence', 0) < min_conf:
        return False

    # Check event type filter
    allowed_types = config.get('event_types', 'all')
    if allowed_types and allowed_types != 'all':
        types_list = [t.strip() for t in allowed_types.split(',')]
        if signal.get('event_type', '') not in types_list:
            return False

    # Check sentiment filter
    sentiment_filter = config.get('sentiment_filter', 'all')
    if sentiment_filter and sentiment_filter != 'all':
        if signal.get('sentiment', '') != sentiment_filter:
            return False

    return True


def check_cooldown(db: TickwaveDB, ticker: str, channel: str, cooldown_minutes: int) -> bool:
    """Check if enough time has passed since last notification for this ticker"""
    last_sent = db.get_last_notification_for_ticker(ticker, channel)
    if last_sent is None:
        return True

    try:
        if isinstance(last_sent, str):
            last_time = datetime.fromisoformat(last_sent.replace('Z', '+00:00').replace('+00:00', ''))
        else:
            last_time = last_sent
        elapsed = (datetime.now() - last_time).total_seconds() / 60
        return elapsed >= cooldown_minutes
    except Exception:
        return True


# ============ PROCESS NEW SIGNALS ============

def process_signal_notifications(signals: list, db: TickwaveDB, app_url: str = ''):
    """Process a batch of new signals and send notifications where appropriate"""
    config = db.get_notification_config()
    if not config:
        logger.info("No notification config found, skipping notifications")
        return

    if not config.get('enabled', True):
        return

    discord_url = config.get('discord_webhook_url', '')
    tg_token = config.get('telegram_bot_token', '')
    tg_chat = config.get('telegram_chat_id', '')
    cooldown = config.get('cooldown_minutes', 30)

    if not discord_url and not tg_token:
        return

    sent_count = 0
    for signal in signals:
        if not should_notify(signal, config):
            continue

        ticker = signal.get('ticker', '')

        # Discord
        if discord_url and check_cooldown(db, ticker, 'discord', cooldown):
            success = send_discord_alert(discord_url, signal)
            db.log_notification(
                signal_event_id=signal.get('event_id', ''),
                channel='discord',
                ticker=ticker,
                alpha_score=signal.get('alpha_score', 0),
                status='sent' if success else 'failed',
                error_message=None if success else 'webhook_error'
            )
            if success:
                sent_count += 1

        # Telegram
        if tg_token and tg_chat and check_cooldown(db, ticker, 'telegram', cooldown):
            success = send_telegram_alert(tg_token, tg_chat, signal, app_url)
            db.log_notification(
                signal_event_id=signal.get('event_id', ''),
                channel='telegram',
                ticker=ticker,
                alpha_score=signal.get('alpha_score', 0),
                status='sent' if success else 'failed',
                error_message=None if success else 'telegram_error'
            )
            if success:
                sent_count += 1

    if sent_count > 0:
        logger.info(f"Sent {sent_count} notifications")


def send_test_notification(db: TickwaveDB, app_url: str = '', user_id: str = 'legacy') -> dict:
    """Send a test notification to verify Discord/Telegram setup"""
    config = db.get_notification_config(user_id=user_id)
    if not config:
        return {'success': False, 'error': 'No notification config saved'}

    test_signal = {
        'ticker': 'TEST',
        'company': 'Test Signal',
        'event_type': 'earnings',
        'event_id': 'test_signal',
        'alpha_score': 85.0,
        'confidence': 0.88,
        'regime': 'bull_strong',
        'sentiment': 'bullish',
        'magnitude': 8,
        'impact_score': 80,
        'entry_price': 1000.00,
        'headline': 'This is a test notification from Tickwave',
        'predictions': {
            '1D': {'return_pct': 1.5, 'confidence': 0.6},
            '3D': {'return_pct': 4.2, 'confidence': 0.55},
            '20D': {'return_pct': 12.8, 'confidence': 0.45}
        }
    }

    results = {}

    discord_url = config.get('discord_webhook_url', '')
    if discord_url:
        results['discord'] = send_discord_alert(discord_url, test_signal)

    tg_token = config.get('telegram_bot_token', '')
    tg_chat = config.get('telegram_chat_id', '')
    if tg_token and tg_chat:
        results['telegram'] = send_telegram_alert(tg_token, tg_chat, test_signal, app_url)

    if not results:
        return {'success': False, 'error': 'No Discord or Telegram configured'}

    return {
        'success': any(results.values()),
        'results': results
    }
