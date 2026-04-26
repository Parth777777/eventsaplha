"""
Schema extensions for Phase 1.5 — new tables + signal column additions.

Runs idempotently alongside the base schema in database_schema.py. Keeping it
separate keeps the original schema file stable and lets us roll back by
dropping only the extension tables.
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)


PG_EXT_SCHEMA = """
-- Promoter / shareholding intelligence
CREATE TABLE IF NOT EXISTS promoter_holdings (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    quarter_end DATE NOT NULL,
    promoter_pct FLOAT,
    promoter_pledge_pct FLOAT,
    fii_pct FLOAT,
    dii_pct FLOAT,
    public_pct FLOAT,
    mutual_fund_pct FLOAT,
    insurance_pct FLOAT,
    raw_xbrl_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, quarter_end)
);
CREATE INDEX IF NOT EXISTS idx_prom_holdings_ticker ON promoter_holdings(ticker);
CREATE INDEX IF NOT EXISTS idx_prom_holdings_quarter ON promoter_holdings(quarter_end);

CREATE TABLE IF NOT EXISTS promoters (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    person_name VARCHAR(300) NOT NULL,
    role VARCHAR(100),
    din VARCHAR(50),
    other_directorships TEXT,
    source TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, person_name)
);
CREATE INDEX IF NOT EXISTS idx_promoters_ticker ON promoters(ticker);
CREATE INDEX IF NOT EXISTS idx_promoters_din ON promoters(din);

CREATE TABLE IF NOT EXISTS promoter_events (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    event_date DATE NOT NULL,
    event_type VARCHAR(50) NOT NULL,          -- sast_acquire, sast_dispose, pit_buy, pit_sell, pledge, release
    person_name VARCHAR(300),
    quantity FLOAT,
    pct_before FLOAT,
    pct_after FLOAT,
    reason TEXT,
    source TEXT,
    link TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, event_date, event_type, person_name)
);
CREATE INDEX IF NOT EXISTS idx_prom_events_ticker ON promoter_events(ticker);
CREATE INDEX IF NOT EXISTS idx_prom_events_date ON promoter_events(event_date);

-- Filings + forensic layer
CREATE TABLE IF NOT EXISTS filings (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    filing_type VARCHAR(50),                  -- quarterly_result, order_intimation, agm, other
    title TEXT,
    pdf_url TEXT,
    filed_at TIMESTAMP,
    source VARCHAR(50),                       -- bse, nse, ir_page
    raw_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, pdf_url)
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker);
CREATE INDEX IF NOT EXISTS idx_filings_filed_at ON filings(filed_at);

CREATE TABLE IF NOT EXISTS filing_numbers (
    id SERIAL PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    field_name VARCHAR(100),                  -- revenue, net_profit, order_value, eps, margin
    value FLOAT,
    unit VARCHAR(20),                         -- cr, lakh, mn, pct, units
    raw_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_filing_nums_filing ON filing_numbers(filing_id);
CREATE INDEX IF NOT EXISTS idx_filing_nums_field ON filing_numbers(field_name);

CREATE TABLE IF NOT EXISTS sebi_disclosures (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    disclosure_type VARCHAR(30),              -- pit_reg7, sast_reg29
    person_name VARCHAR(300),
    designation VARCHAR(100),
    transaction_type VARCHAR(20),             -- buy, sell, pledge, release
    quantity FLOAT,
    pct_before FLOAT,
    pct_after FLOAT,
    transaction_date DATE,
    disclosed_at TIMESTAMP,
    source VARCHAR(50),
    link TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, disclosure_type, person_name, transaction_date, quantity)
);
CREATE INDEX IF NOT EXISTS idx_sebi_ticker ON sebi_disclosures(ticker);
CREATE INDEX IF NOT EXISTS idx_sebi_txn_date ON sebi_disclosures(transaction_date);

CREATE TABLE IF NOT EXISTS article_discrepancies (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(200),
    filing_id INTEGER REFERENCES filings(id) ON DELETE SET NULL,
    field_name VARCHAR(100),
    article_value FLOAT,
    filing_value FLOAT,
    diff_pct FLOAT,
    tolerance_pct FLOAT,
    flagged BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_discrepancy_event ON article_discrepancies(event_id);

CREATE TABLE IF NOT EXISTS manipulation_scores (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(200) UNIQUE NOT NULL,
    score INTEGER,
    band VARCHAR(20),                         -- clean, unverified, likely_manipulated
    reasons TEXT,                             -- JSON list of contributing reason codes
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_manip_event ON manipulation_scores(event_id);
CREATE INDEX IF NOT EXISTS idx_manip_band ON manipulation_scores(band);

-- Social + credibility
CREATE TABLE IF NOT EXISTS social_sources (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL,            -- reddit, twitter, telegram
    handle VARCHAR(200) NOT NULL,
    status VARCHAR(20) DEFAULT 'watched',     -- watched, promoted, demoted, banned
    lead_time_minutes FLOAT DEFAULT 0,
    hit_rate FLOAT DEFAULT 0,
    posts_seen INTEGER DEFAULT 0,
    last_scored_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, handle)
);
CREATE INDEX IF NOT EXISTS idx_social_src_status ON social_sources(status);

CREATE TABLE IF NOT EXISTS source_credibility (
    id SERIAL PRIMARY KEY,
    outlet VARCHAR(200) NOT NULL,
    author VARCHAR(200),
    hit_rate FLOAT DEFAULT 0.5,
    sample_size INTEGER DEFAULT 0,
    credibility_score FLOAT DEFAULT 0.5,
    last_computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(outlet, author)
);
CREATE INDEX IF NOT EXISTS idx_cred_outlet ON source_credibility(outlet);

-- Publish priority (first-seen clustering)
CREATE TABLE IF NOT EXISTS event_clusters (
    id SERIAL PRIMARY KEY,
    cluster_hash VARCHAR(64) UNIQUE NOT NULL,
    canonical_headline TEXT,
    ticker VARCHAR(20),
    first_seen_at TIMESTAMP,
    first_seen_source VARCHAR(100),
    member_count INTEGER DEFAULT 0,
    sources TEXT,                             -- comma-separated list
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cluster_ticker ON event_clusters(ticker);
CREATE INDEX IF NOT EXISTS idx_cluster_seen ON event_clusters(first_seen_at);

-- Glossary cache (LLM-generated definitions awaiting review)
CREATE TABLE IF NOT EXISTS glossary_cache (
    id SERIAL PRIMARY KEY,
    term VARCHAR(200) UNIQUE NOT NULL,
    plain_english TEXT,
    example TEXT,
    hit_count INTEGER DEFAULT 0,
    verified_by VARCHAR(20) DEFAULT 'llm',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Commodity + overnight snapshots
CREATE TABLE IF NOT EXISTS commodity_snapshots (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(30) NOT NULL,
    snap_date DATE NOT NULL,
    close FLOAT,
    pct_change FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, snap_date)
);

CREATE TABLE IF NOT EXISTS overnight_snapshots (
    id SERIAL PRIMARY KEY,
    snap_date DATE UNIQUE NOT NULL,
    payload TEXT,                             -- JSON blob of indices + correlations
    predicted_nifty_bias FLOAT,
    confidence FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Signal column extensions (additive — never drop)
-- Run only if column absent; Postgres supports IF NOT EXISTS on columns
ALTER TABLE signals ADD COLUMN IF NOT EXISTS intent VARCHAR(30);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS fingerprint_flags TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS volume_confirmation BOOLEAN DEFAULT FALSE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS volume_multiplier FLOAT DEFAULT 1.0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS obv_divergence_flag VARCHAR(15);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS article_discrepancy_flag BOOLEAN DEFAULT FALSE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS manipulation_score INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS first_seen_source VARCHAR(100);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS priority_boost BOOLEAN DEFAULT FALSE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS edge_minutes FLOAT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS coordinated_campaign BOOLEAN DEFAULT FALSE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS news_type VARCHAR(20) DEFAULT 'news_article';
ALTER TABLE events ADD COLUMN IF NOT EXISTS news_type VARCHAR(20) DEFAULT 'news_article';
CREATE INDEX IF NOT EXISTS idx_signals_news_type ON signals(news_type);
CREATE INDEX IF NOT EXISTS idx_events_news_type ON events(news_type);
"""


# SQLite doesn't support `ADD COLUMN IF NOT EXISTS` directly in all versions,
# so we probe pragma_table_info and skip existing columns.
SQLITE_EXT_TABLES = """
CREATE TABLE IF NOT EXISTS promoter_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    quarter_end DATE NOT NULL,
    promoter_pct REAL,
    promoter_pledge_pct REAL,
    fii_pct REAL,
    dii_pct REAL,
    public_pct REAL,
    mutual_fund_pct REAL,
    insurance_pct REAL,
    raw_xbrl_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, quarter_end)
);
CREATE INDEX IF NOT EXISTS idx_prom_holdings_ticker ON promoter_holdings(ticker);

CREATE TABLE IF NOT EXISTS promoters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    person_name TEXT NOT NULL,
    role TEXT,
    din TEXT,
    other_directorships TEXT,
    source TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, person_name)
);
CREATE INDEX IF NOT EXISTS idx_promoters_ticker ON promoters(ticker);

CREATE TABLE IF NOT EXISTS promoter_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_date DATE NOT NULL,
    event_type TEXT NOT NULL,
    person_name TEXT,
    quantity REAL,
    pct_before REAL,
    pct_after REAL,
    reason TEXT,
    source TEXT,
    link TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, event_date, event_type, person_name)
);
CREATE INDEX IF NOT EXISTS idx_prom_events_ticker ON promoter_events(ticker);

CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    filing_type TEXT,
    title TEXT,
    pdf_url TEXT,
    filed_at TIMESTAMP,
    source TEXT,
    raw_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, pdf_url)
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker);

CREATE TABLE IF NOT EXISTS filing_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id INTEGER NOT NULL,
    field_name TEXT,
    value REAL,
    unit TEXT,
    raw_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(filing_id) REFERENCES filings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_filing_nums_filing ON filing_numbers(filing_id);

CREATE TABLE IF NOT EXISTS sebi_disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    disclosure_type TEXT,
    person_name TEXT,
    designation TEXT,
    transaction_type TEXT,
    quantity REAL,
    pct_before REAL,
    pct_after REAL,
    transaction_date DATE,
    disclosed_at TIMESTAMP,
    source TEXT,
    link TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, disclosure_type, person_name, transaction_date, quantity)
);
CREATE INDEX IF NOT EXISTS idx_sebi_ticker ON sebi_disclosures(ticker);

CREATE TABLE IF NOT EXISTS article_discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    filing_id INTEGER,
    field_name TEXT,
    article_value REAL,
    filing_value REAL,
    diff_pct REAL,
    tolerance_pct REAL,
    flagged INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(filing_id) REFERENCES filings(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_discrepancy_event ON article_discrepancies(event_id);

CREATE TABLE IF NOT EXISTS manipulation_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    score INTEGER,
    band TEXT,
    reasons TEXT,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_manip_event ON manipulation_scores(event_id);

CREATE TABLE IF NOT EXISTS social_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    status TEXT DEFAULT 'watched',
    lead_time_minutes REAL DEFAULT 0,
    hit_rate REAL DEFAULT 0,
    posts_seen INTEGER DEFAULT 0,
    last_scored_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, handle)
);

CREATE TABLE IF NOT EXISTS source_credibility (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outlet TEXT NOT NULL,
    author TEXT,
    hit_rate REAL DEFAULT 0.5,
    sample_size INTEGER DEFAULT 0,
    credibility_score REAL DEFAULT 0.5,
    last_computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(outlet, author)
);

CREATE TABLE IF NOT EXISTS event_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_hash TEXT UNIQUE NOT NULL,
    canonical_headline TEXT,
    ticker TEXT,
    first_seen_at TIMESTAMP,
    first_seen_source TEXT,
    member_count INTEGER DEFAULT 0,
    sources TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS glossary_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT UNIQUE NOT NULL,
    plain_english TEXT,
    example TEXT,
    hit_count INTEGER DEFAULT 0,
    verified_by TEXT DEFAULT 'llm',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS commodity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    snap_date DATE NOT NULL,
    close REAL,
    pct_change REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, snap_date)
);

CREATE TABLE IF NOT EXISTS overnight_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date DATE UNIQUE NOT NULL,
    payload TEXT,
    predicted_nifty_bias REAL,
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


SIGNAL_EXT_COLUMNS = [
    ("intent", "TEXT"),
    ("fingerprint_flags", "TEXT"),
    ("volume_confirmation", "INTEGER DEFAULT 0"),
    ("volume_multiplier", "REAL DEFAULT 1.0"),
    ("obv_divergence_flag", "TEXT"),
    ("article_discrepancy_flag", "INTEGER DEFAULT 0"),
    ("manipulation_score", "INTEGER"),
    ("first_seen_source", "TEXT"),
    ("priority_boost", "INTEGER DEFAULT 0"),
    ("edge_minutes", "REAL"),
    ("coordinated_campaign", "INTEGER DEFAULT 0"),
    ("news_type", "TEXT DEFAULT 'news_article'"),
    ("reasoning", "TEXT"),  # JSON blob: structured why-this-pick chain
]

EVENT_EXT_COLUMNS = [
    ("news_type", "TEXT DEFAULT 'news_article'"),
]


def _is_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").startswith("postgres"))


def apply(conn) -> None:
    """Apply extension schema to an open connection.

    Safe to call repeatedly — every statement is IF NOT EXISTS.
    """
    cursor = conn.cursor()
    if _is_postgres():
        for stmt in PG_EXT_SCHEMA.split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                cursor.execute(s)
            except Exception as exc:
                logger.warning("schema ext stmt failed (continuing): %s -- %s", s[:80], exc)
                conn.rollback()
        conn.commit()
    else:
        for stmt in SQLITE_EXT_TABLES.split(";"):
            s = stmt.strip()
            if s:
                try:
                    cursor.execute(s)
                except Exception as exc:
                    logger.warning("sqlite ext stmt failed: %s -- %s", s[:80], exc)
        # signal column additions (probe first — SQLite pre-3.35 lacks IF NOT EXISTS on columns)
        existing = {r[1] for r in cursor.execute("PRAGMA table_info(signals)").fetchall()}
        for col, spec in SIGNAL_EXT_COLUMNS:
            if col not in existing:
                try:
                    cursor.execute(f"ALTER TABLE signals ADD COLUMN {col} {spec}")
                except Exception as exc:
                    logger.warning("sqlite add column %s failed: %s", col, exc)
        existing_ev = {r[1] for r in cursor.execute("PRAGMA table_info(events)").fetchall()}
        for col, spec in EVENT_EXT_COLUMNS:
            if col not in existing_ev:
                try:
                    cursor.execute(f"ALTER TABLE events ADD COLUMN {col} {spec}")
                except Exception as exc:
                    logger.warning("sqlite add column events.%s failed: %s", col, exc)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_news_type ON signals(news_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_news_type ON events(news_type)")
        except Exception as exc:
            logger.debug("news_type index create: %s", exc)
        conn.commit()
    logger.info("schema extensions applied")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    from database_schema import TickwaveDB

    db = TickwaveDB()
    apply(db.conn)
    print("extensions applied")
    db.close()
