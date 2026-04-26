"""NSE/BSE bulk & block deal scraper + SEBI ban-list overlay.

Sources:
- NSE: https://archives.nseindia.com/content/equities/bulk.csv (daily)
- BSE: https://www.bseindia.com/markets/equity/EQReports/bulk_deals.aspx
- SEBI ban list: scrapes SEBI orders RSS for "debarred" / "prohibited from
  accessing securities market" keywords.

Failsafe — every fetch is wrapped in try/except, never blocks the pipeline.
Persisted into `bulk_deals` and `sebi_bans` tables (created on demand).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

NSE_BULK_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
NSE_BLOCK_URL = "https://archives.nseindia.com/content/equities/block.csv"
SEBI_ORDERS_URL = "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecognisedFpi=yes"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}


def ensure_schema(db) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bulk_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_date TEXT,
                ticker TEXT,
                company TEXT,
                client_name TEXT,
                side TEXT,        -- BUY / SELL
                quantity INTEGER,
                price REAL,
                value_cr REAL,
                exchange TEXT,    -- NSE / BSE
                deal_kind TEXT,   -- BULK / BLOCK
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(deal_date, ticker, client_name, side, quantity)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bulk_tkr ON bulk_deals(ticker, deal_date)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sebi_bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name TEXT,
                ticker TEXT,
                order_date TEXT,
                ban_kind TEXT,    -- debarred / prohibited / suspended
                duration TEXT,
                source_link TEXT,
                title TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_name, order_date, ban_kind)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ban_tkr ON sebi_bans(ticker, order_date)")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"bulk_deals.ensure_schema: {e}")


def _fetch_csv(url: str, timeout: int = 15) -> List[Dict]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="ignore")
        rows = list(csv.DictReader(io.StringIO(text)))
        return rows
    except Exception as e:
        logger.warning(f"_fetch_csv {url} failed: {e}")
        return []


def _norm(s: str) -> str:
    return (s or "").strip().upper()


def _parse_value_cr(qty, px) -> float:
    try:
        return round(float(qty) * float(px) / 1e7, 2)
    except (TypeError, ValueError):
        return 0.0


def fetch_nse_bulk_deals(db) -> int:
    """Pull today's NSE bulk-deal CSV, persist new rows. Returns rows inserted."""
    ensure_schema(db)
    rows = _fetch_csv(NSE_BULK_URL)
    if not rows:
        return 0
    inserted = 0
    cur = db.conn.cursor()
    for r in rows:
        # Field names vary across NSE CSV versions — normalize known variants
        date = (r.get("Date") or r.get("DEAL_DATE") or r.get("DealDate") or "").strip()
        ticker = _norm(r.get("Symbol") or r.get("SYMBOL") or r.get("TickerSymbol"))
        company = (r.get("Security Name") or r.get("SECURITY_NAME") or r.get("SecurityName") or "").strip()
        client = (r.get("Client Name") or r.get("CLIENT_NAME") or r.get("ClientName") or "").strip()
        side = _norm(r.get("Buy/Sell") or r.get("BUY_SELL") or r.get("BuySell") or "")
        side = "BUY" if side.startswith("B") else "SELL" if side.startswith("S") else side
        try:
            qty = int(float((r.get("Quantity Traded") or r.get("QUANTITY") or r.get("Qty") or "0").replace(",", "")))
        except Exception:
            qty = 0
        try:
            px = float((r.get("Trade Price / Wght. Avg. Price") or r.get("PRICE") or r.get("Price") or "0").replace(",", ""))
        except Exception:
            px = 0.0
        value_cr = _parse_value_cr(qty, px)
        if not (date and ticker and side):
            continue
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO bulk_deals
                (deal_date, ticker, company, client_name, side, quantity, price, value_cr, exchange, deal_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NSE', 'BULK')
                """,
                (date, ticker, company, client, side, qty, px, value_cr),
            )
            inserted += cur.rowcount or 0
        except Exception as e:
            logger.warning(f"bulk insert failed: {e}")
    db.conn.commit()
    if inserted:
        logger.info(f"NSE bulk deals: {inserted} new rows")
    return inserted


def fetch_nse_block_deals(db) -> int:
    """Same as bulk but for the block-deal feed."""
    ensure_schema(db)
    rows = _fetch_csv(NSE_BLOCK_URL)
    if not rows:
        return 0
    inserted = 0
    cur = db.conn.cursor()
    for r in rows:
        date = (r.get("Date") or r.get("DEAL_DATE") or "").strip()
        ticker = _norm(r.get("Symbol") or r.get("SYMBOL"))
        company = (r.get("Security Name") or r.get("SECURITY_NAME") or "").strip()
        client = (r.get("Client Name") or r.get("CLIENT_NAME") or "").strip()
        side = _norm(r.get("Buy/Sell") or r.get("BUY_SELL") or "")
        side = "BUY" if side.startswith("B") else "SELL" if side.startswith("S") else side
        try:
            qty = int(float((r.get("Quantity Traded") or "0").replace(",", "")))
            px = float((r.get("Trade Price") or r.get("PRICE") or "0").replace(",", ""))
        except Exception:
            qty, px = 0, 0.0
        value_cr = _parse_value_cr(qty, px)
        if not (date and ticker and side):
            continue
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO bulk_deals
                (deal_date, ticker, company, client_name, side, quantity, price, value_cr, exchange, deal_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NSE', 'BLOCK')
                """,
                (date, ticker, company, client, side, qty, px, value_cr),
            )
            inserted += cur.rowcount or 0
        except Exception:
            pass
    db.conn.commit()
    return inserted


# ---- SEBI BAN LIST ----------------------------------------------------------

_SEBI_RSS_FEEDS = [
    # Public RSS feeds for SEBI orders / press releases
    "https://www.sebi.gov.in/sebirss.xml",
    "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doSebiCalendar=yes",
]

_BAN_KEYWORDS = [
    ("debarred", "debarred"),
    ("debar", "debarred"),
    ("prohibited from", "prohibited"),
    ("prohibition", "prohibited"),
    ("suspended", "suspended"),
    ("restrained from", "prohibited"),
    ("forfeit", "prohibited"),
]


def fetch_sebi_bans(db, max_articles: int = 100) -> int:
    """Scan SEBI RSS for ban / debar / suspension orders. Persists hits."""
    ensure_schema(db)
    inserted = 0
    cur = db.conn.cursor()
    try:
        import feedparser
    except Exception:
        return 0
    for feed_url in _SEBI_RSS_FEEDS:
        try:
            f = feedparser.parse(feed_url)
        except Exception:
            continue
        for entry in (f.entries or [])[:max_articles]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            text = (title + " " + (entry.get("summary") or "")).lower()
            kind = None
            for needle, label in _BAN_KEYWORDS:
                if needle in text:
                    kind = label
                    break
            if not kind:
                continue
            entity = title.split(":")[0][:200] if ":" in title else title[:200]
            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO sebi_bans
                    (entity_name, ticker, order_date, ban_kind, duration, source_link, title)
                    VALUES (?, NULL, ?, ?, NULL, ?, ?)
                    """,
                    (entity, entry.get("published") or datetime.utcnow().isoformat(),
                     kind, link, title),
                )
                inserted += cur.rowcount or 0
            except Exception:
                pass
    db.conn.commit()
    if inserted:
        logger.info(f"SEBI bans: {inserted} new rows")
    return inserted


# ---- QUERY HELPERS (for /api/* and detectors) ------------------------------

def recent_bulk_deals(db, ticker: Optional[str] = None, days: int = 7,
                     min_value_cr: float = 0) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    args = [f"-{days} days"]
    q = """SELECT deal_date, ticker, company, client_name, side, quantity, price,
                  value_cr, exchange, deal_kind
           FROM bulk_deals
           WHERE deal_date >= date('now', ?) """
    if ticker:
        q += " AND ticker = ? "
        args.append(ticker.upper())
    if min_value_cr > 0:
        q += " AND value_cr >= ? "
        args.append(min_value_cr)
    q += " ORDER BY deal_date DESC, value_cr DESC LIMIT 200"
    cur.execute(q, args)
    return [dict(r) for r in cur.fetchall()]


def is_banned(db, ticker_or_entity: str, days: int = 365) -> List[Dict]:
    """Returns ban records that match the ticker or entity name."""
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """
        SELECT entity_name, ticker, order_date, ban_kind, source_link, title
        FROM sebi_bans
        WHERE order_date >= datetime('now', ?)
          AND (UPPER(entity_name) LIKE ? OR UPPER(ticker) = ?)
        ORDER BY order_date DESC
        """,
        (f"-{days} days", f"%{ticker_or_entity.upper()}%", ticker_or_entity.upper()),
    )
    return [dict(r) for r in cur.fetchall()]


def build_alert_context(db, days: int = 1) -> Dict:
    """Compose the bulk_deals/pledge_changes/forensic_flips context the
    smart_alerts evaluator expects. Run once per scraper cycle.
    """
    ctx: Dict = {"bulk_deals": [], "pledge_changes": [], "forensic_flips": []}
    try:
        ctx["bulk_deals"] = [
            {"ticker": r["ticker"], "side": r["side"], "value_cr": r["value_cr"],
             "party": r["client_name"]}
            for r in recent_bulk_deals(db, days=days)
        ]
    except Exception:
        pass
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            SELECT ticker, MIN(pledge_pct) AS oldest, MAX(pledge_pct) AS latest
            FROM promoter_holdings
            WHERE as_of_date >= date('now', ?)
            GROUP BY ticker
            HAVING (latest - oldest) >= 1
            """,
            (f"-{days*30} days",),
        )
        for row in cur.fetchall():
            ctx["pledge_changes"].append({
                "ticker": row["ticker"],
                "delta_pct": float(row["latest"] or 0) - float(row["oldest"] or 0),
                "current_pct": float(row["latest"] or 0),
            })
    except Exception:
        pass
    return ctx
