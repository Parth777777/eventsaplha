"""Notification retry queue — wraps notifications.py senders so failed
Discord/Telegram deliveries don't drop on the floor.

Schema (own sqlite db so it doesn't depend on main signal store):
    notification_retry(
        id              integer pk,
        channel         text,           -- 'discord' | 'telegram'
        recipient       text,           -- webhook_url for discord, chat_id for telegram
        payload         text,           -- JSON of the signal dict
        signal_event_id text,
        ticker          text,
        attempts        int default 0,
        max_attempts    int default 6,
        last_error      text,
        next_retry_at   timestamp,
        sent_at         timestamp,
        status          text default 'pending',   -- pending | sent | failed_permanent
        created_at      timestamp default current_timestamp
    )

Retry policy: exponential backoff — 30s, 2m, 8m, 30m, 2h, 6h then permanent fail.

Exposes:
    enqueue(channel, recipient, signal, event_id, ticker, error)
    try_send(channel, recipient, signal) -> (ok: bool, error: str|None)
        Wraps notifications.send_* and returns structured result.
    process_pending(limit=20) -> {attempted, sent, failed, permanent}
        Worker entry point — call every 60 s from APScheduler.
    queue_status() -> dict with counts + last error sample
"""
import os, json, sqlite3, threading, logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'notifications.db')
_LOCK = threading.Lock()

# Exponential backoff (seconds) for attempts 1..6
BACKOFF_SECONDS = [30, 120, 480, 1800, 7200, 21600]
MAX_ATTEMPTS = len(BACKOFF_SECONDS)


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _LOCK:
        c = _conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS notification_retry (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel         TEXT NOT NULL,
                recipient       TEXT NOT NULL,
                payload         TEXT NOT NULL,
                signal_event_id TEXT,
                ticker          TEXT,
                attempts        INTEGER NOT NULL DEFAULT 0,
                max_attempts    INTEGER NOT NULL DEFAULT 6,
                last_error      TEXT,
                next_retry_at   TIMESTAMP,
                sent_at         TIMESTAMP,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_nr_status_next ON notification_retry(status, next_retry_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_nr_event ON notification_retry(signal_event_id)")
        c.commit()
        c.close()


def enqueue(channel: str, recipient: str, signal: dict, error: str):
    """Add a failed send to the retry queue with backoff schedule."""
    next_retry = datetime.utcnow() + timedelta(seconds=BACKOFF_SECONDS[0])
    with _LOCK:
        c = _conn()
        c.execute("""
            INSERT INTO notification_retry
                (channel, recipient, payload, signal_event_id, ticker,
                 attempts, max_attempts, last_error, next_retry_at, status)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'pending')
        """, (
            channel, recipient,
            json.dumps(signal, default=str),
            signal.get('event_id', ''),
            signal.get('ticker', ''),
            MAX_ATTEMPTS,
            error[:500],
            next_retry.isoformat(),
        ))
        c.commit()
        c.close()
    logger.info(f"notification_retry: enqueued {channel} for {signal.get('ticker')} (attempt 1/{MAX_ATTEMPTS})")


def _do_send(channel: str, recipient: str, signal: dict):
    """Single send attempt — returns (ok, error|None)."""
    try:
        from notifications import send_discord_alert, send_telegram_alert
        if channel == 'discord':
            ok = send_discord_alert(recipient, signal)
            return (ok, None if ok else 'discord webhook returned non-2xx')
        if channel == 'telegram':
            # recipient encoded as "bot_token::chat_id"
            try:
                token, chat = recipient.split('::', 1)
            except ValueError:
                return (False, 'telegram recipient must be bot_token::chat_id')
            ok = send_telegram_alert(token, chat, signal)
            return (ok, None if ok else 'telegram API returned non-OK')
        return (False, f'unknown channel: {channel}')
    except Exception as e:
        return (False, str(e)[:500])


def try_send(channel: str, recipient: str, signal: dict):
    """First-try entry point. On failure, returns (False, err) AND enqueues for retry."""
    ok, err = _do_send(channel, recipient, signal)
    if not ok:
        try:
            enqueue(channel, recipient, signal, err or 'unknown')
        except Exception as e:
            logger.warning(f"notification_retry: enqueue failed: {e}")
    return (ok, err)


def process_pending(limit: int = 20) -> dict:
    """Drain due-for-retry items. Called every 60 s by the scheduler."""
    now = datetime.utcnow().isoformat()
    stats = {'attempted': 0, 'sent': 0, 'failed': 0, 'permanent': 0}
    with _LOCK:
        c = _conn()
        rows = c.execute("""
            SELECT * FROM notification_retry
             WHERE status = 'pending' AND next_retry_at <= ?
             ORDER BY next_retry_at ASC
             LIMIT ?
        """, (now, limit)).fetchall()
        # Mark them attempted up front so a concurrent worker doesn't double-send
        ids = [r['id'] for r in rows]
        if ids:
            qmarks = ','.join('?' * len(ids))
            c.execute(f"UPDATE notification_retry SET status='in_flight' WHERE id IN ({qmarks})", ids)
            c.commit()
        c.close()

    for row in rows:
        stats['attempted'] += 1
        signal = json.loads(row['payload'])
        ok, err = _do_send(row['channel'], row['recipient'], signal)
        with _LOCK:
            c = _conn()
            if ok:
                stats['sent'] += 1
                c.execute("UPDATE notification_retry SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
                          (datetime.utcnow().isoformat(), row['id']))
            else:
                attempts = (row['attempts'] or 0) + 1
                if attempts >= (row['max_attempts'] or MAX_ATTEMPTS):
                    stats['permanent'] += 1
                    c.execute("UPDATE notification_retry SET status='failed_permanent', attempts=?, last_error=? WHERE id=?",
                              (attempts, (err or '')[:500], row['id']))
                else:
                    stats['failed'] += 1
                    backoff = BACKOFF_SECONDS[min(attempts, MAX_ATTEMPTS - 1)]
                    next_retry = (datetime.utcnow() + timedelta(seconds=backoff)).isoformat()
                    c.execute("""UPDATE notification_retry
                                    SET status='pending', attempts=?, last_error=?,
                                        next_retry_at=?
                                  WHERE id=?""",
                              (attempts, (err or '')[:500], next_retry, row['id']))
            c.commit()
            c.close()
    if stats['attempted']:
        logger.info(f"notification_retry: drained — {stats}")
    return stats


def queue_status() -> dict:
    with _LOCK:
        c = _conn()
        pending   = c.execute("SELECT COUNT(*) AS n FROM notification_retry WHERE status='pending'").fetchone()['n']
        in_flight = c.execute("SELECT COUNT(*) AS n FROM notification_retry WHERE status='in_flight'").fetchone()['n']
        sent      = c.execute("SELECT COUNT(*) AS n FROM notification_retry WHERE status='sent'").fetchone()['n']
        perm      = c.execute("SELECT COUNT(*) AS n FROM notification_retry WHERE status='failed_permanent'").fetchone()['n']
        next_one  = c.execute("""SELECT channel, ticker, next_retry_at, attempts, last_error
                                   FROM notification_retry
                                  WHERE status='pending'
                                  ORDER BY next_retry_at ASC LIMIT 1""").fetchone()
        recent_fail = c.execute("""SELECT channel, ticker, last_error, attempts
                                     FROM notification_retry
                                    WHERE status='failed_permanent'
                                    ORDER BY id DESC LIMIT 3""").fetchall()
        c.close()
    return {
        'pending':   pending,
        'in_flight': in_flight,
        'sent':      sent,
        'failed_permanent': perm,
        'next_due':  dict(next_one) if next_one else None,
        'recent_failures': [dict(r) for r in recent_fail],
    }


# Init on import so the table is ready before the first send.
init()
