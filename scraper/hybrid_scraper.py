#!/usr/bin/env python3
"""
Tickwave Hybrid Multi-Source Scraper
=====================================
Combines RSS feeds, NewsAPI, Google News, market data APIs, and NLP
for real-time event extraction. Writes to database AND JSON.

Components:
1. RSSFeedCollector - News from RSS feeds + Google News + NewsAPI
2. MarketDataCollector - Stock prices via yfinance (batch mode)
3. PolicyParser - NLP-based event extraction and sentiment analysis
4. SignalEngine - Converts events to trading signals with alpha scoring
5. DataPipeline - Orchestrator that combines all sources and writes to DB
"""

import logging
import os
import json
import socket
import requests
import feedparser

# feedparser respects the default socket timeout for its internal urllib calls.
# Without this, a single dead RSS feed (e.g. a host that accepts the TCP
# connection then never sends) hangs fetch_feeds() forever and silently
# starves the whole pipeline. Tunable via FEED_TIMEOUT_SECS.
# Bumped 10s → 25s: under load some upstream RSS hosts respond slowly but
# successfully past 10s; the previous timeout dropped legitimate articles.
socket.setdefaulttimeout(float(os.getenv('FEED_TIMEOUT_SECS', '25')))
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import re
from collections import defaultdict
import time
import hashlib

# Config
from config import (
    RSS_SOURCES, MONITORED_STOCKS, STOCK_COMPANIES, EVENT_KEYWORDS,
    POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, OUTPUT_FILE, BACKUP_FILE,
    LOG_FILE, LOG_LEVEL, REQUEST_TIMEOUT, MAX_ARTICLES_PER_SOURCE,
    NEWSAPI_KEY, NEWSAPI_QUERY, GOOGLE_NEWS_QUERIES,
    GEO_KEYWORDS, LARGE_CAP, MID_CAP, SMALL_CAP,
    MACRO_SECTOR_MAP, SECTOR_STOCKS,
    SECTOR_ROTATION_QUERIES, GOOGLE_NEWS_SECTOR_PER_CYCLE,
    TICKER_NEWS_ROTATION, TICKERS_PER_CYCLE,
    GOOGLE_NEWS_ENTRIES_PER_QUERY,
)
from stock_universe import (
    STOCK_UNIVERSE, UNIVERSE_TICKERS, UNIVERSE_SHORT_NAMES,
    AMBIGUOUS_TICKERS, AMBIGUOUS_FIRST_WORDS,
)

# Module-level rotation state — persists across scrape cycles within a process
_ROTATION_STATE = {'sector_idx': 0, 'ticker_idx': 0}

def _next_rotation_slice(items, count, key):
    """Return next `count` items from `items` starting at the rotation cursor for `key`,
    wrapping around. Updates the cursor."""
    if not items:
        return []
    n = len(items)
    start = _ROTATION_STATE.get(key, 0) % n
    out = []
    for i in range(min(count, n)):
        out.append(items[(start + i) % n])
    _ROTATION_STATE[key] = (start + count) % n
    return out

# Alpha Scoring Engine
from alpha_scoring_engine import (
    RegimeDetector, AlphaScoringEngine, SignalValidator, PredictionEngine,
    MarketData, EventData, MarketRegime, StockContext
)

# Sector mappings
from config import STOCK_SECTORS, SECTOR_STOCKS

# Database
from database_schema import TickwaveDB

# Source tiering (Tier 1-4 weighting + content-hash dedup + cross-source gate)
try:
    from source_tiering import (
        classify_source,
        cluster_events,
        composite_event_weight,
        alpha_cap_for_status,
        freshness_label,
    )
except Exception:  # pragma: no cover — keep pipeline working if module missing
    classify_source = cluster_events = composite_event_weight = None
    alpha_cap_for_status = freshness_label = None


def enrich_events_with_tiering(events, articles):
    """Attach source_tier, freshness, confirmation status to each event.

    Mutates events in place (also returns the list for chaining).
    Downstream alpha scoring reads `event['tier_weight']` and
    `event['confirmation']` to penalise single-source / unverified news.
    """
    if not classify_source:
        return events

    # Cluster the raw articles first so each event can pick up its cluster
    clusters = cluster_events(articles or [])
    title_to_cluster = {}
    for cl in clusters:
        title_to_cluster[cl.canonical_title] = cl
        for s in cl.sources:
            # also index by link for direct lookup
            if s.get("link"):
                title_to_cluster[s["link"]] = cl

    for ev in events:
        link = ev.get("link", "")
        title = ev.get("title", "")
        cl = title_to_cluster.get(link) or title_to_cluster.get(title)

        is_filing = bool(classify_source(ev.get("source", ""), link).tier == 1)
        if cl:
            w = composite_event_weight(cl, is_filing=is_filing)
            ev["tier_weight"] = w["weight"]
            ev["source_tier"] = w["best_tier"]
            ev["freshness"] = w["freshness"]
            ev["freshness_label"] = w["freshness_label"]
            ev["confirmation"] = w["confirmation"]
            ev["source_count"] = w["source_count"]
            ev["distinct_sources"] = w["distinct_sources"]
            ev["news_velocity_per_hour"] = w["velocity_per_hour"]
            ev["high_velocity"] = w["is_high_velocity"]
            cap = alpha_cap_for_status(w["confirmation"])
            if cap is not None:
                ev["alpha_cap"] = cap
        else:
            meta = classify_source(ev.get("source", ""), link)
            ev["tier_weight"] = meta.weight
            ev["source_tier"] = meta.tier
            published = ev.get("published") or ev.get("timestamp")
            ev["freshness_label"] = freshness_label(published) if freshness_label else None
            ev["confirmation"] = "single" if meta.tier <= 2 else "unconfirmed"

        # Apply tier weight to impact/magnitude — single tipster sources get downweighted.
        try:
            tw = float(ev.get("tier_weight", 1.0) or 1.0)
            base_mag = float(ev.get("magnitude", 0) or 0)
            ev["magnitude"] = max(1, int(round(base_mag * (0.5 + 0.5 * tw))))
            ev["impact_score"] = int(ev["magnitude"] * 10)
        except Exception:
            pass

        # Cluster summary: list co-reporting sources (for "5 sources covering this" UI)
        if cl and cl.source_count > 1:
            ev["cluster_sources"] = sorted({
                s.get("canonical") or s.get("name") for s in cl.sources if s.get("canonical")
            })

        # Filing-first override: if any source in this cluster is Tier-1
        # (BSE / NSE / SEBI / RBI filing), flag it as filing-grade so the alpha
        # engine can give it a higher confidence floor and the UI can label it.
        if cl and any(s.get("tier") == 1 for s in cl.sources):
            ev["is_filing"] = True
            # Filings get a confidence floor: if event_confidence is too low, lift it.
            try:
                ev["event_confidence"] = max(float(ev.get("event_confidence", 0) or 0), 0.85)
                ev["sentiment_confidence"] = max(float(ev.get("sentiment_confidence", 0) or 0), 0.75)
            except Exception:
                pass
            # And drop any unconfirmed alpha cap — filings are confirmation by themselves.
            ev.pop("alpha_cap", None)

    return events

# LLM config
from config import GROQ_API_KEY, GROQ_MODEL, GROQ_BATCH_SIZE

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============ LLM CROSS-VERIFIER ============
class LLMCrossVerifier:
    """Uses Groq LLM to cross-verify uncertain keyword analysis results.

    Keyword engine is PRIMARY. LLM is only called when:
    1. Sentiment confidence < 0.55 (mixed signals)
    2. Event type = 'news' but magnitude > 5 (important but unclassified)
    3. Macro event detected (high-stakes, verify direction)

    ~10-15 LLM calls per scraper run. Well within Groq free tier (14,400/day).
    """

    SYSTEM_PROMPT = """You are a financial news analyst specializing in the Indian stock market (NSE/BSE).
Analyze the following news article and return a JSON object with EXACTLY this structure:
{
  "event_type": one of ["earnings", "merger", "policy", "order_win", "dividend", "supply", "insider", "news"],
  "sentiment": one of ["bullish", "bearish", "neutral"],
  "confidence": float 0.0-1.0 (how confident you are),
  "companies": [{"ticker": "NSE_TICKER", "sentiment": "bullish or bearish or neutral"}],
  "insight": "one-line trading rationale under 120 chars",
  "magnitude": integer 1-10 (market impact severity),
  "is_macro": true if this is a sector/economy-wide event rather than company-specific,
  "macro_theme": one of ["war_conflict", "oil_surge", "oil_crash", "rate_hike", "rate_cut", "recession_fear", "inflation", "rupee_weakness", "china_risk", "infra_push", "tech_boom"] or null
}

Known NSE tickers: INFY, TCS, WIPRO, LT, RELIANCE, HDFC, ICICIBANK, SBIN, BAJAJFINSV, MARUTI, TATASTEEL, JSWSTEEL, ADANIGREEN, ADANIPORTS, SUNPHARMA, DIVI, HINDUNILVR, ITC, NESTLEIND, AXISBANK, KNRCON, TECHM, HCLTECH, BAJAJ-AUTO, BHARTIARTL, CIPLA, LUPIN, POWERGRID, NTPC, COALINDIA, IOC, TATAMOTORS

Only include tickers from this list. If no specific company is affected, return companies as empty array.
Handle negation carefully: "did NOT beat" is bearish, "despite war, markets rallied" is bullish.
Return ONLY valid JSON, nothing else."""

    def __init__(self, db: TickwaveDB):
        self.db = db
        self.api_key = GROQ_API_KEY
        self.model = GROQ_MODEL
        self.daily_count = 0

    def is_available(self):
        """Check if Groq API is configured and under limits"""
        if not self.api_key or self.daily_count >= 14000:
            return False
        try:
            from groq_governor import governor
            return governor.can_spend("verification", est_tokens=700)
        except Exception:
            return True

    @staticmethod
    def needs_verification(event):
        """Determine if this event needs LLM cross-verification.
        Returns True for uncertain/high-stakes events only."""
        # Trigger 1: Low sentiment confidence (mixed keywords)
        if event.get('sentiment_confidence', 1.0) < 0.55:
            return True
        # Trigger 2: Unclassified but important
        if event.get('event_type') == 'news' and event.get('magnitude', 0) > 5:
            return True
        # Trigger 3: Macro event (high-stakes)
        if event.get('macro'):
            return True
        return False

    def verify_batch(self, events):
        """Cross-verify a batch of events with the LLM.
        Returns dict: event_id -> LLM result (or None if failed)."""
        if not self.is_available():
            return {}

        results = {}
        to_verify = [e for e in events if self.needs_verification(e)][:GROQ_BATCH_SIZE]

        if not to_verify:
            return {}

        logger.info(f"LLM cross-verifying {len(to_verify)} uncertain events...")

        try:
            from groq import Groq
            client = Groq(api_key=self.api_key)
        except ImportError:
            logger.warning("groq package not installed, skipping LLM verification")
            return {}
        except Exception as e:
            logger.warning(f"Groq client init failed: {e}")
            return {}

        try:
            from groq_governor import governor as _gov
        except Exception:
            _gov = None

        backoff = 1.0
        budget_stop_logged = False
        for event in to_verify:
            # Check cache first
            url_hash = hashlib.sha256(event.get('link', '').encode()).hexdigest()
            cached = self.db.get_cached_llm_result(url_hash)
            if cached:
                try:
                    results[event['event_id']] = json.loads(cached)
                    continue
                except json.JSONDecodeError:
                    pass

            # Centralized budget gate — kills the 429 retry-storm at source.
            if _gov is not None and not _gov.can_spend("verification", est_tokens=700):
                if not budget_stop_logged:
                    logger.warning("Groq verification budget exhausted — stopping batch early")
                    budget_stop_logged = True
                break

            try:
                text = f"Title: {event.get('title', '')}\nSummary: {event.get('summary', '')}"
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": text}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=500
                )
                self.daily_count += 1
                if _gov is not None:
                    try:
                        tokens = int(getattr(response.usage, "total_tokens", 700))
                    except Exception:
                        tokens = 700
                    _gov.record_spend("verification", tokens)
                backoff = 1.0  # reset on success

                raw = response.choices[0].message.content
                parsed = json.loads(raw)

                # Validate required fields
                if 'sentiment' in parsed and 'event_type' in parsed:
                    results[event['event_id']] = parsed
                    self.db.save_llm_result(url_hash, raw)
                    logger.info(f"  LLM verified: {event.get('title', '')[:50]}... -> {parsed.get('sentiment')}")

            except Exception as e:
                msg = str(e)
                if "429" in msg or "rate_limit" in msg.lower():
                    if _gov is not None:
                        _gov.record_429("verification")
                    logger.warning("  LLM 429 — backing off %.1fs and stopping batch", backoff)
                    time.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 60.0)
                    break  # don't keep hammering
                logger.warning(f"  LLM verify failed for event: {e}")
                continue

            # Brief pause to respect rate limits
            time.sleep(0.5)

        logger.info(f"LLM verified {len(results)}/{len(to_verify)} events (daily: {self.daily_count})")
        return results

    @staticmethod
    def apply_verification(event, llm_result):
        """Merge LLM result into event, overriding keyword analysis where LLM disagrees.
        When LLM agrees with keywords, boost confidence."""
        if not llm_result:
            return event

        keyword_sentiment = event.get('sentiment')
        llm_sentiment = llm_result.get('sentiment')

        if llm_sentiment and llm_sentiment != keyword_sentiment:
            # LLM disagrees — override
            event['sentiment'] = llm_sentiment
            event['sentiment_confidence'] = llm_result.get('confidence', 0.8)
            event['llm_corrected'] = True
        elif llm_sentiment == keyword_sentiment:
            # LLM agrees — boost confidence
            event['sentiment_confidence'] = min(0.98, event.get('sentiment_confidence', 0.5) + 0.15)

        # Override event type if LLM found something keywords missed
        if llm_result.get('event_type') and llm_result['event_type'] != 'news':
            if event.get('event_type') == 'news':
                event['event_type'] = llm_result['event_type']
                event['event_confidence'] = llm_result.get('confidence', 0.7)

        # Add companies LLM found that keywords missed
        llm_companies = [c['ticker'] for c in llm_result.get('companies', []) if 'ticker' in c]
        for ticker in llm_companies:
            if ticker not in event.get('companies', []):
                event['companies'].append(ticker)

        # Add insight
        if llm_result.get('insight'):
            event['insight'] = llm_result['insight']

        # Override magnitude if LLM thinks it's higher
        if llm_result.get('magnitude', 0) > event.get('magnitude', 0):
            event['magnitude'] = llm_result['magnitude']
            event['impact_score'] = llm_result['magnitude'] * 10

        # Macro theme
        if llm_result.get('is_macro') and llm_result.get('macro_theme'):
            if not event.get('macro'):
                event['macro'] = {
                    'theme': llm_result['macro_theme'],
                    'sectors': {},
                    'magnitude_boost': 1.2,
                    'match_score': 2
                }

        event['llm_verified'] = True
        return event


# ============ RSS FEED COLLECTOR ============
def _fetch_feed_with_retry(url: str, attempts: int = 2, base_delay: float = 1.0):
    """Parse an RSS/Atom feed with bounded retry on transient failure.

    Most upstream RSS hosts have intermittent timeouts; a single retry with
    a short delay recovers the majority. Caps at `attempts` total tries.
    Returns the feedparser result (or a parsed object with empty .entries)
    so callers can treat it uniformly.
    """
    last_exc = None
    for i in range(attempts):
        try:
            f = feedparser.parse(url)
            # feedparser returns an object even on network failure; check
            # whether we actually got entries or a bozo flag.
            if getattr(f, 'entries', None):
                return f
            # No entries — might be empty feed (legitimate) or transient.
            # Only retry if there's a bozo exception.
            bozo = getattr(f, 'bozo_exception', None)
            if not bozo:
                return f
            last_exc = bozo
        except Exception as exc:
            last_exc = exc
        if i < attempts - 1:
            time.sleep(base_delay * (2 ** i))
    if last_exc:
        logger.debug(f"feed fetch gave up after {attempts}: {url[:80]} → {last_exc}")
    # Return whatever the last parse produced (likely empty entries)
    try:
        return feedparser.parse(url)
    except Exception:
        class _Empty:
            entries = []
        return _Empty()


class RSSFeedCollector:
    """Collects news from RSS feeds, Google News RSS, and NewsAPI"""

    def __init__(self, db: TickwaveDB):
        self.sources = RSS_SOURCES
        self.db = db
        self.articles = []

    def _fetch_one_source(self, source_name: str, feed_url: str) -> List[Dict]:
        """Pure fetcher (no DB writes). Used by the parallel pool."""
        try:
            feed = _fetch_feed_with_retry(feed_url)
            return [(source_name, e) for e in feed.entries[:MAX_ARTICLES_PER_SOURCE]]
        except Exception as exc:
            logger.warning(f"  Failed to fetch {source_name}: {exc}")
            return []

    def fetch_feeds(self) -> List[Dict]:
        """Fetch all news sources and return deduplicated articles.

        RSS feeds are fetched in parallel with bounded concurrency (8 workers)
        to compress wall-clock time without hammering hosts. The previous
        sequential loop took 27 × ~2s ≈ 1 min in the worst case; this caps
        the dominant cost at roughly max-source-latency.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        logger.info("Fetching news from all sources...")
        all_articles = []

        # 1. Standard RSS feeds — parallel fetch, then sequential dedup write.
        # DB writes stay sequential because is_article_seen + mark_article_seen
        # form a check-then-write pair that races under threading.
        per_source_counts: Dict[str, int] = {}
        max_workers = int(os.getenv('RSS_PARALLEL_WORKERS', '8'))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._fetch_one_source, name, url): name
                for name, url in self.sources.items()
            }
            for fut in as_completed(futures):
                source_name = futures[fut]
                try:
                    pairs = fut.result()
                except Exception as exc:
                    logger.warning(f"  Failed to fetch {source_name}: {exc}")
                    continue
                count = 0
                for src, entry in pairs:
                    article = self._parse_entry(entry, src)
                    if article and not self.db.is_article_seen(article['link'], article['title']):
                        all_articles.append(article)
                        self.db.mark_article_seen(article['link'], article['title'], src, article.get('summary'))
                        count += 1
                if count:
                    per_source_counts[source_name] = count
        for src, n in per_source_counts.items():
            logger.info(f"  {src}: {n} new articles")

        # 2. Google News RSS (free, no API key)
        # Build the query plan for this cycle:
        #   (a) all generic broad queries  — every cycle
        #   (b) rotating slice of SECTOR_ROTATION_QUERIES — broadens sector coverage
        #   (c) rotating slice of TICKER_NEWS_ROTATION — explicit small/mid cap focus
        #       sorted last past 2 days (Google News supports `when:2d`)
        sector_slice = _next_rotation_slice(SECTOR_ROTATION_QUERIES,
                                             GOOGLE_NEWS_SECTOR_PER_CYCLE, 'sector_idx')
        ticker_slice = _next_rotation_slice(TICKER_NEWS_ROTATION,
                                             TICKERS_PER_CYCLE, 'ticker_idx')

        # Per-ticker queries — use the company short-name + "stock" hint.
        # `when:2d` filter forces Google to return news from the last 2 days only,
        # which keeps recency tight. Falls back to all-time if Google ignores the
        # operator (rare).
        ticker_queries = []
        for tk in ticker_slice:
            company = STOCK_UNIVERSE.get(tk, {}).get('name', tk)
            short = company.split(' ')[0] if company else tk
            ticker_queries.append((f'tk:{tk}', f'"{short}" {tk} stock NSE when:2d'))

        plan = (
            [('broad', q) for q in GOOGLE_NEWS_QUERIES] +
            [('sector', q) for q in sector_slice] +
            ticker_queries
        )

        for kind, query in plan:
            try:
                url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
                feed = _fetch_feed_with_retry(url)
                count = 0
                for entry in feed.entries[:GOOGLE_NEWS_ENTRIES_PER_QUERY]:
                    article = self._parse_entry(entry, f'google_news')
                    if article and not self.db.is_article_seen(article['link'], article['title']):
                        all_articles.append(article)
                        self.db.mark_article_seen(article['link'], article['title'], 'google_news', article.get('summary'))
                        count += 1
                if count > 0:
                    logger.info(f"  Google News [{kind}] ({query[:32]}): {count} new")
            except Exception as e:
                logger.warning(f"  Google News query failed [{kind}] {query[:32]}: {e}")

        # 3. NewsAPI (optional, free tier: 100 req/day)
        if NEWSAPI_KEY:
            try:
                resp = requests.get(
                    'https://newsapi.org/v2/everything',
                    params={
                        'q': NEWSAPI_QUERY,
                        'language': 'en',
                        'sortBy': 'publishedAt',
                        'pageSize': 20,
                        'apiKey': NEWSAPI_KEY
                    },
                    timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 200:
                    data = resp.json()
                    count = 0
                    for item in data.get('articles', []):
                        article = {
                            'source': f"newsapi_{item.get('source', {}).get('id', 'unknown')}",
                            'title': item.get('title', ''),
                            'summary': item.get('description', '') or '',
                            'link': item.get('url', ''),
                            'published': item.get('publishedAt', datetime.now().isoformat()),
                            'timestamp': datetime.now().isoformat()
                        }
                        if article['title'] and not self.db.is_article_seen(article['link'], article['title']):
                            all_articles.append(article)
                            self.db.mark_article_seen(article['link'], article['title'], 'newsapi', article.get('summary'))
                            count += 1
                    if count > 0:
                        logger.info(f"  NewsAPI: {count} new articles")
                else:
                    logger.warning(f"  NewsAPI returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"  NewsAPI failed: {e}")

        self.articles = all_articles
        logger.info(f"Total new articles collected: {len(all_articles)}")
        return all_articles

    # Non-financial topics to reject immediately
    JUNK_KEYWORDS = [
        'cricket', 'ipl', 'football', 'soccer', 'tennis', 'hockey', 'sports',
        'world cup', 'olympic', 'match score', 'batting', 'bowling', 'wicket',
        'bollywood', 'amitabh', 'shah rukh', 'salman khan', 'actress', 'actor',
        'movie review', 'film review', 'box office', 'celebrity', 'entertainment',
        'recipe', 'cooking', 'fashion', 'beauty tips', 'horoscope', 'astrology',
        'zodiac', 'weather forecast', 'tourist', 'vacation', 'travel guide',
        'wedding', 'relationship', 'dating', 'fitness tips', 'yoga poses',
        'big boss', 'bigg boss', 'kbc', 'reality show', 'tv show', 'serial',
        'mattel', 'toy', 'action figure', 'anime', 'manga', 'gaming', 'esports',
        'playstation', 'xbox', 'nintendo', 'music album', 'concert', 'singer',
    ]

    # Financial signal words — at least one must be present
    FINANCE_SIGNALS = [
        'stock', 'share', 'market', 'nse', 'bse', 'nifty', 'sensex', 'trading',
        'earnings', 'revenue', 'profit', 'loss', 'quarter', 'gdp', 'inflation',
        'rbi', 'sebi', 'fed', 'rate', 'bond', 'yield', 'fund', 'investor',
        'ipo', 'merger', 'acquisition', 'dividend', 'buyback', 'fii', 'dii',
        'rupee', 'dollar', 'forex', 'crude', 'gold', 'silver', 'commodity',
        'bank', 'insurance', 'fintech', 'pharma', 'auto', 'infra', 'steel',
        'oil', 'energy', 'power', 'telecom', 'real estate', 'cement',
        'target price', 'buy', 'sell', 'hold', 'upgrade', 'downgrade',
        'bullish', 'bearish', 'rally', 'crash', 'correction', 'volatility',
        'sector', 'index', 'portfolio', 'mutual fund', 'etf', 'derivative',
        'company', 'business', 'economy', 'fiscal', 'monetary', 'trade war',
        'sanction', 'tariff', 'export', 'import', 'supply chain',
        'rs', 'crore', 'lakh', 'billion', 'million',
    ]

    @classmethod
    def _is_financial(cls, text):
        """Check if article text is about finance/markets, not sports/entertainment"""
        t = text.lower()
        # Reject if junk keyword found
        if any(kw in t for kw in cls.JUNK_KEYWORDS):
            return False
        # Accept if at least one finance signal
        return any(kw in t for kw in cls.FINANCE_SIGNALS)

    @classmethod
    def _parse_entry(cls, entry, source_name):
        """Parse a feedparser entry, rejecting non-financial articles"""
        title = entry.get('title', '').strip()
        link = entry.get('link', '').strip()
        if not title or not link:
            return None
        summary = (entry.get('summary', '') or entry.get('description', '') or '')[:500]
        text = f"{title} {summary}"

        # Filter out sports, entertainment, lifestyle junk
        if not cls._is_financial(text):
            return None

        return {
            'source': source_name,
            'title': title,
            'summary': summary,
            'link': link,
            'published': entry.get('published', datetime.now().isoformat()),
            'timestamp': datetime.now().isoformat()
        }


# ============ MARKET DATA COLLECTOR ============
class MarketDataCollector:
    """Collects stock prices via yfinance batch download"""

    def __init__(self, tickers: List[str]):
        self.tickers = tickers
        self.stock_data = {}

    def fetch_stock_data(self) -> Dict:
        """Fetch stock data using batch download for speed"""
        logger.info(f"Fetching market data for {len(self.tickers)} stocks...")
        stock_data = {}

        # Build NSE symbols
        symbols = [f"{t}.NS" for t in self.tickers]

        try:
            # Batch download - much faster than individual calls
            data = yf.download(symbols, period='30d', group_by='ticker',
                               progress=False, threads=True)

            for ticker in self.tickers:
                symbol = f"{ticker}.NS"
                try:
                    if len(self.tickers) == 1:
                        hist = data
                    else:
                        if symbol not in data.columns.get_level_values(0):
                            continue
                        hist = data[symbol]

                    if hist.empty or hist['Close'].dropna().empty:
                        continue

                    prices = hist['Close'].dropna().values
                    if len(prices) < 2:
                        continue

                    returns = (prices[-1] - prices[0]) / prices[0] * 100
                    volatility = pd.Series(prices).pct_change().std() * 100

                    stock_data[ticker] = {
                        'ticker': ticker,
                        'company': STOCK_COMPANIES.get(ticker, ticker),
                        'current_price': float(prices[-1]),
                        'change_pct': float(returns),
                        'volatility': float(volatility) if not pd.isna(volatility) else 0,
                        'volume': float(hist['Volume'].dropna().iloc[-1]) if 'Volume' in hist.columns else 0,
                        'timestamp': datetime.now().isoformat()
                    }
                except Exception as e:
                    logger.warning(f"  Failed to process {ticker}: {e}")

        except Exception as e:
            logger.warning(f"Batch download failed, trying individual: {e}")
            # Fallback to individual downloads
            for ticker in self.tickers:
                try:
                    symbol = f"{ticker}.NS"
                    stock = yf.Ticker(symbol)
                    hist = stock.history(period='30d')
                    if not hist.empty:
                        prices = hist['Close'].values
                        returns = (prices[-1] - prices[0]) / prices[0] * 100
                        volatility = hist['Close'].pct_change().std() * 100
                        stock_data[ticker] = {
                            'ticker': ticker,
                            'company': STOCK_COMPANIES.get(ticker, ticker),
                            'current_price': float(prices[-1]),
                            'change_pct': float(returns),
                            'volatility': float(volatility),
                            'volume': float(hist['Volume'].iloc[-1]) if 'Volume' in hist.columns else 0,
                            'timestamp': datetime.now().isoformat()
                        }
                except Exception as e:
                    logger.warning(f"  Failed to fetch {ticker}: {e}")

        self.stock_data = stock_data
        logger.info(f"Fetched data for {len(stock_data)} stocks")
        return stock_data


# ============ POLICY PARSER (NLP ENGINE) ============
class PolicyParser:
    """NLP-based event extraction from news articles"""

    def __init__(self, llm_verifier=None):
        self.events = []
        self.llm_verifier = llm_verifier

    def classify_event_type(self, text: str) -> Tuple[str, float]:
        """Classify event type using keyword matching"""
        text_lower = text.lower()
        scores = {}

        for event_type, keywords in EVENT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            scores[event_type] = score

        if max(scores.values()) > 0:
            event_type = max(scores, key=scores.get)
            confidence = min(scores[event_type] / max(len(EVENT_KEYWORDS[event_type]) * 0.3, 1), 1.0)
            return event_type, confidence

        return 'news', 0.3

    def analyze_sentiment(self, text: str) -> Tuple[str, float]:
        """Analyze sentiment: bullish, bearish, or neutral"""
        text_lower = text.lower()

        positive_score = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
        negative_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)

        if positive_score > negative_score:
            sentiment = 'bullish'
            confidence = min((positive_score / (positive_score + negative_score + 1)), 1.0)
        elif negative_score > positive_score:
            sentiment = 'bearish'
            confidence = min((negative_score / (positive_score + negative_score + 1)), 1.0)
        else:
            sentiment = 'neutral'
            confidence = 0.5

        return sentiment, confidence

    def analyze_sentiment_for_entity(self, text: str, entity: str) -> Tuple:
        """Entity-aware sentiment: analyze sentiment in the context of a specific company.

        Splits text into sentences and weights sentences that mention the entity
        more heavily. This prevents "RIL reports strong earnings amid oil sector
        weakness" from being scored as neutral for RIL.

        Falls back to full-text sentiment if no entity-specific sentences found.
        """
        import re
        sentences = re.split(r'[.!?;]+', text)
        if not sentences or not entity:
            return self.analyze_sentiment(text)

        # Find entity names to match (ticker + company name)
        entity_names = [entity.lower()]
        company = STOCK_COMPANIES.get(entity, '')
        if company:
            # Add first word of company name (e.g., "Reliance" from "Reliance Industries")
            short = company.split(' ')[0].lower()
            if len(short) > 3:
                entity_names.append(short)

        # Score sentences mentioning the entity separately
        entity_pos = 0
        entity_neg = 0
        entity_sentence_count = 0
        other_pos = 0
        other_neg = 0

        for sentence in sentences:
            s = sentence.lower().strip()
            if not s:
                continue

            pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in s)
            neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in s)

            mentions_entity = any(name in s for name in entity_names)
            if mentions_entity:
                entity_pos += pos
                entity_neg += neg
                entity_sentence_count += 1
            else:
                other_pos += pos
                other_neg += neg

        # If we found entity-specific sentences, weight them 3x
        if entity_sentence_count > 0:
            total_pos = entity_pos * 3 + other_pos
            total_neg = entity_neg * 3 + other_neg
        else:
            # No entity-specific sentences, fall back to full text
            total_pos = entity_pos + other_pos
            total_neg = entity_neg + other_neg

        if total_pos > total_neg:
            sentiment = 'bullish'
            confidence = min((total_pos / (total_pos + total_neg + 1)), 1.0)
        elif total_neg > total_pos:
            sentiment = 'bearish'
            confidence = min((total_neg / (total_pos + total_neg + 1)), 1.0)
        else:
            sentiment = 'neutral'
            confidence = 0.5

        # Return enrichment data as 4-tuple so caller can pass to EventData
        total_sents = max(1, len([s for s in sentences if s.strip()]))
        return sentiment, confidence, entity_sentence_count, total_sents

    # Words that look like tickers but are just common English
    # Only blacklist tickers that are VERY common English words (2-3 letters)
    # and genuinely cause false matches in every article
    TICKER_BLACKLIST = {
        'IT', 'LT', 'BE', 'DO', 'GO', 'ON', 'OR', 'US', 'AM', 'AN', 'IF',
        'ALL', 'YES', 'CAN', 'GET', 'MAN', 'ADD', 'BIG', 'LOW', 'HIGH',
    }

    # Foreign companies that should NOT trigger Indian stock signals
    FOREIGN_KEYWORDS = ['netflix', 'warner bros', 'disney', 'amazon prime', 'apple tv',
                        'google', 'microsoft', 'tesla', 'nvidia', 'meta', 'openai',
                        'spacex', 'samsung', 'toyota', 'sony']

    # Listicle headlines like "5 Speciality Chemical Stocks", "Top 10 Banking
    # Shares to Buy", "7 Defence Stocks for 2025" \u2014 the descriptor word is an
    # adjective, not a company. Skip first-word short-name matches in these.
    _LISTICLE_RE = re.compile(
        r'^\s*(top\s+)?\d+\s+\w+(\s+\w+){0,4}?\s+(stocks?|shares?|companies|picks?|bets?)\b',
        re.IGNORECASE,
    )

    def extract_entities(self, text: str) -> Dict:
        """Extract companies from text using the full 500-stock universe.

        Prevents false matches for foreign articles while catching
        mentions of any NSE/BSE listed company.

        Defenses against false positives:
          1. Ambiguous tickers (those whose lowercase form is a common English
             word \u2014 SPECIALITY, POWER, GLOBAL, CAPITAL \u2026) require ALL-CAPS
             spelling in the source.  "Speciality Chemical Stocks" no longer
             trips SPECIALITY (Speciality Restaurants).
          2. Listicle headlines ("5 X Y Stocks", "Top 10 Banking Shares \u2026")
             skip the first-word short-name path entirely.
          3. Short-name matches (case-insensitive first-word) require that the
             match appears as a proper noun (capitalised in the original text),
             except for curated overrides (tata, adani, \u2026) where the lowercase
             form is itself unambiguous.
        """
        entities = {'companies': []}
        text_lower = text.lower()

        foreign_focus = sum(1 for kw in self.FOREIGN_KEYWORDS if kw in text_lower)
        is_foreign_article = foreign_focus >= 2

        # Detect listicle headlines \u2014 first ~120 chars are the headline body
        head = text[:120]
        is_listicle = bool(self._LISTICLE_RE.match(head))

        # \u2500\u2500 Pass 1: ticker scan \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        for ticker in UNIVERSE_TICKERS:
            if ticker in self.TICKER_BLACKLIST or len(ticker) < 3:
                continue
            pattern = r'\b' + re.escape(ticker) + r'\b'
            # Ambiguous tickers must match in ALL CAPS only (case-sensitive).
            # Everything else stays case-insensitive.
            flags = 0 if ticker in AMBIGUOUS_TICKERS else re.IGNORECASE
            if re.search(pattern, text, flags):
                if is_foreign_article:
                    fin_pattern = r'\b' + re.escape(ticker) + r'\b.{0,20}(share|stock|NSE|BSE|target|buy|sell|rating|Rs|INR|\u20b9)'
                    if not re.search(fin_pattern, text, re.IGNORECASE):
                        continue
                entities['companies'].append(ticker)

        # \u2500\u2500 Pass 2: short-name (first-word) scan \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if not is_foreign_article:
            for short_name, ticker in UNIVERSE_SHORT_NAMES.items():
                if ticker in entities['companies'] or ticker in self.TICKER_BLACKLIST:
                    continue
                # Word-bounded match (NOT substring) \u2014 fixes "indus" false-matching
                # inside "industries", "tata" inside "tatami", etc.
                m = re.search(r'\b' + re.escape(short_name) + r'\b', text, re.IGNORECASE)
                if not m:
                    continue
                # Skip listicle false-positives \u2014 descriptor words \u2260 companies
                if is_listicle and m.start() < len(head):
                    continue
                # Require the short-name to appear as a proper noun (capitalised)
                # in the original text. Catches "5 Power Stocks\u2026" without losing
                # "Power Finance Corp said today\u2026". Curated overrides (tata,
                # adani, etc.) still pass because they appear capitalised in news.
                if not m.group(0)[0].isupper():
                    continue
                entities['companies'].append(ticker)

        return entities

    # Domestic Indian stock news keywords — these should NOT generate geo events
    # (they're not geopolitical, they're just local market chatter)
    DOMESTIC_NOISE = ['target price', 'buy rating', 'sell rating', 'stock pick',
                      'multibagger', 'penny stock', 'intraday', 'technical analysis',
                      'support level', 'resistance level', 'moving average']

    def classify_geo_event(self, text: str) -> Optional[Dict]:
        """Classify GEOPOLITICAL relevance — only for genuinely global events.

        Filters out: domestic Indian stock tips, analyst recommendations,
        and technical analysis articles. Only tags events that have real
        geographic significance (war, trade policy, central bank decisions, etc.)
        """
        text_lower = text.lower()

        # Skip domestic stock noise — these aren't geo events
        if any(kw in text_lower for kw in self.DOMESTIC_NOISE):
            return None

        best_country = None
        best_score = 0

        for country, info in GEO_KEYWORDS.items():
            score = 0
            for kw in info['keywords']:
                # Word boundary for short keywords, substring for multi-word
                if len(kw) <= 3:
                    if re.search(r'\b' + re.escape(kw) + r'\b', text_lower):
                        score += 1
                else:
                    if kw in text_lower:
                        score += 1

            if score > best_score:
                best_score = score
                best_country = country

        # Require at least 2 keyword matches for geo classification
        # (prevents "us stocks" from matching on just "us")
        if best_country and best_score >= 2:
            info = GEO_KEYWORDS[best_country]
            return {
                'country': best_country,
                'latitude': info['lat'],
                'longitude': info['lng']
            }
        return None

    def detect_macro_theme(self, text: str) -> Optional[Dict]:
        """Detect macro/sector-level themes (war, oil shock, rate change, etc.)

        Uses word-boundary matching to avoid false positives like
        'warns' matching 'war' or 'conflict of interest' matching 'conflict'.
        Requires at least 2 keyword matches for macro detection.
        """
        text_lower = text.lower()
        best_match = None
        best_score = 0

        for theme_name, theme in MACRO_SECTOR_MAP.items():
            score = 0
            for kw in theme['keywords']:
                # Use word boundary matching for single words, substring for multi-word
                if ' ' in kw:
                    if kw in text_lower:
                        score += 1
                else:
                    if re.search(r'\b' + re.escape(kw) + r'\b', text_lower):
                        score += 1
            if score > best_score:
                best_score = score
                best_match = theme_name

        # Require at least 2 keyword matches for macro detection
        if best_match and best_score >= 2:
            theme = MACRO_SECTOR_MAP[best_match]
            return {
                'theme': best_match,
                'sectors': theme['sectors'],
                'magnitude_boost': theme['magnitude_boost'],
                'match_score': best_score
            }
        return None

    def expand_macro_to_companies(self, macro: Dict) -> List[Dict]:
        """Given a macro theme, return list of (ticker, sentiment) for all
        stocks in affected sectors.

        e.g., war_conflict → ENERGY:bullish → [RELIANCE:bullish, IOC:bullish, ...]
        """
        results = []
        for sector, sentiment in macro['sectors'].items():
            if sentiment == 'neutral':
                continue
            tickers = SECTOR_STOCKS.get(sector, [])
            for ticker in tickers:
                results.append({'ticker': ticker, 'sentiment': sentiment, 'sector': sector})
        return results

    def estimate_magnitude(self, text: str, sentiment_confidence: float) -> int:
        """Estimate event magnitude (1-10 scale)"""
        text_length_factor = min(len(text) / 500, 1.0)
        high_impact_phrases = ['critical', 'major deal', 'significant', 'unprecedented',
                               'historic', 'record high', 'record low', 'crash',
                               'crisis', 'sanctions', 'embargo']
        text_lower = text.lower()
        high_impact_count = sum(1 for kw in high_impact_phrases if kw in text_lower)

        magnitude = int(2 + (sentiment_confidence * 3) + (text_length_factor * 2) + (high_impact_count * 1))
        return min(max(magnitude, 1), 10)

    def parse_articles(self, articles: List[Dict]) -> List[Dict]:
        """Parse articles and extract events.

        For articles that mention specific companies → company-level events.
        For articles with no company match but macro theme detected →
        expand into sector-level events affecting all stocks in that sector.
        """
        logger.info(f"Parsing {len(articles)} articles...")
        events = []

        for article in articles:
            text = f"{article['title']} {article['summary']}"

            event_type, event_conf = self.classify_event_type(text)
            sentiment, sent_conf = self.analyze_sentiment(text)
            entities = self.extract_entities(text)
            magnitude = self.estimate_magnitude(text, sent_conf)
            geo = self.classify_geo_event(text)
            macro = self.detect_macro_theme(text)

            # If no companies matched but macro theme detected,
            # expand the macro theme into sector stocks
            if not entities['companies'] and macro:
                expanded = self.expand_macro_to_companies(macro)
                if expanded:
                    boosted_mag = min(8, int(magnitude * macro['magnitude_boost']))  # Cap at 8 for macro
                    entities['companies'] = [e['ticker'] for e in expanded]
                    if 'war' in (macro.get('theme', '')):
                        event_type = 'supply'
                    elif 'rate' in (macro.get('theme', '')):
                        event_type = 'policy'
                    magnitude = boosted_mag
                    # Macro signals get lower confidence (inferred, not directly mentioned)
                    event_conf = min(event_conf, 0.55)
                    sent_conf = min(sent_conf, 0.55)
                    logger.info(f"  Macro [{macro['theme']}] → {len(expanded)} stocks across {list(macro['sectors'].keys())}")

            event = {
                'id': len(events),
                'event_id': hashlib.md5(f"{article['link']}_{article['title']}".encode()).hexdigest()[:16],
                'title': article['title'],
                'summary': article['summary'][:300],
                'source': article['source'],
                'event_type': event_type,
                'event_confidence': float(event_conf),
                'sentiment': sentiment,
                'sentiment_confidence': float(sent_conf),
                'magnitude': magnitude,
                'impact_score': int(magnitude * 10),
                'companies': entities['companies'],
                'macro': macro,  # Preserve macro info for signal engine
                'geo': geo,
                'timestamp': datetime.now().isoformat(),
                'link': article['link'],
                'published': article.get('published', '')
            }

            events.append(event)

        self.events = events
        macro_count = sum(1 for e in events if e.get('macro'))
        logger.info(f"Extracted {len(events)} events ({sum(1 for e in events if e['companies'])} with companies, {macro_count} macro)")

        # LLM cross-verification: only verify uncertain/high-stakes events
        if self.llm_verifier and self.llm_verifier.is_available():
            llm_results = self.llm_verifier.verify_batch(events)
            if llm_results:
                for event in events:
                    llm_result = llm_results.get(event['event_id'])
                    if llm_result:
                        LLMCrossVerifier.apply_verification(event, llm_result)

        return events


# ============ SIGNAL ENGINE ============
class SignalEngine:
    """Converts events to trading signals with alpha scoring"""

    def __init__(self, parser: PolicyParser = None):
        self.regime_detector = RegimeDetector()
        self.scoring_engine = AlphaScoringEngine()
        self.validator = SignalValidator()
        self.predictor = PredictionEngine()
        self.parser = parser or PolicyParser()

    def generate_signals(self, events: List[Dict], stock_data: Dict) -> Tuple[List[Dict], MarketRegime, float]:
        """Convert events to trading signals. Returns (signals, regime, regime_strength)"""
        logger.info("Generating trading signals...")
        signals = []

        # Detect current market regime
        market_data = self._build_market_data(stock_data)
        regime = self.regime_detector.detect_regime(market_data)
        regime_strength = self.regime_detector.get_regime_strength(regime, market_data)
        logger.info(f"  Market Regime: {regime.value} (strength: {regime_strength:.0%})")

        # Pre-compute sector-level momentum for relative strength analysis
        sector_returns = self._compute_sector_momentum(stock_data)

        for event in events:
            # If the event has no specific ticker but is a macro/policy event
            # with sector impact (e.g. RBI rate cut → BFSI sector), expand it
            # to the top stocks in each affected sector so the signal stream
            # surfaces it. Without this expansion, RBI/SEBI/state-policy
            # events get extracted but never generate a signal.
            if not event['companies']:
                macro = event.get('macro') or {}
                sector_impacts = macro.get('sectors') or {}
                if not sector_impacts:
                    continue
                # Expand to top-N stocks per affected sector. Cap per-sector
                # to avoid 50 signals from a single rate cut.
                MAX_STOCKS_PER_SECTOR = int(os.getenv('MACRO_EXPANSION_MAX', '3'))
                expanded: List[str] = []
                for sec, sec_sentiment in sector_impacts.items():
                    if not sec_sentiment or sec_sentiment == 'neutral':
                        continue
                    sec_stocks = SECTOR_STOCKS.get(sec, []) or []
                    # Prefer stocks present in stock_data (we have current prices)
                    candidates = [s for s in sec_stocks if s in stock_data]
                    expanded.extend(candidates[:MAX_STOCKS_PER_SECTOR])
                if not expanded:
                    continue
                # Discount magnitude on derived signals so direct-hit events
                # outrank sector-bucket inferences. Mark for downstream UI.
                event = {**event, 'companies': expanded,
                         'magnitude': float(event.get('magnitude', 50)) * 0.65,
                         '_macro_expanded': True}

            for company in event['companies']:
                try:
                    stock_info = stock_data.get(company, {})
                    entry_price = stock_info.get('current_price', 0)

                    # Determine sentiment for this specific company.
                    # For macro events (war, oil, etc.), use the macro→sector mapping
                    # which knows the directional impact per sector.
                    # For company-specific events, use entity-aware NLP.
                    macro = event.get('macro')
                    entity_mentions_count = 1
                    total_sentences_count = 5
                    if macro and macro.get('sectors'):
                        stock_sector = STOCK_SECTORS.get(company, '')
                        macro_sent = macro['sectors'].get(stock_sector)
                        if macro_sent and macro_sent != 'neutral':
                            company_sentiment = macro_sent
                            company_sent_conf = min(0.6 + macro.get('match_score', 1) * 0.1, 0.95)
                        else:
                            company_sentiment = event['sentiment']
                            company_sent_conf = event['sentiment_confidence']
                    else:
                        full_text = f"{event.get('title', '')} {event.get('summary', '')}"
                        result = self.parser.analyze_sentiment_for_entity(full_text, company)
                        company_sentiment, company_sent_conf = result[0], result[1]
                        if len(result) == 4:
                            entity_mentions_count = result[2]
                            total_sentences_count = result[3]

                    # Look up prior magnitude for this ticker (SurpriseFactor)
                    prior_mag = 0.0
                    try:
                        from database_schema import TickwaveDB, _execute_query
                        _db = TickwaveDB()
                        prior_rows = _execute_query(_db.conn,
                            "SELECT alpha_score FROM signals WHERE ticker=? AND event_type=? ORDER BY created_at DESC LIMIT 1",
                            (company, event['event_type']), fetch=True)
                        if prior_rows:
                            prior_mag = float(prior_rows[0]['alpha_score'] or 0) / 10.0  # alpha→magnitude proxy
                        _db.close()
                    except Exception:
                        pass

                    event_data = EventData(
                        event_type=event['event_type'],
                        ticker=company,
                        sentiment=company_sentiment,
                        magnitude=event['magnitude'],
                        confidence=company_sent_conf,
                        description=event['summary'],
                        source_count=event.get('source_count', 1),
                        entity_mentions=entity_mentions_count,
                        total_sentences=total_sentences_count,
                        prior_magnitude=prior_mag,
                    )

                    # Build per-stock context with sector awareness
                    stock_ctx = self._build_stock_context(company, stock_data, sector_returns)

                    alpha_score = self.scoring_engine.calculate_alpha_score(
                        event=event_data,
                        market_data=market_data,
                        regime=regime,
                        market_cap_category=self._get_market_cap_category(company),
                        days_old=0,
                        stock_context=stock_ctx
                    )
                    alpha_breakdown = getattr(AlphaScoringEngine, '_last_breakdown', {})

                    # Cross-source confirmation cap: unconfirmed/single-tipster
                    # events can't fire above alpha=65 even if scoring loves them.
                    cap = event.get('alpha_cap')
                    if cap is not None and alpha_score > cap:
                        alpha_score = cap
                        if isinstance(alpha_breakdown, dict):
                            alpha_breakdown = {**alpha_breakdown, 'capped_for_unconfirmed': True}

                    is_valid, validation = self.validator.is_valid_signal(
                        alpha_score=alpha_score,
                        confidence=event['sentiment_confidence'],
                        regime=regime
                    )

                    # Generate predictions for all horizons
                    predictions = {}
                    try:
                        from prediction_intervals import compute_interval as _ci
                    except Exception:
                        _ci = None
                    _db_for_ci = None
                    if _ci:
                        try:
                            from database_schema import TickwaveDB as _DB
                            _db_for_ci = _DB()
                        except Exception:
                            _db_for_ci = None
                    for horizon in ['1D', '3D', '20D']:
                        pred = self.predictor.predict_return(
                            event_type=event['event_type'],
                            alpha_score=alpha_score,
                            volatility=market_data.volatility,
                            regime=regime,
                            sentiment=company_sentiment,
                            horizon=horizon
                        )
                        return_pct = float(pred['predicted_return_pct'])
                        rec = {
                            'return_pct': return_pct,
                            'predicted_return_pct': return_pct,  # alias for UI compat
                            'confidence': float(pred['confidence'])
                        }
                        if _ci and _db_for_ci is not None:
                            try:
                                interval = _ci(_db_for_ci, horizon, return_pct,
                                               volatility=market_data.volatility)
                                rec['interval'] = interval
                                rec['range_68'] = [interval['lower68'], interval['upper68']]
                                rec['range_95'] = [interval['lower95'], interval['upper95']]
                            except Exception:
                                pass
                        predictions[horizon] = rec
                    if _db_for_ci is not None:
                        try:
                            _db_for_ci.close()
                        except Exception:
                            pass

                    # Price targets
                    price_targets = {}
                    if entry_price > 0:
                        price_targets = self.predictor.predict_price_targets(
                            entry_price=entry_price,
                            alpha_score=alpha_score,
                            predicted_returns={h: p['return_pct'] for h, p in predictions.items()}
                        )

                    signal = {
                        'ticker': company,
                        'company': STOCK_COMPANIES.get(company, company),
                        'event_type': event['event_type'],
                        'event_id': f"{company}_{event['event_type']}_{event['event_id']}",
                        'alpha_score': float(alpha_score),
                        'confidence': float(company_sent_conf),
                        'regime': regime.value,
                        'regime_strength': float(regime_strength),
                        'valid': bool(is_valid),
                        'sentiment': company_sentiment,
                        'article_sentiment': event['sentiment'],
                        'sector': stock_ctx.sector if stock_ctx else '',
                        'relative_strength': float(stock_ctx.relative_strength) if stock_ctx else 0,
                        'magnitude': event['magnitude'],
                        'impact_score': event['impact_score'],
                        'entry_price': float(entry_price),
                        'predictions': predictions,
                        'price_targets': price_targets,
                        'validation': {
                            'alpha_margin': float(validation['alpha_margin']),
                            'confidence_margin': float(validation['confidence_margin'])
                        },
                        'headline': event['title'],
                        'source': event['source'],
                        'link': event['link'],
                        'geo': event.get('geo'),
                        'alpha_breakdown': alpha_breakdown,
                        'source_tier': event.get('source_tier'),
                        'tier_weight': event.get('tier_weight'),
                        'confirmation': event.get('confirmation'),
                        'freshness_label': event.get('freshness_label'),
                        'source_count': event.get('source_count', 1),
                        'distinct_sources': event.get('distinct_sources', 1),
                        'news_velocity_per_hour': event.get('news_velocity_per_hour', 0),
                        'high_velocity': event.get('high_velocity', False),
                        'timestamp': datetime.now().isoformat()
                    }

                    signals.append(signal)

                except Exception as e:
                    logger.warning(f"  Failed to score {company}: {e}")
                    continue

        logger.info(f"Generated {len(signals)} signals ({sum(1 for s in signals if s['valid'])} valid)")
        return signals, regime, regime_strength

    @staticmethod
    def _build_market_data(stock_data: Dict) -> MarketData:
        """Aggregate stock data into market-wide conditions"""
        if not stock_data:
            return MarketData(
                volatility=0.15, price_change_1d=0.0,
                momentum_score=0.0, trend_strength=0.5, sector_momentum=0.0
            )

        volatilities = [s.get('volatility', 0) / 100 for s in stock_data.values()]
        changes = [s.get('change_pct', 0) / 100 for s in stock_data.values()]

        avg_volatility = sum(volatilities) / len(volatilities) if volatilities else 0.15
        avg_change = sum(changes) / len(changes) if changes else 0.0

        positive_changes = sum(1 for c in changes if c > 0)
        momentum = (positive_changes / len(changes) - 0.5) * 2 if changes else 0
        trend_strength = min(abs(momentum) * 1.5, 1.0)

        return MarketData(
            volatility=avg_volatility,
            price_change_1d=avg_change,
            momentum_score=momentum,
            trend_strength=trend_strength,
            sector_momentum=momentum * 0.7
        )

    @staticmethod
    def _compute_sector_momentum(stock_data: Dict) -> Dict[str, float]:
        """Compute per-sector average return from stock data.
        Returns dict: sector_name -> avg change_pct"""
        sector_returns = {}
        sector_counts = {}
        for ticker, info in stock_data.items():
            sector = STOCK_SECTORS.get(ticker, '')
            if sector:
                change = info.get('change_pct', 0)
                sector_returns[sector] = sector_returns.get(sector, 0) + change
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
        # Average
        for sector in sector_returns:
            if sector_counts[sector] > 0:
                sector_returns[sector] /= sector_counts[sector]
        return sector_returns

    @staticmethod
    def _build_stock_context(ticker: str, stock_data: Dict,
                             sector_returns: Dict[str, float]) -> StockContext:
        """Build per-stock context with sector data for smarter scoring"""
        stock_info = stock_data.get(ticker, {})
        sector = STOCK_SECTORS.get(ticker, '')
        stock_change = stock_info.get('change_pct', 0)
        stock_vol = stock_info.get('volatility', 0) / 100.0

        sector_avg = sector_returns.get(sector, 0)
        # Normalize sector momentum to -1..+1 range (assume +-20% is extreme)
        sector_mom = max(-1, min(1, sector_avg / 20.0))
        # Relative strength: how much this stock outperforms its sector
        relative_strength = stock_change - sector_avg

        return StockContext(
            ticker=ticker,
            stock_change_pct=stock_change,
            stock_volatility=stock_vol,
            sector=sector,
            sector_momentum=sector_mom,
            relative_strength=relative_strength
        )

    @staticmethod
    def _get_market_cap_category(ticker: str) -> str:
        """Classify stock by market cap"""
        if ticker in LARGE_CAP:
            return 'large'
        elif ticker in MID_CAP:
            return 'mid'
        else:
            return 'small'


# ============ DATA PIPELINE ORCHESTRATOR ============
class DataPipeline:
    """Orchestrates data collection, processing, and storage"""

    def __init__(self):
        self.db = TickwaveDB()
        self.db.init_schema()
        self.rss_collector = RSSFeedCollector(self.db)
        self.market_collector = MarketDataCollector(MONITORED_STOCKS)
        self.llm_verifier = LLMCrossVerifier(self.db) if GROQ_API_KEY else None
        self.parser = PolicyParser(llm_verifier=self.llm_verifier)
        self.signal_engine = SignalEngine(parser=self.parser)

    def run(self) -> Dict:
        """Execute complete data pipeline"""
        logger.info("\n" + "=" * 60)
        logger.info("Tickwave Data Pipeline Starting...")
        logger.info("=" * 60)

        start_time = time.time()

        # Load latest calibration multipliers (if any) so new signals use them.
        try:
            from calibration import load as _cal_load
            from alpha_scoring_engine import set_magnitude_multipliers
            mults = {}
            for h in ("1D", "3D", "20D"):
                row = _cal_load(self.db, "magnitude", h)
                if row and row.get("params", {}).get("k") is not None:
                    mults[h] = row["params"]["k"]
            if mults:
                set_magnitude_multipliers(mults)
        except Exception as _cal_exc:
            logger.debug("calibration load skipped: %s", _cal_exc)

        # Step 1: Collect news from all sources
        articles = self.rss_collector.fetch_feeds()

        # Step 2: Fetch market data (batch mode, all stocks)
        stock_data = self.market_collector.fetch_stock_data()

        # Step 3: Parse events with NLP
        events = self.parser.parse_articles(articles)

        # Step 3.5: Enrich events with source tiering + freshness + cross-source
        # confirmation. Drops weight on single-source / stale / tipster items.
        try:
            events = enrich_events_with_tiering(events, articles)
        except Exception as _tier_exc:
            logger.warning(f"source tiering enrichment skipped: {_tier_exc}")

        # Step 4: Generate signals with alpha scoring
        signals, regime, regime_strength = self.signal_engine.generate_signals(events, stock_data)

        # Step 5: Write everything to database
        self._write_to_database(events, signals, regime, regime_strength, stock_data)

        # Step 5.5: Phase 1.5 — social collection + volume/intent/forensic enrichment.
        # Runs AFTER the base write so extended fields get UPDATE'd onto the
        # just-inserted signal rows. Fail-soft — never blocks the pipeline.
        try:
            from orchestrator_ext import run_phase_1_5
            phase_summary = run_phase_1_5(self.db, signals, MONITORED_STOCKS)
            logger.info(f"  Phase 1.5: {phase_summary}")
        except Exception as phase_exc:
            logger.warning(f"Phase 1.5 enrichment skipped: {phase_exc}")

        # Build output
        sorted_signals = sorted(signals, key=lambda x: x['alpha_score'], reverse=True)
        output = {
            'timestamp': datetime.now().isoformat(),
            'execution_time_seconds': round(time.time() - start_time, 2),
            'summary': {
                'articles_collected': len(articles),
                'stocks_monitored': len(stock_data),
                'events_extracted': len(events),
                'signals_generated': len(signals),
                'valid_signals': sum(1 for s in signals if s['valid']),
            },
            'regime': {
                'type': regime.value,
                'strength': float(regime_strength)
            },
            'stocks': stock_data,
            'events': events[:30],
            'signals': sorted_signals[:20],
            'data_quality': {
                'event_extraction_rate': round(len(events) / max(len(articles), 1), 2),
                'signal_generation_rate': round(len(signals) / max(len(events), 1), 2),
            }
        }

        elapsed = time.time() - start_time
        logger.info(f"Pipeline completed in {elapsed:.2f}s")
        logger.info("=" * 60 + "\n")

        return output

    def _write_to_database(self, events, signals, regime, regime_strength, stock_data):
        """Persist all pipeline output to database"""
        logger.info("Writing to database...")

        # Write events
        event_count = 0
        for event in events:
            success = self.db.insert_event(
                event_id=event['event_id'],
                title=event['title'],
                summary=event['summary'],
                source=event['source'],
                link=event['link'],
                event_type=event['event_type'],
                event_confidence=event['event_confidence'],
                sentiment=event['sentiment'],
                sentiment_confidence=event['sentiment_confidence'],
                magnitude=event['magnitude'],
                impact_score=event['impact_score'],
                companies=event['companies'],
                published_at=event.get('published')
            )
            if success:
                event_count += 1

        # Write geo events
        geo_count = 0
        for event in events:
            if event.get('geo'):
                success = self.db.insert_geo_event(
                    event_id=f"geo_{event['event_id']}",
                    country=event['geo']['country'],
                    description=event['title'],
                    impact='high' if event['impact_score'] >= 70 else 'medium' if event['impact_score'] >= 40 else 'low',
                    latitude=event['geo']['latitude'],
                    longitude=event['geo']['longitude'],
                    related_assets=event['companies'],
                    source=event['source']
                )
                if success:
                    geo_count += 1

        # Write signals and their predictions
        signal_count = 0
        deduped = 0
        cooldown_h = int(os.getenv('SIGNAL_COOLDOWN_HOURS', '24'))
        for signal in signals:
            # Cooldown: skip if same (ticker, event_type, sentiment) is already active
            # within the cooldown window. Stops the same news re-firing every cycle.
            if cooldown_h > 0 and self.db.recent_signal_exists(
                signal['ticker'], signal['event_type'], signal.get('sentiment', 'neutral'),
                hours=cooldown_h,
            ):
                deduped += 1
                continue
            success = self.db.upsert_signal(
                event_id=signal['event_id'],
                event_type=signal['event_type'],
                ticker=signal['ticker'],
                alpha_score=signal['alpha_score'],
                confidence=signal['confidence'],
                regime=signal['regime'],
                entry_price=signal['entry_price'],
                sentiment=signal['sentiment'],
                company=signal.get('company'),
                regime_strength=signal.get('regime_strength', 0),
                magnitude=signal.get('magnitude', 0),
                impact_score=signal.get('impact_score', 0),
                source=signal.get('source'),
                headline=signal.get('headline'),
                link=signal.get('link')
            )
            if success:
                signal_count += 1
                # Write predictions for this signal
                for horizon, pred in signal.get('predictions', {}).items():
                    target = signal.get('price_targets', {}).get(horizon, {})
                    self.db.insert_prediction(
                        signal_id=signal['event_id'],
                        event_id=signal['event_id'],
                        horizon=horizon,
                        predicted_return_pct=pred['return_pct'],
                        target_price=target.get('target_price', 0),
                        confidence=pred.get('confidence', 0)
                    )

        # Write market regime
        today = datetime.now().strftime('%Y-%m-%d')
        market_data = self.signal_engine._build_market_data(stock_data)
        self.db.insert_regime(
            regime_date=today,
            regime_type=regime.value,
            volatility=market_data.volatility,
            momentum=market_data.momentum_score,
            trend_strength=market_data.trend_strength,
            price_change_1d=market_data.price_change_1d,
            regime_strength=regime_strength
        )

        logger.info(
            f"  DB writes: {event_count} events, {geo_count} geo, "
            f"{signal_count} signals (skipped {deduped} cooldown dupes)"
        )

    def save_output(self, data: Dict) -> Optional[Path]:
        """Save pipeline output to JSON (backup/debug)"""
        try:
            if OUTPUT_FILE.exists():
                BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(OUTPUT_FILE, BACKUP_FILE)

            with open(OUTPUT_FILE, 'w') as f:
                json.dump(data, f, indent=2, default=str)

            logger.info(f"JSON saved: {OUTPUT_FILE}")
            return OUTPUT_FILE

        except Exception as e:
            logger.error(f"Failed to save JSON: {e}")
            return None


# ============ MAIN EXECUTION ============
def run_pipeline():
    """Run the scraper pipeline once. Returns output dict."""
    try:
        pipeline = DataPipeline()
        output = pipeline.run()

        # Print summary
        summary = output['summary']
        print(f"\n{'='*40}")
        print(f"  Articles: {summary['articles_collected']}")
        print(f"  Stocks:   {summary['stocks_monitored']}")
        print(f"  Events:   {summary['events_extracted']}")
        print(f"  Signals:  {summary['signals_generated']} ({summary['valid_signals']} valid)")
        print(f"  Regime:   {output['regime']['type']}")
        print(f"  Time:     {output['execution_time_seconds']}s")
        print(f"{'='*40}\n")

        # Also save JSON backup
        pipeline.save_output(output)

        return output

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        return None


def main():
    """Run the scraper pipeline"""
    run_pipeline()


if __name__ == '__main__':
    main()
