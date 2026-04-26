"""Social Hub — unified collector + ranker for the social.html tab.

Policy (per user spec):
- X.com is the PRIMARY source for verified quick news. Only handles in
  VERIFIED_X_HANDLES are surfaced as "quick news".
- Reddit is SECONDARY. Used only for SERIOUS news — filtered by serious-news
  keywords + minimum upvote threshold + curated subs.
- Telegram is SECONDARY. Used only for SERIOUS news — filtered by serious-news
  keywords on a curated channel list.

Each item is normalized into a `social_items` row and tagged with:
  - severity: 'quick' | 'serious'
  - verified: True if from a verified-handle list
  - serious_score: 0..100 keyword-based seriousness ranking
  - is_pump_dump_risk: bool — for filtering tipsy posts out
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============ CURATED LISTS ================================================

# Verified X handles — only these surface as "verified quick news".
# Tier 1 = exchanges/regulators/wires; Tier 2 = recognized financial press.
VERIFIED_X_HANDLES = {
    # Tier 1 — exchanges & regulators
    "NSEIndia": 1, "BSEIndia": 1, "SEBI_India": 1, "RBI": 1,
    # Tier 2 — major financial press / official accounts
    "CNBCTV18News": 2, "CNBCTV18Live": 2, "moneycontrolcom": 2,
    "ETMarkets": 2, "EconomicTimes": 2, "livemint": 2,
    "ReutersIndia": 2, "ReutersBiz": 2, "BloombergQuint": 2,
    "BloombergAsia": 2, "ndtvprofit": 2, "FinancialXpress": 2,
    "businessline": 2, "bsindia": 2,
    # Tier 2.5 — useful market wires
    "ANI": 2, "PIB_India": 2,
}

# Reddit serious-news subs only. We deliberately skip /r/IndianStreetBets-style
# meme subs because their signal-to-noise is dreadful for serious news.
SERIOUS_REDDIT_SUBS = [
    "IndianStockMarket",
    "IndiaInvestments",
    "StockMarketIndia",
]

# Telegram serious-only channels. These are public broadcast channels of
# established outlets / exchanges — no operator-style tip channels.
SERIOUS_TELEGRAM_CHANNELS = [
    "bseindiaofficial",
    "nsebseindia",
    "ETMarkets",
    "moneycontrolcom",
    "CNBCTV18News",
    "livemintoff",
]

# Reddit minimum upvote threshold to qualify as "serious"
REDDIT_MIN_SCORE = 50

# How recent posts must be (hours) to surface in the feed
DEFAULT_FRESHNESS_HOURS = 12


# ============ SERIOUSNESS DETECTION ========================================

# Words that flag a post as serious/material market news.
SERIOUS_KEYWORDS = [
    # Filings / corporate actions
    "earnings", "results", "q1", "q2", "q3", "q4", "quarterly", "annual report",
    "dividend", "buyback", "bonus issue", "rights issue", "split", "spin-off",
    "ipo", "fpo", "listing", "delisting", "merger", "acquisition", "takeover",
    "open offer",
    # Regulatory
    "sebi", "rbi", "gst council", "policy", "tariff", "duty", "ban", "fine",
    "penalty", "order", "circular", "notification",
    # Macro
    "rate", "inflation", "gdp", "cpi", "wpi", "fed", "ecb", "boj", "budget",
    "monetary policy", "mpc",
    # Major business events
    "raise", "fundraise", "loan", "debt", "rating", "downgrade", "upgrade",
    "default", "fraud", "investigation", "raid", "auditor", "cfo resign",
    "ceo resign", "managing director resign",
    # Geopolitics & supply
    "war", "conflict", "sanctions", "embargo", "shutdown", "strike",
    "supply chain", "shortage",
]

# Words that strongly suggest pump/dump-style content. Items matching these
# without serious-keyword balance get filtered out.
PUMP_DUMP_FLAGS = [
    "multibagger", "10x", "100x", "1000%", "guaranteed return", "sure shot",
    "tips", "telegram tips", "premium call", "join my channel", "intraday tip",
    "operator buying", "circuit lock", "lock upper", "bumper",
    "join group", "paid call", "subscribe channel", "%-target",
]


def serious_score(text: str) -> int:
    """0..100 score of how 'serious-news-y' a post looks."""
    if not text:
        return 0
    t = text.lower()
    serious_hits = sum(1 for kw in SERIOUS_KEYWORDS if kw in t)
    pump_hits = sum(1 for kw in PUMP_DUMP_FLAGS if kw in t)
    raw = serious_hits * 12 - pump_hits * 25
    # Length bonus — long posts more likely to be substantive
    if len(text) > 200: raw += 10
    if len(text) > 500: raw += 5
    return max(0, min(100, raw))


def is_pump_dump_risk(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return sum(1 for kw in PUMP_DUMP_FLAGS if kw in t) >= 1


# ============ DB SCHEMA =====================================================

def ensure_schema(db) -> None:
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS social_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,        -- x | reddit | telegram
            handle TEXT NOT NULL,           -- @user or r/sub or t.me/channel
            url TEXT UNIQUE,
            title TEXT,
            text TEXT,
            tickers TEXT,                   -- JSON array
            primary_ticker TEXT,
            posted_at TEXT,
            collected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            verified INTEGER DEFAULT 0,
            verified_tier INTEGER,          -- 1 / 2 / null
            severity TEXT,                  -- quick | serious
            serious_score INTEGER,
            pump_dump_risk INTEGER DEFAULT 0,
            engagement INTEGER,             -- upvotes / likes / views
            extra TEXT                      -- JSON for platform-specific data
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_si_platform ON social_items(platform, posted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_si_ticker ON social_items(primary_ticker)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS social_sources_user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default',
            platform TEXT NOT NULL,
            handle TEXT NOT NULL,
            kind TEXT,                      -- verified_quick | serious_only
            active INTEGER DEFAULT 1,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, platform, handle)
        )
        """
    )
    db.conn.commit()


def seed_default_sources(db) -> int:
    """Seed the curated source lists if the user has no entries yet."""
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM social_sources_user WHERE user_id='default'")
    if (cur.fetchone() or {"n": 0})["n"]:
        return 0
    n = 0
    for handle in VERIFIED_X_HANDLES:
        cur.execute(
            "INSERT OR IGNORE INTO social_sources_user (user_id, platform, handle, kind) VALUES (?, ?, ?, ?)",
            ("default", "x", handle, "verified_quick"),
        )
        n += cur.rowcount
    for sub in SERIOUS_REDDIT_SUBS:
        cur.execute(
            "INSERT OR IGNORE INTO social_sources_user (user_id, platform, handle, kind) VALUES (?, ?, ?, ?)",
            ("default", "reddit", sub, "serious_only"),
        )
        n += cur.rowcount
    for ch in SERIOUS_TELEGRAM_CHANNELS:
        cur.execute(
            "INSERT OR IGNORE INTO social_sources_user (user_id, platform, handle, kind) VALUES (?, ?, ?, ?)",
            ("default", "telegram", ch, "serious_only"),
        )
        n += cur.rowcount
    db.conn.commit()
    return n


# ============ COLLECTION =====================================================

def _persist_post(db, post: Dict, *, platform: str, severity: str,
                  verified: bool = False, verified_tier: Optional[int] = None,
                  engagement: int = 0, extra: Optional[Dict] = None) -> int:
    text = post.get("text") or post.get("title") or ""
    score = serious_score(text)
    pd = is_pump_dump_risk(text)
    cur = db.conn.cursor()
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO social_items
            (platform, handle, url, title, text, tickers, primary_ticker, posted_at,
             verified, verified_tier, severity, serious_score, pump_dump_risk,
             engagement, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform, post.get("handle") or "", post.get("url") or post.get("link") or "",
                (post.get("title") or text[:280]),
                text,
                json.dumps(post.get("tickers") or []),
                post.get("primary_ticker"),
                post.get("published_at") or datetime.now(timezone.utc).isoformat(),
                1 if verified else 0,
                verified_tier,
                severity,
                score,
                1 if pd else 0,
                int(engagement or 0),
                json.dumps(extra or {}, default=str),
            ),
        )
        return cur.rowcount or 0
    except Exception as e:
        logger.warning(f"social_items insert failed: {e}")
        return 0


def collect_x(db, known_tickers: Iterable[str], max_per_handle: int = 15) -> int:
    """X (Nitter) — only verified handles → severity=quick + verified=True."""
    try:
        from social.twitter_collector import TwitterCollector
    except Exception as e:
        logger.warning(f"twitter_collector unavailable: {e}")
        return 0
    handles = list(VERIFIED_X_HANDLES.keys())
    try:
        col = TwitterCollector(known_tickers=known_tickers, handles=handles,
                               max_per_handle=max_per_handle)
    except TypeError:
        # Fallback signature variations
        try:
            col = TwitterCollector(handles=handles)
        except Exception:
            col = TwitterCollector(known_tickers)
    posts = []
    try:
        posts = col.collect() or []
    except Exception as e:
        logger.warning(f"twitter collect failed: {e}")
    n = 0
    db.conn.commit()
    for p in posts:
        h = (p.get("handle") or "").lstrip("@")
        tier = VERIFIED_X_HANDLES.get(h)
        if not tier:
            continue  # only verified handles surface as quick-news
        n += _persist_post(db, p, platform="x", severity="quick",
                           verified=True, verified_tier=tier)
    db.conn.commit()
    return n


def collect_reddit_serious(db, known_tickers: Iterable[str],
                           subs: Optional[List[str]] = None,
                           limit_per_sub: int = 30) -> int:
    """Reddit — curated serious subs + serious_score >= 25 + min engagement."""
    try:
        from social.reddit_json import RedditJsonCollector
    except Exception as e:
        logger.warning(f"reddit_json unavailable: {e}")
        return 0
    target_subs = subs or SERIOUS_REDDIT_SUBS
    try:
        col = RedditJsonCollector(known_tickers=known_tickers, subs=target_subs)
    except TypeError:
        col = RedditJsonCollector(known_tickers, subs=target_subs)
    posts = []
    try:
        posts = col.collect(limit_per_sub=limit_per_sub) or []
    except Exception as e:
        logger.warning(f"reddit collect failed: {e}")
    n = 0
    for p in posts:
        text = (p.get("text") or "") + " " + (p.get("title") or "")
        # Reddit posts often carry score in extra/score field
        engagement = int(p.get("score") or p.get("upvotes") or 0)
        if engagement < REDDIT_MIN_SCORE:
            # Allow through if seriousness keywords are very strong
            if serious_score(text) < 50:
                continue
        if is_pump_dump_risk(text):
            continue
        if serious_score(text) < 20:
            continue
        n += _persist_post(db, p, platform="reddit", severity="serious",
                           verified=False, engagement=engagement)
    db.conn.commit()
    return n


def collect_telegram_serious(db, known_tickers: Iterable[str],
                             channels: Optional[List[str]] = None) -> int:
    """Telegram — curated channels only + serious_score gate."""
    try:
        from social.telegram_web import TelegramWebCollector
    except Exception as e:
        logger.warning(f"telegram_web unavailable: {e}")
        return 0
    target = channels or SERIOUS_TELEGRAM_CHANNELS
    try:
        col = TelegramWebCollector(known_tickers=known_tickers, channels=target)
    except TypeError:
        col = TelegramWebCollector(known_tickers, channels=target)
    posts = []
    try:
        posts = col.collect() or []
    except Exception as e:
        logger.warning(f"telegram collect failed: {e}")
    n = 0
    for p in posts:
        text = (p.get("text") or "") + " " + (p.get("title") or "")
        if is_pump_dump_risk(text):
            continue
        if serious_score(text) < 20:
            continue
        n += _persist_post(db, p, platform="telegram", severity="serious",
                           verified=False)
    db.conn.commit()
    return n


def run_all(db, known_tickers: Iterable[str]) -> Dict[str, int]:
    """One call to collect from all three platforms with the right severity."""
    seed_default_sources(db)
    results = {"x": 0, "reddit": 0, "telegram": 0}
    try:
        results["x"] = collect_x(db, known_tickers)
    except Exception as e:
        logger.warning(f"collect_x failed: {e}")
    try:
        results["reddit"] = collect_reddit_serious(db, known_tickers)
    except Exception as e:
        logger.warning(f"collect_reddit failed: {e}")
    try:
        results["telegram"] = collect_telegram_serious(db, known_tickers)
    except Exception as e:
        logger.warning(f"collect_telegram failed: {e}")
    return results


# ============ QUERY API =====================================================

def feed(
    db,
    *,
    platform: Optional[str] = None,    # x | reddit | telegram | None=all
    ticker: Optional[str] = None,
    severity: Optional[str] = None,    # quick | serious | None=all
    verified_only: bool = False,
    hours: int = DEFAULT_FRESHNESS_HOURS,
    limit: int = 50,
) -> List[Dict]:
    """Read items from social_items with the standard filters."""
    ensure_schema(db)
    cur = db.conn.cursor()
    args: List = [f"-{int(hours)} hours"]
    q = """SELECT platform, handle, url, title, text, tickers, primary_ticker,
                  posted_at, collected_at, verified, verified_tier, severity,
                  serious_score, pump_dump_risk, engagement
           FROM social_items
           WHERE COALESCE(posted_at, collected_at) >= datetime('now', ?) """
    if platform:
        q += " AND platform = ?"; args.append(platform)
    if ticker:
        q += " AND (primary_ticker = ? OR tickers LIKE ?)"
        args += [ticker.upper(), f"%\"{ticker.upper()}\"%"]
    if severity:
        q += " AND severity = ?"; args.append(severity)
    if verified_only:
        q += " AND verified = 1"
    q += " AND pump_dump_risk = 0"
    q += " ORDER BY datetime(COALESCE(posted_at, collected_at)) DESC LIMIT ?"
    args.append(int(limit))
    cur.execute(q, args)
    out = []
    for r in cur.fetchall():
        try:
            ticks = json.loads(r["tickers"] or "[]")
        except Exception:
            ticks = []
        out.append({
            "platform": r["platform"], "handle": r["handle"], "url": r["url"],
            "title": r["title"], "text": r["text"],
            "tickers": ticks, "primary_ticker": r["primary_ticker"],
            "posted_at": r["posted_at"], "collected_at": r["collected_at"],
            "verified": bool(r["verified"]), "verified_tier": r["verified_tier"],
            "severity": r["severity"], "serious_score": r["serious_score"],
            "engagement": r["engagement"],
        })
    return out
