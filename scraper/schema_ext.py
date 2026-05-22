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
-- Track 1 Phase A: per-row exchange tag so screeners can filter NSE vs BSE
-- vs DUAL. Backfilled to 'NSE' for legacy rows (matches prior universe).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS exchange VARCHAR(8) DEFAULT 'NSE';
ALTER TABLE events  ADD COLUMN IF NOT EXISTS exchange VARCHAR(8) DEFAULT 'NSE';
-- Track 1 Phase B: bucketed corporate filings (concalls, presentations,
-- ratings, etc.) gain per-row exchange + AI-generated summary cache.
ALTER TABLE filings ADD COLUMN IF NOT EXISTS exchange VARCHAR(8);
ALTER TABLE filings ADD COLUMN IF NOT EXISTS summary  TEXT;
-- Addendum 2026-05-18: multi-source aggregation. Each signal links back
-- to its event_clusters row (cluster_hash) so we can JOIN to get sources
-- list + member_count. raw_alpha_score preserves the pre-multiplier value
-- for transparency / debugging. cluster_size is denormalized snapshot so
-- screeners don't have to JOIN for every list query.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cluster_hash   VARCHAR(64);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS raw_alpha_score FLOAT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cluster_size   INTEGER DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_signals_news_type ON signals(news_type);
CREATE INDEX IF NOT EXISTS idx_events_news_type ON events(news_type);
CREATE INDEX IF NOT EXISTS idx_signals_exchange  ON signals(exchange);
CREATE INDEX IF NOT EXISTS idx_events_exchange   ON events(exchange);
CREATE INDEX IF NOT EXISTS idx_filings_type_filed ON filings(filing_type, filed_at);
CREATE INDEX IF NOT EXISTS idx_signals_cluster_hash ON signals(cluster_hash);

-- Concall transcript paragraphs (Track 1, Phase D — 2026-05-19).
-- Splits filings.raw_text from concall-type filings into searchable
-- paragraphs. Powers /api/v1/concalls/search. The compounding archive moat:
-- every quarter management commentary becomes queryable across all NSE.
CREATE TABLE IF NOT EXISTS concall_paragraphs (
    id SERIAL PRIMARY KEY,
    filing_id INTEGER REFERENCES filings(id) ON DELETE CASCADE,
    ticker VARCHAR(20),
    filed_at TIMESTAMP,
    paragraph_idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (filing_id, paragraph_idx)
);
CREATE INDEX IF NOT EXISTS idx_cp_ticker ON concall_paragraphs(ticker);
CREATE INDEX IF NOT EXISTS idx_cp_filed_at ON concall_paragraphs(filed_at);
CREATE INDEX IF NOT EXISTS idx_cp_filing ON concall_paragraphs(filing_id);

-- Earnings reaction archive (Track 1, Phase C — 2026-05-18).
-- Every historical earnings date × T+1/T+3/T+5 price reaction × EPS surprise.
-- Populated by backend/earnings_reactions_backfill.py; the v1 endpoint
-- /api/v1/earnings/reactions/<ticker> reads from here first, falls back to
-- live yfinance only on cache miss. This is the data-accumulation moat —
-- every quarter the archive deepens; competitors with no historical store
-- can't replicate the comp-against-history view.
CREATE TABLE IF NOT EXISTS earnings_reactions (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    earnings_date DATE NOT NULL,
    base_close FLOAT,            -- closing price ON or just before the announcement
    ret_1d_pct FLOAT,
    ret_3d_pct FLOAT,
    ret_5d_pct FLOAT,
    eps_estimate FLOAT,
    eps_reported FLOAT,
    surprise_pct FLOAT,          -- (reported - estimate) / |estimate| * 100
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, earnings_date)
);
CREATE INDEX IF NOT EXISTS idx_er_ticker ON earnings_reactions(ticker);
CREATE INDEX IF NOT EXISTS idx_er_date ON earnings_reactions(earnings_date);

-- Earnings forecast archive (Track A, 2026-05-19).
-- Predicts next-quarter results: revenue/EPS estimate + CI, beat probability,
-- expected reaction, post-event entry window. Powers /api/v1/earnings/forecast.
-- Reused by the wedge audit log ("we forecasted this beat 7 days ago") and
-- the Pro-Research dashboard. Idempotent on (ticker, target_earnings_date,
-- model_version) so daily re-runs update in place.
CREATE TABLE IF NOT EXISTS earnings_forecasts (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    forecast_date DATE NOT NULL,
    target_earnings_date DATE NOT NULL,
    model_version VARCHAR(20) NOT NULL DEFAULT 'v0',
    revenue_estimate FLOAT,
    revenue_low FLOAT,
    revenue_high FLOAT,
    eps_estimate FLOAT,
    eps_low FLOAT,
    eps_high FLOAT,
    -- Multi-metric forecasts (in crores ₹ unless _pct)
    net_profit_estimate FLOAT,
    net_profit_low FLOAT,
    net_profit_high FLOAT,
    ebitda_estimate FLOAT,
    ebitda_low FLOAT,
    ebitda_high FLOAT,
    operating_income_estimate FLOAT,
    operating_margin_pct FLOAT,
    ebitda_margin_pct FLOAT,
    revenue_growth_yoy_pct FLOAT,
    revenue_growth_qoq_pct FLOAT,
    -- Hit/miss
    p_beat FLOAT,
    p_meet FLOAT,
    p_miss FLOAT,
    predicted_hit VARCHAR(10),
    hit_confidence FLOAT,
    expected_ret_1d_pct FLOAT,
    ret_1d_ci_low FLOAT,
    ret_1d_ci_high FLOAT,
    dump_risk_score FLOAT,
    recommended_action VARCHAR(20),
    wait_minutes_estimate INTEGER,
    optimal_entry_start_min INTEGER,
    optimal_entry_end_min INTEGER,
    expected_gap_pct FLOAT,
    fade_probability FLOAT,
    expected_5d_max_drawdown_pct FLOAT,
    expected_5d_max_gain_pct FLOAT,
    analogs_used INTEGER,
    fallback_to_sector BOOLEAN DEFAULT FALSE,
    top_drivers TEXT,
    narrative_summary TEXT,
    llm_generated BOOLEAN DEFAULT FALSE,
    features_json TEXT,
    data_insufficient BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, target_earnings_date, model_version)
);
CREATE INDEX IF NOT EXISTS idx_ef_ticker ON earnings_forecasts(ticker);
CREATE INDEX IF NOT EXISTS idx_ef_target ON earnings_forecasts(target_earnings_date);
CREATE INDEX IF NOT EXISTS idx_ef_forecast_date ON earnings_forecasts(forecast_date);

CREATE TABLE IF NOT EXISTS concall_guidance_llm (
    id SERIAL PRIMARY KEY,
    filing_id INTEGER NOT NULL,
    direction VARCHAR(20),
    magnitude VARCHAR(20),
    time_horizon VARCHAR(40),
    confidence FLOAT,
    raw_extract TEXT,
    model_used VARCHAR(40),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (filing_id)
);
CREATE INDEX IF NOT EXISTS idx_cgl_filing ON concall_guidance_llm(filing_id);

CREATE TABLE IF NOT EXISTS news_extraction_llm (
    id SERIAL PRIMARY KEY,
    news_event_id VARCHAR(200) NOT NULL,
    event_type VARCHAR(40),
    entities TEXT,
    monetary_value_cr FLOAT,
    time_horizon VARCHAR(40),
    sector_impact TEXT,
    confidence FLOAT,
    model_used VARCHAR(40),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (news_event_id)
);
CREATE INDEX IF NOT EXISTS idx_nel_news ON news_extraction_llm(news_event_id);

-- Financial planner agent (Track A, 2026-05-20) — multi-turn threads,
-- per-message persistence, scope/recommendation refusal log, user profile.
-- See [[financial-planner-agent]] memory. Powered by backend/chat_routes.py.
CREATE TABLE IF NOT EXISTS chat_threads (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_chat_threads_user_active
    ON chat_threads(user_id, last_active_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL,                -- 'user' | 'assistant'
    content TEXT NOT NULL,
    citations_json TEXT,
    tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread
    ON chat_messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS user_profile (
    user_id TEXT PRIMARY KEY,
    risk_profile VARCHAR(16),                 -- low | med | high
    horizon VARCHAR(16),                      -- intraday | swing | short | medium | long
    sectors_json TEXT,                        -- JSON array, ≤10 entries
    goals_text TEXT,                          -- ≤500 chars
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_refusals (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    thread_id INTEGER,
    question TEXT,
    reason VARCHAR(20),                       -- 'recommendation' | 'off_topic'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_refusals_user ON chat_refusals(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_refusals_reason ON chat_refusals(reason);
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

-- Concall paragraphs — see comment on the Postgres definition.
CREATE TABLE IF NOT EXISTS concall_paragraphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id INTEGER,
    ticker TEXT,
    filed_at TIMESTAMP,
    paragraph_idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (filing_id, paragraph_idx)
);
CREATE INDEX IF NOT EXISTS idx_cp_ticker ON concall_paragraphs(ticker);
CREATE INDEX IF NOT EXISTS idx_cp_filed_at ON concall_paragraphs(filed_at);
CREATE INDEX IF NOT EXISTS idx_cp_filing ON concall_paragraphs(filing_id);

-- Earnings reaction archive — see comment on the Postgres definition.
CREATE TABLE IF NOT EXISTS earnings_reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    base_close REAL,
    ret_1d_pct REAL,
    ret_3d_pct REAL,
    ret_5d_pct REAL,
    eps_estimate REAL,
    eps_reported REAL,
    surprise_pct REAL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, earnings_date)
);
CREATE INDEX IF NOT EXISTS idx_er_ticker ON earnings_reactions(ticker);
CREATE INDEX IF NOT EXISTS idx_er_date ON earnings_reactions(earnings_date);

-- Earnings forecast archive — see comment on the Postgres definition.
CREATE TABLE IF NOT EXISTS earnings_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    forecast_date DATE NOT NULL,
    target_earnings_date DATE NOT NULL,
    model_version TEXT NOT NULL DEFAULT 'v0',
    revenue_estimate REAL,
    revenue_low REAL,
    revenue_high REAL,
    eps_estimate REAL,
    eps_low REAL,
    eps_high REAL,
    -- Multi-metric forecasts (in crores ₹ unless _pct)
    net_profit_estimate REAL,
    net_profit_low REAL,
    net_profit_high REAL,
    ebitda_estimate REAL,
    ebitda_low REAL,
    ebitda_high REAL,
    operating_income_estimate REAL,
    operating_margin_pct REAL,
    ebitda_margin_pct REAL,
    revenue_growth_yoy_pct REAL,
    revenue_growth_qoq_pct REAL,
    p_beat REAL,
    p_meet REAL,
    p_miss REAL,
    predicted_hit TEXT,
    hit_confidence REAL,
    expected_ret_1d_pct REAL,
    ret_1d_ci_low REAL,
    ret_1d_ci_high REAL,
    dump_risk_score REAL,
    recommended_action TEXT,
    wait_minutes_estimate INTEGER,
    optimal_entry_start_min INTEGER,
    optimal_entry_end_min INTEGER,
    expected_gap_pct REAL,
    fade_probability REAL,
    expected_5d_max_drawdown_pct REAL,
    expected_5d_max_gain_pct REAL,
    analogs_used INTEGER,
    fallback_to_sector INTEGER DEFAULT 0,
    top_drivers TEXT,
    narrative_summary TEXT,
    llm_generated INTEGER DEFAULT 0,
    features_json TEXT,
    data_insufficient INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, target_earnings_date, model_version)
);
CREATE INDEX IF NOT EXISTS idx_ef_ticker ON earnings_forecasts(ticker);
CREATE INDEX IF NOT EXISTS idx_ef_target ON earnings_forecasts(target_earnings_date);
CREATE INDEX IF NOT EXISTS idx_ef_forecast_date ON earnings_forecasts(forecast_date);

CREATE TABLE IF NOT EXISTS concall_guidance_llm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id INTEGER NOT NULL,
    direction TEXT,
    magnitude TEXT,
    time_horizon TEXT,
    confidence REAL,
    raw_extract TEXT,
    model_used TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (filing_id)
);
CREATE INDEX IF NOT EXISTS idx_cgl_filing ON concall_guidance_llm(filing_id);

CREATE TABLE IF NOT EXISTS news_extraction_llm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_event_id TEXT NOT NULL,
    event_type TEXT,
    entities TEXT,
    monetary_value_cr REAL,
    time_horizon TEXT,
    sector_impact TEXT,
    confidence REAL,
    model_used TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (news_event_id)
);
CREATE INDEX IF NOT EXISTS idx_nel_news ON news_extraction_llm(news_event_id);

-- Financial planner agent (Track A, 2026-05-20) — see PG block for design notes.
CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_threads_user_active
    ON chat_threads(user_id, last_active_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations_json TEXT,
    tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(thread_id) REFERENCES chat_threads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread
    ON chat_messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS user_profile (
    user_id TEXT PRIMARY KEY,
    risk_profile TEXT,
    horizon TEXT,
    sectors_json TEXT,
    goals_text TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    thread_id INTEGER,
    question TEXT,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_refusals_user ON chat_refusals(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_refusals_reason ON chat_refusals(reason);
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
    # Track 1 Phase A — distinguishes NSE vs BSE vs DUAL listings so screeners,
    # movers, and ticker-resolution paths can branch on exchange.
    ("exchange", "TEXT DEFAULT 'NSE'"),
    # Subject-confidence: how confident is the entity_linker that this ticker
    # is the editorial subject of the source article (0.0–1.0). Lets the
    # curated-signals filter and the LLM explainer reason about match quality.
    ("subject_confidence", "REAL"),
    ("subject_evidence", "TEXT"),  # JSON: reasons + intermediate scores
    # Addendum 2026-05-18 — multi-source aggregation
    ("cluster_hash",    "TEXT"),
    ("raw_alpha_score", "REAL"),
    ("cluster_size",    "INTEGER DEFAULT 1"),
]

EVENT_EXT_COLUMNS = [
    ("news_type", "TEXT DEFAULT 'news_article'"),
    ("exchange", "TEXT DEFAULT 'NSE'"),
    ("subject_confidence", "REAL"),
    ("subject_evidence", "TEXT"),
]

FILING_EXT_COLUMNS = [
    ("exchange", "TEXT"),
    ("summary", "TEXT"),
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
        existing_fl = {r[1] for r in cursor.execute("PRAGMA table_info(filings)").fetchall()}
        for col, spec in FILING_EXT_COLUMNS:
            if col not in existing_fl:
                try:
                    cursor.execute(f"ALTER TABLE filings ADD COLUMN {col} {spec}")
                except Exception as exc:
                    logger.warning("sqlite add column filings.%s failed: %s", col, exc)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_news_type ON signals(news_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_news_type ON events(news_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_exchange ON signals(exchange)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_exchange ON events(exchange)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_cluster_hash ON signals(cluster_hash)")
        except Exception as exc:
            logger.debug("news_type/exchange/cluster_hash index create: %s", exc)
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
