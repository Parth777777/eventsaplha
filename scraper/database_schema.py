"""
EventAlpha Database Schema
Supports PostgreSQL (production via Supabase) and SQLite (local development)
"""

import os
import logging
import hashlib
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# ============ POSTGRESQL SCHEMA ============
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(200) UNIQUE NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    company VARCHAR(200),
    alpha_score FLOAT NOT NULL,
    confidence FLOAT NOT NULL,
    regime VARCHAR(50) NOT NULL,
    regime_strength FLOAT DEFAULT 0,
    entry_price FLOAT NOT NULL,
    sentiment VARCHAR(20) NOT NULL,
    magnitude FLOAT DEFAULT 0,
    impact_score FLOAT DEFAULT 0,
    source VARCHAR(100),
    headline TEXT,
    link TEXT,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_alpha_score ON signals(alpha_score);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_event_type ON signals(event_type);

CREATE TABLE IF NOT EXISTS predictions (
    id SERIAL PRIMARY KEY,
    signal_id VARCHAR(200) NOT NULL,
    event_id VARCHAR(200) NOT NULL REFERENCES signals(event_id) ON DELETE CASCADE,
    horizon VARCHAR(5) NOT NULL,
    predicted_return_pct FLOAT NOT NULL,
    target_price FLOAT NOT NULL,
    confidence FLOAT DEFAULT 0,
    actual_price FLOAT,
    actual_return_pct FLOAT,
    exit_timestamp TIMESTAMP,
    hit_target BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_predictions_signal_id ON predictions(signal_id);
CREATE INDEX IF NOT EXISTS idx_predictions_event_id ON predictions(event_id);
CREATE INDEX IF NOT EXISTS idx_predictions_horizon ON predictions(horizon);

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(200) UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source VARCHAR(100),
    link TEXT,
    event_type VARCHAR(50),
    event_confidence FLOAT DEFAULT 0,
    sentiment VARCHAR(20),
    sentiment_confidence FLOAT DEFAULT 0,
    magnitude FLOAT DEFAULT 0,
    impact_score FLOAT DEFAULT 0,
    companies TEXT,
    published_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_sentiment ON events(sentiment);

CREATE TABLE IF NOT EXISTS backtest_results (
    id SERIAL PRIMARY KEY,
    signal_id VARCHAR(200) NOT NULL,
    event_id VARCHAR(200) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    alpha_score FLOAT NOT NULL,
    entry_price FLOAT NOT NULL,
    exit_price FLOAT NOT NULL,
    entry_timestamp TIMESTAMP NOT NULL,
    exit_timestamp TIMESTAMP NOT NULL,
    holding_days INTEGER NOT NULL,
    gross_return_pct FLOAT NOT NULL,
    net_return_pct FLOAT NOT NULL,
    fees_pct FLOAT DEFAULT 0.1,
    win BOOLEAN NOT NULL,
    prediction_accuracy_pct FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_backtest_alpha ON backtest_results(alpha_score);
CREATE INDEX IF NOT EXISTS idx_backtest_event_type ON backtest_results(event_type);
CREATE INDEX IF NOT EXISTS idx_backtest_win ON backtest_results(win);

CREATE TABLE IF NOT EXISTS performance_summary (
    id SERIAL PRIMARY KEY,
    alpha_bucket VARCHAR(20) NOT NULL,
    total_signals INTEGER NOT NULL,
    winning_signals INTEGER NOT NULL,
    accuracy_pct FLOAT NOT NULL,
    avg_return_pct FLOAT NOT NULL,
    sharpe_ratio FLOAT NOT NULL,
    max_drawdown_pct FLOAT NOT NULL,
    profit_factor FLOAT NOT NULL,
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_regimes (
    id SERIAL PRIMARY KEY,
    regime_date DATE NOT NULL,
    regime_type VARCHAR(50) NOT NULL,
    volatility FLOAT NOT NULL,
    momentum FLOAT NOT NULL,
    trend_strength FLOAT NOT NULL,
    price_change_1d FLOAT NOT NULL,
    regime_strength FLOAT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_regimes_date ON market_regimes(regime_date);

CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(200) DEFAULT 'legacy',
    ticker VARCHAR(20) NOT NULL,
    alert_type VARCHAR(50) NOT NULL,
    condition VARCHAR(20) NOT NULL,
    threshold FLOAT NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    triggered_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(200) DEFAULT 'legacy',
    ticker VARCHAR(20) NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);

CREATE TABLE IF NOT EXISTS notification_config (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(200) DEFAULT 'legacy' UNIQUE,
    discord_webhook_url TEXT,
    telegram_bot_token TEXT,
    telegram_chat_id TEXT,
    min_alpha_score FLOAT DEFAULT 70,
    min_confidence FLOAT DEFAULT 0.7,
    event_types TEXT DEFAULT 'all',
    sentiment_filter VARCHAR(20) DEFAULT 'all',
    cooldown_minutes INTEGER DEFAULT 30,
    enabled BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_log (
    id SERIAL PRIMARY KEY,
    signal_event_id VARCHAR(200),
    channel VARCHAR(20) NOT NULL,
    ticker VARCHAR(20),
    alpha_score FLOAT,
    status VARCHAR(20) NOT NULL,
    error_message TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notif_log_sent ON notification_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_notif_log_ticker ON notification_log(ticker);

CREATE TABLE IF NOT EXISTS scraped_articles (
    id SERIAL PRIMARY KEY,
    url_hash VARCHAR(64) UNIQUE NOT NULL,
    title_hash VARCHAR(64) NOT NULL,
    source VARCHAR(100),
    url TEXT,
    title TEXT,
    summary TEXT,
    llm_verified BOOLEAN DEFAULT FALSE,
    llm_result TEXT,
    llm_verified_at TIMESTAMP,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_articles_url_hash ON scraped_articles(url_hash);
CREATE INDEX IF NOT EXISTS idx_articles_llm_pending ON scraped_articles(llm_verified);

CREATE TABLE IF NOT EXISTS user_profiles (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(200) UNIQUE NOT NULL,
    email VARCHAR(200),
    display_name VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS geo_events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(200) UNIQUE,
    country VARCHAR(100) NOT NULL,
    event_description TEXT NOT NULL,
    impact VARCHAR(20) DEFAULT 'medium',
    latitude FLOAT,
    longitude FLOAT,
    related_assets TEXT,
    source VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_geo_country ON geo_events(country);
"""

# ============ SQLITE SCHEMA ============
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT,
    alpha_score REAL NOT NULL,
    confidence REAL NOT NULL,
    regime TEXT NOT NULL,
    regime_strength REAL DEFAULT 0,
    entry_price REAL NOT NULL,
    sentiment TEXT NOT NULL,
    magnitude REAL DEFAULT 0,
    impact_score REAL DEFAULT 0,
    source TEXT,
    headline TEXT,
    link TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_alpha_score ON signals(alpha_score);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_event_type ON signals(event_type);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    predicted_return_pct REAL NOT NULL,
    target_price REAL NOT NULL,
    confidence REAL DEFAULT 0,
    actual_price REAL,
    actual_return_pct REAL,
    exit_timestamp TIMESTAMP,
    hit_target INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(event_id) REFERENCES signals(event_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_signal_id ON predictions(signal_id);
CREATE INDEX IF NOT EXISTS idx_predictions_event_id ON predictions(event_id);
CREATE INDEX IF NOT EXISTS idx_predictions_horizon ON predictions(horizon);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    link TEXT,
    event_type TEXT,
    event_confidence REAL DEFAULT 0,
    sentiment TEXT,
    sentiment_confidence REAL DEFAULT 0,
    magnitude REAL DEFAULT 0,
    impact_score REAL DEFAULT 0,
    companies TEXT,
    published_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    alpha_score REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_timestamp TIMESTAMP NOT NULL,
    exit_timestamp TIMESTAMP NOT NULL,
    holding_days INTEGER NOT NULL,
    gross_return_pct REAL NOT NULL,
    net_return_pct REAL NOT NULL,
    fees_pct REAL DEFAULT 0.1,
    win INTEGER NOT NULL,
    prediction_accuracy_pct REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(event_id) REFERENCES signals(event_id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_alpha ON backtest_results(alpha_score);
CREATE INDEX IF NOT EXISTS idx_backtest_event_type ON backtest_results(event_type);
CREATE INDEX IF NOT EXISTS idx_backtest_win ON backtest_results(win);

CREATE TABLE IF NOT EXISTS performance_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alpha_bucket TEXT NOT NULL,
    total_signals INTEGER NOT NULL,
    winning_signals INTEGER NOT NULL,
    accuracy_pct REAL NOT NULL,
    avg_return_pct REAL NOT NULL,
    sharpe_ratio REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    profit_factor REAL NOT NULL,
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    regime_date DATE NOT NULL,
    regime_type TEXT NOT NULL,
    volatility REAL NOT NULL,
    momentum REAL NOT NULL,
    trend_strength REAL NOT NULL,
    price_change_1d REAL NOT NULL,
    regime_strength REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_regimes_date ON market_regimes(regime_date);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'legacy',
    ticker TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    condition TEXT NOT NULL,
    threshold REAL NOT NULL,
    active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    triggered_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'legacy',
    ticker TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);

CREATE TABLE IF NOT EXISTS notification_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'legacy' UNIQUE,
    discord_webhook_url TEXT,
    telegram_bot_token TEXT,
    telegram_chat_id TEXT,
    min_alpha_score REAL DEFAULT 70,
    min_confidence REAL DEFAULT 0.7,
    event_types TEXT DEFAULT 'all',
    sentiment_filter TEXT DEFAULT 'all',
    cooldown_minutes INTEGER DEFAULT 30,
    enabled INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_event_id TEXT,
    channel TEXT NOT NULL,
    ticker TEXT,
    alpha_score REAL,
    status TEXT NOT NULL,
    error_message TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notif_log_sent ON notification_log(sent_at);

CREATE TABLE IF NOT EXISTS scraped_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT UNIQUE NOT NULL,
    title_hash TEXT NOT NULL,
    source TEXT,
    url TEXT,
    title TEXT,
    summary TEXT,
    llm_verified INTEGER DEFAULT 0,
    llm_result TEXT,
    llm_verified_at TIMESTAMP,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_articles_url_hash ON scraped_articles(url_hash);
CREATE INDEX IF NOT EXISTS idx_articles_llm_pending ON scraped_articles(llm_verified);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS geo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE,
    country TEXT NOT NULL,
    event_description TEXT NOT NULL,
    impact TEXT DEFAULT 'medium',
    latitude REAL,
    longitude REAL,
    related_assets TEXT,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_geo_country ON geo_events(country);
"""


# ============ DATABASE HELPER ============

def _is_postgres():
    """Check if PostgreSQL is configured"""
    return bool(os.environ.get('DATABASE_URL', '').startswith('postgres'))


def get_connection():
    """Get database connection (PostgreSQL or SQLite)"""
    db_url = os.environ.get('DATABASE_URL', '')

    if db_url.startswith('postgres'):
        import psycopg2
        import psycopg2.extras
        # Supabase uses postgresql:// but psycopg2 needs postgresql://
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        return conn
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'eventalpha.db')
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # check_same_thread=False required for Flask's threaded dev server —
        # we serialize access via the single EventAlphaDB singleton.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def _execute_query(conn, query, params=None, fetch=False):
    """Execute a query, handling differences between PostgreSQL and SQLite"""
    cursor = conn.cursor()
    if params:
        if _is_postgres():
            # Convert ? placeholders to %s for psycopg2
            query = query.replace('?', '%s')
        cursor.execute(query, params)
    else:
        cursor.execute(query)

    if fetch:
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        if _is_postgres():
            return [dict(zip(columns, row)) for row in rows]
        else:
            return [dict(row) for row in rows]
    return cursor


class EventAlphaDB:
    """Database helper for EventAlpha - supports PostgreSQL and SQLite"""

    def __init__(self):
        self.is_postgres = _is_postgres()
        self.conn = None
        self._connect()

    def _connect(self):
        """Establish database connection"""
        try:
            self.conn = get_connection()
            logger.info(f"Database connected ({'PostgreSQL' if self.is_postgres else 'SQLite'})")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def _ensure_connected(self):
        """Reconnect if connection is lost"""
        try:
            if self.is_postgres:
                self.conn.cursor().execute("SELECT 1")
            else:
                self.conn.cursor().execute("SELECT 1")
        except Exception:
            logger.warning("Reconnecting to database...")
            self._connect()

    def init_schema(self):
        """Create all tables"""
        try:
            schema = PG_SCHEMA if self.is_postgres else SQLITE_SCHEMA
            cursor = self.conn.cursor()
            for statement in schema.split(';'):
                stmt = statement.strip()
                if stmt:
                    cursor.execute(stmt)
            self.conn.commit()
            logger.info("Database schema initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Schema initialization failed: {e}")
            self.conn.rollback()
            return False

    # ---- Signal Operations ----

    def upsert_signal(self, event_id, event_type, ticker, alpha_score, confidence,
                      regime, entry_price, sentiment, company=None, regime_strength=0,
                      magnitude=0, impact_score=0, source=None, headline=None, link=None):
        """Insert or update a signal (deduplication by event_id)"""
        self._ensure_connected()
        try:
            if self.is_postgres:
                self.conn.cursor().execute("""
                    INSERT INTO signals (event_id, event_type, ticker, company, alpha_score,
                        confidence, regime, regime_strength, entry_price, sentiment,
                        magnitude, impact_score, source, headline, link, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                    ON CONFLICT (event_id) DO UPDATE SET
                        alpha_score = EXCLUDED.alpha_score,
                        confidence = EXCLUDED.confidence,
                        regime = EXCLUDED.regime,
                        status = 'active',
                        created_at = CURRENT_TIMESTAMP
                """, (event_id, event_type, ticker, company, alpha_score, confidence,
                      regime, regime_strength, entry_price, sentiment, magnitude,
                      impact_score, source, headline, link))
            else:
                self.conn.execute("""
                    INSERT OR REPLACE INTO signals (event_id, event_type, ticker, company,
                        alpha_score, confidence, regime, regime_strength, entry_price,
                        sentiment, magnitude, impact_score, source, headline, link, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """, (event_id, event_type, ticker, company, alpha_score, confidence,
                      regime, regime_strength, entry_price, sentiment, magnitude,
                      impact_score, source, headline, link))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to upsert signal {event_id}: {e}")
            self.conn.rollback()
            return False

    def recent_signal_exists(self, ticker, event_type, sentiment, hours=24):
        """Return True if an active signal for the same (ticker, event_type, sentiment)
        was created within the cooldown window. Used as a dedup gate before insert.
        """
        self._ensure_connected()
        try:
            cur = self.conn.cursor()
            if self.is_postgres:
                cur.execute(
                    """SELECT 1 FROM signals
                        WHERE ticker = %s AND event_type = %s AND sentiment = %s
                          AND status = 'active'
                          AND created_at >= NOW() - (%s || ' hours')::interval
                        LIMIT 1""",
                    (ticker, event_type, sentiment, str(hours)),
                )
            else:
                cur.execute(
                    f"""SELECT 1 FROM signals
                         WHERE ticker = ? AND event_type = ? AND sentiment = ?
                           AND status = 'active'
                           AND created_at >= datetime('now', '-{int(hours)} hours')
                         LIMIT 1""",
                    (ticker, event_type, sentiment),
                )
            return cur.fetchone() is not None
        except Exception as e:
            logger.warning(f"recent_signal_exists check failed: {e}")
            return False

    def expire_stale_signals(self, max_age_days=7):
        """Mark signals older than max_age_days as 'expired'.

        Their predicted horizons are at most 5D, plus a 2-day buffer for the
        prediction tracker to resolve. Returns count expired.
        """
        self._ensure_connected()
        try:
            cur = self.conn.cursor()
            if self.is_postgres:
                cur.execute(
                    """UPDATE signals SET status = 'expired'
                        WHERE status = 'active'
                          AND created_at < NOW() - (%s || ' days')::interval""",
                    (str(max_age_days),),
                )
            else:
                cur.execute(
                    f"""UPDATE signals SET status = 'expired'
                         WHERE status = 'active'
                           AND created_at < datetime('now', '-{int(max_age_days)} days')""",
                )
            n = cur.rowcount
            self.conn.commit()
            return n
        except Exception as e:
            logger.warning(f"expire_stale_signals failed: {e}")
            self.conn.rollback()
            return 0

    # ---- Phase 1.5 Extension Writers ----
    EXTENDED_SIGNAL_COLUMNS = {
        'intent', 'fingerprint_flags', 'volume_confirmation', 'volume_multiplier',
        'obv_divergence_flag', 'article_discrepancy_flag', 'manipulation_score',
        'first_seen_source', 'priority_boost', 'edge_minutes', 'coordinated_campaign',
        'news_type', 'reasoning',
    }

    def update_signal_extensions(self, event_id, fields):
        """Update Phase 1.5 extended columns on an existing signal.

        `fields` is a dict of column→value; only columns in EXTENDED_SIGNAL_COLUMNS
        are accepted. Booleans are coerced to 0/1 for SQLite. JSON-ables are dumped.
        """
        if not fields or not event_id:
            return False
        import json as _json
        clean = {}
        for k, v in fields.items():
            if k not in self.EXTENDED_SIGNAL_COLUMNS:
                continue
            if v is None:
                continue
            if isinstance(v, bool):
                clean[k] = 1 if v else 0
            elif isinstance(v, (list, dict)):
                clean[k] = _json.dumps(v, default=str)
            else:
                clean[k] = v
        if not clean:
            return False
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            set_clause = ', '.join(f"{k} = {placeholder}" for k in clean.keys())
            values = list(clean.values()) + [event_id]
            self.conn.cursor().execute(
                f"UPDATE signals SET {set_clause} WHERE event_id = {placeholder}",
                values,
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.warning(f"update_signal_extensions failed event={event_id}: {e}")
            self.conn.rollback()
            return False

    def update_event_news_type(self, event_id, news_type):
        """Set events.news_type on an already-inserted row."""
        if not event_id or not news_type:
            return
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(
                f"UPDATE events SET news_type = {placeholder} WHERE event_id = {placeholder}",
                (news_type, event_id),
            )
            self.conn.commit()
        except Exception as e:
            logger.debug(f"update_event_news_type failed: {e}")
            self.conn.rollback()

    def insert_prediction(self, signal_id, event_id, horizon, predicted_return_pct,
                          target_price, confidence=0):
        """Insert prediction for a signal"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                INSERT INTO predictions (signal_id, event_id, horizon,
                    predicted_return_pct, target_price, confidence)
                VALUES ({placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder})
            """, (signal_id, event_id, horizon, predicted_return_pct,
                  target_price, confidence))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to insert prediction: {e}")
            self.conn.rollback()
            return False

    def insert_event(self, event_id, title, summary=None, source=None, link=None,
                     event_type=None, event_confidence=0, sentiment=None,
                     sentiment_confidence=0, magnitude=0, impact_score=0,
                     companies=None, published_at=None):
        """Insert a scraped event"""
        self._ensure_connected()
        try:
            companies_str = ','.join(companies) if isinstance(companies, list) else companies
            if self.is_postgres:
                self.conn.cursor().execute("""
                    INSERT INTO events (event_id, title, summary, source, link, event_type,
                        event_confidence, sentiment, sentiment_confidence, magnitude,
                        impact_score, companies, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                """, (event_id, title, summary, source, link, event_type,
                      event_confidence, sentiment, sentiment_confidence, magnitude,
                      impact_score, companies_str, published_at))
            else:
                self.conn.execute("""
                    INSERT OR IGNORE INTO events (event_id, title, summary, source, link,
                        event_type, event_confidence, sentiment, sentiment_confidence,
                        magnitude, impact_score, companies, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, title, summary, source, link, event_type,
                      event_confidence, sentiment, sentiment_confidence, magnitude,
                      impact_score, companies_str, published_at))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to insert event: {e}")
            self.conn.rollback()
            return False

    def insert_geo_event(self, event_id, country, description, impact='medium',
                         latitude=None, longitude=None, related_assets=None, source=None):
        """Insert a geopolitical event"""
        self._ensure_connected()
        try:
            assets_str = ','.join(related_assets) if isinstance(related_assets, list) else related_assets
            if self.is_postgres:
                self.conn.cursor().execute("""
                    INSERT INTO geo_events (event_id, country, event_description, impact,
                        latitude, longitude, related_assets, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                """, (event_id, country, description, impact, latitude, longitude,
                      assets_str, source))
            else:
                self.conn.execute("""
                    INSERT OR IGNORE INTO geo_events (event_id, country, event_description,
                        impact, latitude, longitude, related_assets, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, country, description, impact, latitude, longitude,
                      assets_str, source))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to insert geo event: {e}")
            self.conn.rollback()
            return False

    def insert_regime(self, regime_date, regime_type, volatility, momentum,
                      trend_strength, price_change_1d, regime_strength):
        """Insert market regime snapshot"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                INSERT INTO market_regimes (regime_date, regime_type, volatility,
                    momentum, trend_strength, price_change_1d, regime_strength)
                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder})
            """, (regime_date, regime_type, volatility, momentum,
                  trend_strength, price_change_1d, regime_strength))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to insert regime: {e}")
            self.conn.rollback()
            return False

    # ---- Article Deduplication ----

    def is_article_seen(self, url, title):
        """Check if article was already processed"""
        self._ensure_connected()
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            cursor = self.conn.cursor()
            cursor.execute(f"SELECT id FROM scraped_articles WHERE url_hash = {placeholder}",
                           (url_hash,))
            return cursor.fetchone() is not None
        except Exception:
            return False

    def mark_article_seen(self, url, title, source=None, summary=None):
        """Mark article as seen, storing text for potential LLM verification"""
        self._ensure_connected()
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        title_hash = hashlib.sha256(title.encode()).hexdigest()
        try:
            if self.is_postgres:
                self.conn.cursor().execute("""
                    INSERT INTO scraped_articles (url_hash, title_hash, source, url, title, summary)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url_hash) DO NOTHING
                """, (url_hash, title_hash, source, url, title, summary))
            else:
                self.conn.execute("""
                    INSERT OR IGNORE INTO scraped_articles (url_hash, title_hash, source, url, title, summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (url_hash, title_hash, source, url, title, summary))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to mark article: {e}")
            self.conn.rollback()

    # ---- LLM Cross-Verification ----

    def get_pending_verification(self, limit=25):
        """Get articles that haven't been LLM-verified yet"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        false_val = 'FALSE' if self.is_postgres else '0'
        return _execute_query(self.conn, f"""
            SELECT * FROM scraped_articles
            WHERE llm_verified = {false_val} AND title IS NOT NULL
            ORDER BY first_seen_at DESC LIMIT {placeholder}
        """, (limit,), fetch=True)

    def save_llm_result(self, url_hash, result_json):
        """Save LLM verification result for an article"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            true_val = 'TRUE' if self.is_postgres else '1'
            self.conn.cursor().execute(f"""
                UPDATE scraped_articles
                SET llm_verified = {true_val}, llm_result = {placeholder},
                    llm_verified_at = CURRENT_TIMESTAMP
                WHERE url_hash = {placeholder}
            """, (result_json, url_hash))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to save LLM result: {e}")
            self.conn.rollback()

    def get_cached_llm_result(self, url_hash):
        """Get cached LLM result for an article"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT llm_result FROM scraped_articles
            WHERE url_hash = {placeholder} AND llm_result IS NOT NULL
        """, (url_hash,), fetch=True)
        return rows[0]['llm_result'] if rows else None

    # ---- Prediction Tracking ----

    def get_expired_predictions(self, limit=100):
        """Find predictions past their horizon that haven't been checked yet"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        if self.is_postgres:
            query = """
                SELECT p.*, s.ticker, s.entry_price, s.sentiment
                FROM predictions p
                JOIN signals s ON p.event_id = s.event_id
                WHERE p.actual_price IS NULL AND (
                    (p.horizon = '1D' AND p.created_at <= NOW() - INTERVAL '1 day') OR
                    (p.horizon = '3D' AND p.created_at <= NOW() - INTERVAL '3 days') OR
                    (p.horizon = '5D' AND p.created_at <= NOW() - INTERVAL '5 days') OR
                    (p.horizon = '20D' AND p.created_at <= NOW() - INTERVAL '20 days')
                )
                ORDER BY p.created_at ASC LIMIT %s
            """
        else:
            query = """
                SELECT p.*, s.ticker, s.entry_price, s.sentiment
                FROM predictions p
                JOIN signals s ON p.event_id = s.event_id
                WHERE p.actual_price IS NULL AND (
                    (p.horizon = '1D' AND p.created_at <= datetime('now', '-1 day')) OR
                    (p.horizon = '3D' AND p.created_at <= datetime('now', '-3 days')) OR
                    (p.horizon = '5D' AND p.created_at <= datetime('now', '-5 days')) OR
                    (p.horizon = '20D' AND p.created_at <= datetime('now', '-20 days'))
                )
                ORDER BY p.created_at ASC LIMIT ?
            """
        return _execute_query(self.conn, query, (limit,), fetch=True)

    def update_prediction_actual(self, prediction_id, actual_price, actual_return_pct, hit_target):
        """Record the actual outcome of a prediction"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                UPDATE predictions
                SET actual_price = {placeholder}, actual_return_pct = {placeholder},
                    hit_target = {placeholder}, exit_timestamp = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """, (actual_price, actual_return_pct, 1 if hit_target else 0, prediction_id))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to update prediction: {e}")
            self.conn.rollback()
            return False

    def get_prediction_accuracy(self):
        """Get hit rates grouped by horizon"""
        self._ensure_connected()
        true_val = 'TRUE' if self.is_postgres else '1'
        return _execute_query(self.conn, f"""
            SELECT
                horizon,
                COUNT(*) as total,
                SUM(CASE WHEN hit_target = {true_val} THEN 1 ELSE 0 END) as hits,
                ROUND(AVG(actual_return_pct), 2) as avg_actual_return,
                ROUND(AVG(predicted_return_pct), 2) as avg_predicted_return
            FROM predictions
            WHERE actual_price IS NOT NULL
            GROUP BY horizon
            ORDER BY horizon
        """, fetch=True)

    def get_recent_prediction_outcomes(self, limit=20):
        """Get recently checked predictions with signal info"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT p.*, s.ticker, s.sentiment, s.alpha_score
            FROM predictions p
            JOIN signals s ON p.event_id = s.event_id
            WHERE p.actual_price IS NOT NULL
            ORDER BY p.exit_timestamp DESC LIMIT {placeholder}
        """, (limit,), fetch=True)

    # ---- User Management (SQLite local auth) ----

    def create_user(self, email, password_hash, display_name=None):
        """Create a new user (SQLite local auth only)"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                INSERT INTO users (email, password_hash, display_name)
                VALUES ({placeholder}, {placeholder}, {placeholder})
            """, (email, password_hash, display_name))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to create user: {e}")
            self.conn.rollback()
            return False

    def get_user_by_email(self, email):
        """Get user by email"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT * FROM users WHERE email = {placeholder}
        """, (email,), fetch=True)
        return rows[0] if rows else None

    # ---- Query Methods (used by API) ----

    def get_active_signals(self, limit=50):
        """Get active signals ranked by recency-decayed alpha.

        Each day-of-age subtracts ~5 effective alpha points so today's strong
        signal beats a 3-day-old signal of the same alpha — otherwise the home
        feed gets dominated by old high-alpha picks and looks stale to users.
        """
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        if self.is_postgres:
            order = ("(alpha_score - EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 * 5) "
                     "DESC, created_at DESC")
        else:
            order = ("(alpha_score - (julianday('now') - julianday(created_at)) * 5) "
                     "DESC, created_at DESC")
        return _execute_query(self.conn, f"""
            SELECT * FROM signals WHERE status = 'active'
            ORDER BY {order}
            LIMIT {placeholder}
        """, (limit,), fetch=True)

    def get_signal_by_id(self, event_id):
        """Get a single signal by event_id"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT * FROM signals WHERE event_id = {placeholder}
        """, (event_id,), fetch=True)
        return rows[0] if rows else None

    def get_predictions_for_signal(self, signal_id):
        """Get predictions for a signal"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT * FROM predictions WHERE signal_id = {placeholder}
            ORDER BY horizon
        """, (signal_id,), fetch=True)

    def get_recent_events(self, limit=50):
        """Get recent events"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT * FROM events ORDER BY created_at DESC LIMIT {placeholder}
        """, (limit,), fetch=True)

    def get_event_by_id(self, event_id):
        """Get a single event"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT * FROM events WHERE event_id = {placeholder}
        """, (event_id,), fetch=True)
        return rows[0] if rows else None

    def get_geo_events(self, limit=50):
        """Get geopolitical events"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT * FROM geo_events ORDER BY created_at DESC LIMIT {placeholder}
        """, (limit,), fetch=True)

    def get_alerts(self, limit=20, user_id='legacy'):
        """Get alerts for a user"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT * FROM alerts WHERE user_id = {placeholder}
            ORDER BY created_at DESC LIMIT {placeholder}
        """, (user_id, limit), fetch=True)

    def create_alert(self, ticker, alert_type, condition, threshold, user_id='legacy'):
        """Create a new alert for a user"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                INSERT INTO alerts (user_id, ticker, alert_type, condition, threshold)
                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
            """, (user_id, ticker, alert_type, condition, threshold))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to create alert: {e}")
            self.conn.rollback()
            return False

    def get_watchlist(self, limit=50, user_id='legacy'):
        """Get watchlist items for a user"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        return _execute_query(self.conn, f"""
            SELECT * FROM watchlist WHERE user_id = {placeholder}
            ORDER BY created_at DESC LIMIT {placeholder}
        """, (user_id, limit), fetch=True)

    def add_to_watchlist(self, ticker, notes='', user_id='legacy'):
        """Add stock to user's watchlist"""
        self._ensure_connected()
        try:
            if self.is_postgres:
                self.conn.cursor().execute("""
                    INSERT INTO watchlist (user_id, ticker, notes)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, ticker) DO UPDATE SET notes = EXCLUDED.notes,
                        updated_at = CURRENT_TIMESTAMP
                """, (user_id, ticker, notes))
            else:
                self.conn.execute("""
                    INSERT OR REPLACE INTO watchlist (user_id, ticker, notes)
                    VALUES (?, ?, ?)
                """, (user_id, ticker, notes))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add to watchlist: {e}")
            self.conn.rollback()
            return False

    def remove_from_watchlist(self, ticker, user_id='legacy'):
        """Remove stock from user's watchlist"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                DELETE FROM watchlist WHERE ticker = {placeholder} AND user_id = {placeholder}
            """, (ticker, user_id))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to remove from watchlist: {e}")
            self.conn.rollback()
            return False

    # ---- Notification Config ----

    def get_notification_config(self, user_id='legacy'):
        """Get notification configuration for a user"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT * FROM notification_config WHERE user_id = {placeholder} LIMIT 1
        """, (user_id,), fetch=True)
        return rows[0] if rows else None

    def save_notification_config(self, config, user_id='legacy'):
        """Save or update notification configuration for a user"""
        self._ensure_connected()
        vals = (
            config.get('discord_webhook_url'), config.get('telegram_bot_token'),
            config.get('telegram_chat_id'), config.get('min_alpha_score', 70),
            config.get('min_confidence', 0.7), config.get('event_types', 'all'),
            config.get('sentiment_filter', 'all'), config.get('cooldown_minutes', 30),
        )
        try:
            existing = self.get_notification_config(user_id)
            if existing:
                if self.is_postgres:
                    self.conn.cursor().execute("""
                        UPDATE notification_config SET
                            discord_webhook_url = %s, telegram_bot_token = %s,
                            telegram_chat_id = %s, min_alpha_score = %s,
                            min_confidence = %s, event_types = %s,
                            sentiment_filter = %s, cooldown_minutes = %s,
                            enabled = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = %s
                    """, (*vals, config.get('enabled', True), user_id))
                else:
                    self.conn.execute("""
                        UPDATE notification_config SET
                            discord_webhook_url = ?, telegram_bot_token = ?,
                            telegram_chat_id = ?, min_alpha_score = ?,
                            min_confidence = ?, event_types = ?,
                            sentiment_filter = ?, cooldown_minutes = ?,
                            enabled = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ?
                    """, (*vals, 1 if config.get('enabled', True) else 0, user_id))
            else:
                if self.is_postgres:
                    self.conn.cursor().execute("""
                        INSERT INTO notification_config (user_id, discord_webhook_url,
                            telegram_bot_token, telegram_chat_id, min_alpha_score,
                            min_confidence, event_types, sentiment_filter,
                            cooldown_minutes, enabled)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, *vals, config.get('enabled', True)))
                else:
                    self.conn.execute("""
                        INSERT INTO notification_config (user_id, discord_webhook_url,
                            telegram_bot_token, telegram_chat_id, min_alpha_score,
                            min_confidence, event_types, sentiment_filter,
                            cooldown_minutes, enabled)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, *vals, 1 if config.get('enabled', True) else 0))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save notification config: {e}")
            self.conn.rollback()
            return False

    def log_notification(self, signal_event_id, channel, ticker, alpha_score, status,
                         error_message=None):
        """Log a notification attempt"""
        self._ensure_connected()
        try:
            placeholder = '%s' if self.is_postgres else '?'
            self.conn.cursor().execute(f"""
                INSERT INTO notification_log (signal_event_id, channel, ticker,
                    alpha_score, status, error_message)
                VALUES ({placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder})
            """, (signal_event_id, channel, ticker, alpha_score, status, error_message))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to log notification: {e}")
            self.conn.rollback()

    def get_last_notification_for_ticker(self, ticker, channel):
        """Get last notification time for a ticker (cooldown check)"""
        self._ensure_connected()
        placeholder = '%s' if self.is_postgres else '?'
        rows = _execute_query(self.conn, f"""
            SELECT sent_at FROM notification_log
            WHERE ticker = {placeholder} AND channel = {placeholder} AND status = 'sent'
            ORDER BY sent_at DESC LIMIT 1
        """, (ticker, channel), fetch=True)
        return rows[0]['sent_at'] if rows else None

    # ---- Stats / Dashboard ----

    def get_dashboard_stats(self):
        """Get aggregated stats for the dashboard"""
        self._ensure_connected()
        try:
            stats = {}
            cursor = self.conn.cursor()

            # Signal counts
            rows = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM signals WHERE status = 'active'", fetch=True)
            stats['active_signals'] = rows[0]['cnt'] if rows else 0

            # Average alpha
            rows = _execute_query(self.conn,
                "SELECT AVG(alpha_score) as avg_alpha FROM signals WHERE status = 'active'",
                fetch=True)
            stats['avg_alpha'] = round(rows[0]['avg_alpha'] or 0, 1) if rows else 0

            # Average confidence
            rows = _execute_query(self.conn,
                "SELECT AVG(confidence) as avg_conf FROM signals WHERE status = 'active'",
                fetch=True)
            stats['avg_confidence'] = round((rows[0]['avg_conf'] or 0) * 100, 1) if rows else 0

            # Events today
            rows = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM events WHERE DATE(created_at) = CURRENT_DATE",
                fetch=True)
            stats['events_today'] = rows[0]['cnt'] if rows else 0

            # Total events
            rows = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM events", fetch=True)
            stats['total_events'] = rows[0]['cnt'] if rows else 0

            # Current regime
            rows = _execute_query(self.conn, """
                SELECT regime_type, regime_strength FROM market_regimes
                ORDER BY regime_date DESC LIMIT 1
            """, fetch=True)
            if rows:
                stats['current_regime'] = rows[0]['regime_type']
                stats['regime_strength'] = rows[0]['regime_strength']
            else:
                stats['current_regime'] = 'unknown'
                stats['regime_strength'] = 0

            # Watchlist count
            rows = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM watchlist", fetch=True)
            stats['watchlist_count'] = rows[0]['cnt'] if rows else 0

            # Bullish/Bearish split
            rows = _execute_query(self.conn, """
                SELECT sentiment, COUNT(*) as cnt FROM signals
                WHERE status = 'active' GROUP BY sentiment
            """, fetch=True)
            sentiment_counts = {r['sentiment']: r['cnt'] for r in rows}
            stats['bullish_count'] = sentiment_counts.get('bullish', 0)
            stats['bearish_count'] = sentiment_counts.get('bearish', 0)
            stats['neutral_count'] = sentiment_counts.get('neutral', 0)

            return stats
        except Exception as e:
            logger.error(f"Failed to get dashboard stats: {e}")
            return {
                'active_signals': 0, 'avg_alpha': 0, 'avg_confidence': 0,
                'events_today': 0, 'total_events': 0, 'current_regime': 'unknown',
                'regime_strength': 0, 'watchlist_count': 0,
                'bullish_count': 0, 'bearish_count': 0, 'neutral_count': 0
            }

    def get_scraper_status(self):
        """Get scraper status info"""
        self._ensure_connected()
        try:
            # Latest signal time
            rows = _execute_query(self.conn,
                "SELECT MAX(created_at) as last_run FROM signals", fetch=True)
            last_signal = rows[0]['last_run'] if rows and rows[0]['last_run'] else None

            # Latest event time
            rows = _execute_query(self.conn,
                "SELECT MAX(created_at) as last_run FROM events", fetch=True)
            last_event = rows[0]['last_run'] if rows and rows[0]['last_run'] else None

            # Counts
            signals = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM signals", fetch=True)
            events = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM events", fetch=True)
            articles = _execute_query(self.conn,
                "SELECT COUNT(*) as cnt FROM scraped_articles", fetch=True)

            return {
                'last_signal_at': str(last_signal) if last_signal else None,
                'last_event_at': str(last_event) if last_event else None,
                'total_signals': signals[0]['cnt'] if signals else 0,
                'total_events': events[0]['cnt'] if events else 0,
                'total_articles_processed': articles[0]['cnt'] if articles else 0
            }
        except Exception as e:
            logger.error(f"Failed to get scraper status: {e}")
            return {'error': str(e)}

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


# ============ CLI ============
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

    db = EventAlphaDB()
    if db.init_schema():
        print("Database schema initialized successfully!")
        print(f"Using: {'PostgreSQL' if db.is_postgres else 'SQLite'}")
    else:
        print("Schema initialization failed!")
    db.close()
