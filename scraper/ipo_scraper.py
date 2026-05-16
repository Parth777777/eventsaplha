"""
Tickwave - IPO Data Scraper

Fetches IPO data from public sources, computes IPO alpha scores via the
6-factor model, and persists them to the `ipos` table. Also promotes
listed IPOs to the main `signals` table so they appear in normal feeds.

Sources:
  - NSE allIpo API (official - issue price, dates, subscription, listing)
  - MoneyControl IPO RSS feed (already in config.RSS_SOURCES['mc_ipo'])
  - Google News for company-specific buzz

Key entry points:
  - fetch_nse_ipos()              -> list[dict] (raw NSE rows)
  - fetch_ipo_news_buzz(name)     -> int (article count last 7d)
  - refresh_ipos(db)              -> dict (counts + summary)
  - promote_listed_ipos(db)       -> dict (count of newly-promoted listings)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import requests
import feedparser

logger = logging.getLogger(__name__)

# NSE requires a browser-like UA + a session-warmed cookie set.
NSE_BASE = 'https://www.nseindia.com'
NSE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': f'{NSE_BASE}/market-data/all-upcoming-ipos-ofs',
}
NSE_TIMEOUT = float(os.getenv('NSE_TIMEOUT_SECS', '12'))

MC_IPO_RSS = (
    'https://www.moneycontrol.com/rss/iponews.xml'  # mirror of mc_ipo in config.py
)

# Google News query template for IPO buzz lookups
GOOGLE_NEWS_TPL = (
    'https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en'
)


def _new_session() -> requests.Session:
    """Return a session pre-warmed with NSE cookies (required for API access)."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        # The cookie wall: hitting any HTML page sets the required cookies.
        s.get(NSE_BASE, timeout=NSE_TIMEOUT)
        s.get(f'{NSE_BASE}/market-data/all-upcoming-ipos-ofs', timeout=NSE_TIMEOUT)
    except Exception as e:
        logger.warning(f"NSE cookie warmup failed: {e}")
    return s


def _parse_date(s: Optional[str]) -> Optional[str]:
    """Parse various date formats into ISO YYYY-MM-DD (or None)."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.upper() in ('NA', 'N/A', '-'):
        return None
    fmts = ['%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%b %d, %Y', '%d %b %Y']
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    logger.debug(f"Could not parse date: {s!r}")
    return None


def _parse_float(s) -> Optional[float]:
    """Best-effort float conversion."""
    if s is None or s == '':
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        cleaned = re.sub(r'[^\d.\-]', '', str(s))
        return float(cleaned) if cleaned not in ('', '-', '.') else None
    except (ValueError, TypeError):
        return None


def _parse_price_band(s) -> tuple:
    """'100-110' or '100 - 110' -> (100.0, 110.0). Single value -> (v, v)."""
    if s is None:
        return (None, None)
    text = str(s).strip()
    if not text:
        return (None, None)
    parts = re.findall(r'\d+\.?\d*', text)
    if not parts:
        return (None, None)
    try:
        if len(parts) >= 2:
            return (float(parts[0]), float(parts[1]))
        v = float(parts[0])
        return (v, v)
    except ValueError:
        return (None, None)


def _derive_status(open_date: Optional[str], close_date: Optional[str],
                   listing_date: Optional[str], listing_price: Optional[float]) -> str:
    """Derive lifecycle status from the dates."""
    today = date.today()
    od = datetime.fromisoformat(open_date).date() if open_date else None
    cd = datetime.fromisoformat(close_date).date() if close_date else None
    ld = datetime.fromisoformat(listing_date).date() if listing_date else None

    if ld and ld <= today and listing_price:
        return 'listed'
    if ld and ld <= today:
        return 'allotment'  # listed today but no price yet
    if cd and cd < today:
        return 'allotment'
    if od and cd and od <= today <= cd:
        return 'open'
    if od and od > today:
        return 'upcoming'
    return 'upcoming'


def fetch_nse_ipos() -> List[Dict]:
    """Fetch upcoming/current/recent IPOs from NSE.

    Tries multiple known endpoints since NSE periodically renames them.
    Returns a list of normalized dicts with our internal field names.
    Returns [] on failure (NSE down, geofence, schema change). Never raises.
    """
    # NSE endpoints to try in order. Each returns a different schema; we
    # detect at parse time. The first that returns data wins.
    candidate_endpoints = [
        # Modern (post-2024): three separate endpoints
        ('current',  '/api/ipo-current-issue'),
        ('upcoming', '/api/all-upcoming-issues?category=ipo'),
        ('past',     '/api/public-past-issues?index=equities'),
        # Legacy unified endpoint (kept as fallback)
        ('all',      '/api/allIpo'),
    ]

    s = _new_session()
    out: List[Dict] = []
    seen_symbols = set()

    for bucket_key, path in candidate_endpoints:
        try:
            resp = s.get(f'{NSE_BASE}{path}', timeout=NSE_TIMEOUT)
            if resp.status_code == 404:
                logger.debug(f"NSE endpoint {path} returned 404 (skipping)")
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"NSE endpoint {path} failed: {e}")
            continue

        # Schema detection: legacy returns {current, upcoming, past}, modern
        # returns a flat list or {data: [...]} or just a list.
        rows: List[Dict] = []
        if bucket_key == 'all' and isinstance(data, dict):
            for k in ('current', 'upcoming', 'past'):
                rows.extend(data.get(k) or [])
        elif isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get('data') or data.get('Items') or []
            if not rows and bucket_key in data:
                rows = data[bucket_key] or []

        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                normalized = _normalize_nse_row(row, bucket_key)
                if normalized and normalized['symbol'] not in seen_symbols:
                    seen_symbols.add(normalized['symbol'])
                    out.append(normalized)
            except Exception as e:
                logger.debug(f"IPO row normalization failed: {e}")
                continue

    if not out:
        logger.warning("NSE returned no IPOs from any endpoint")
    else:
        logger.info(f"NSE returned {len(out)} IPOs across endpoints")
    return out


def _normalize_nse_row(row: Dict, bucket: str) -> Optional[Dict]:
    """Map raw NSE field names to our internal schema. Returns None if unusable."""
    # NSE field names vary; defend against schema drift.
    symbol = (row.get('symbol') or row.get('isin') or '').strip().upper()
    company = (row.get('companyName') or row.get('issuer') or '').strip()
    if not symbol and not company:
        return None
    if not symbol:
        # Synthesize a stable symbol from company name when NSE doesn't provide one
        symbol = re.sub(r'[^A-Z0-9]', '', company.upper())[:40] or hashlib.md5(
            company.encode()
        ).hexdigest()[:12].upper()

    open_date = _parse_date(row.get('issueStartDate') or row.get('startDate'))
    close_date = _parse_date(row.get('issueEndDate') or row.get('endDate'))
    listing_date = _parse_date(row.get('listingDate') or row.get('lstngDate'))
    allotment_date = _parse_date(row.get('allotmentDate'))

    plow, phigh = _parse_price_band(row.get('issuePrice') or row.get('priceBand'))
    issue_size = _parse_float(row.get('issueSize'))
    # NSE often quotes issue size in Rs Cr already; if it's in Rs, divide
    if issue_size and issue_size > 1_00_000:  # >1 lakh suggests it's in Rs not Cr
        issue_size = issue_size / 1_00_00_000  # convert to Cr

    sub_total = _parse_float(row.get('noOfTimesSubs') or row.get('totalSubs'))
    sub_qib = _parse_float(row.get('qibSubs'))
    sub_hni = _parse_float(row.get('hniSubs') or row.get('niiSubs'))
    sub_retail = _parse_float(row.get('retailSubs'))

    listing_price = _parse_float(row.get('listingPrice'))
    listing_gain = None
    if listing_price and phigh:
        listing_gain = round((listing_price - phigh) / phigh * 100, 2)

    status = _derive_status(open_date, close_date, listing_date, listing_price)

    return {
        'symbol': symbol,
        'ticker': symbol if status == 'listed' else None,
        'company_name': company or symbol,
        'isin': (row.get('isin') or '').strip() or None,
        'sector': (row.get('sectorName') or row.get('industry') or '').strip() or None,
        'exchange': 'NSE',
        'issue_price_low': plow,
        'issue_price_high': phigh,
        'lot_size': int(_parse_float(row.get('lotSize')) or 0) or None,
        'issue_size_cr': issue_size,
        'open_date': open_date,
        'close_date': close_date,
        'allotment_date': allotment_date,
        'listing_date': listing_date,
        'status': status,
        'sub_total': sub_total or 0,
        'sub_qib': sub_qib or 0,
        'sub_hni': sub_hni or 0,
        'sub_retail': sub_retail or 0,
        'listing_price': listing_price,
        'listing_gain_pct': listing_gain,
        'registrar': (row.get('registrar') or '').strip() or None,
        'lead_managers': (row.get('leadManager') or '').strip() or None,
    }


def fetch_ipo_news_buzz(company_name: str, days: int = 7) -> int:
    """Count Google News articles mentioning the company in the last N days."""
    if not company_name:
        return 0
    q = requests.utils.quote(f'"{company_name}" IPO')
    url = GOOGLE_NEWS_TPL.format(q=q)
    try:
        feed = feedparser.parse(url)
        cutoff = datetime.utcnow() - timedelta(days=days)
        count = 0
        for entry in (feed.entries or [])[:50]:
            pub = entry.get('published_parsed')
            if pub:
                try:
                    pub_dt = datetime(*pub[:6])
                    if pub_dt >= cutoff:
                        count += 1
                except Exception:
                    count += 1  # be lenient
            else:
                count += 1
        return count
    except Exception as e:
        logger.debug(f"News buzz fetch failed for {company_name}: {e}")
        return 0


def fetch_mc_ipo_buzz_map() -> Dict[str, int]:
    """Bulk lookup: count mentions per company in MoneyControl IPO RSS.

    Returns map of lowercase-normalized company-substring -> count, used as a
    quick prefilter before expensive per-IPO Google News queries.
    """
    out: Dict[str, int] = {}
    try:
        feed = feedparser.parse(MC_IPO_RSS)
        for e in (feed.entries or [])[:100]:
            title = (e.get('title') or '').lower()
            for word in re.findall(r'\b[a-z][a-z\s]{3,}\b', title)[:3]:
                key = word.strip()
                if len(key) >= 4:
                    out[key] = out.get(key, 0) + 1
    except Exception as e:
        logger.debug(f"MC IPO RSS fetch failed: {e}")
    return out


def _sector_signal_summary(db, sector: Optional[str]) -> tuple:
    """Return (avg_alpha, signal_count) for active signals in `sector` over last 7d.

    Sector mapping isn't enforced on the signals table directly; we use ticker
    sector mapping via stock_universe + STOCK_SECTORS. Falls back to overall
    market average when sector unknown.
    """
    if not sector:
        return None, 0
    placeholder = '%s' if db.is_postgres else '?'
    try:
        # Map sector name → list of tickers (best effort)
        from config import STOCK_SECTORS
        from stock_universe import TICKER_SECTOR_OVERRIDE
        tickers = [t for t, s in STOCK_SECTORS.items() if s.upper() == sector.upper()]
        tickers += [t for t, s in TICKER_SECTOR_OVERRIDE.items() if s.upper() == sector.upper()]
        tickers = list(set(tickers))[:50]
        if not tickers:
            return None, 0
        cur = db.conn.cursor()
        in_clause = ','.join([placeholder] * len(tickers))
        if db.is_postgres:
            cur.execute(
                f"SELECT AVG(alpha_score) AS a, COUNT(*) AS c FROM signals "
                f"WHERE ticker IN ({in_clause}) AND status = 'active' "
                f"AND created_at >= NOW() - INTERVAL '7 days'",
                tickers,
            )
        else:
            cur.execute(
                f"SELECT AVG(alpha_score) AS a, COUNT(*) AS c FROM signals "
                f"WHERE ticker IN ({in_clause}) AND status = 'active' "
                f"AND created_at >= datetime('now', '-7 days')",
                tickers,
            )
        row = cur.fetchone()
        if row:
            avg = row['a'] if isinstance(row, dict) else row[0]
            cnt = row['c'] if isinstance(row, dict) else row[1]
            try:
                avg = float(avg) if avg is not None else None
            except (TypeError, ValueError):
                avg = None
            return avg, int(cnt or 0)
    except Exception as e:
        logger.debug(f"sector signal summary failed: {e}")
    return None, 0


def refresh_ipos(db, fetch_news: bool = True, max_news_lookups: int = 10) -> Dict:
    """End-to-end refresh: fetch NSE → enrich with buzz → score → persist.

    Args:
        fetch_news: if False, skip per-IPO Google News calls (faster)
        max_news_lookups: cap expensive news lookups per cycle

    Returns summary counts.
    """
    from ipo_alpha_scorer import compute_ipo_alpha_score, factors_to_json

    started = time.time()
    raw_ipos = fetch_nse_ipos()
    if not raw_ipos:
        return {'ok': False, 'fetched': 0, 'note': 'NSE returned no data'}

    # Pre-compute sector signal summary cache (small set)
    sector_cache: Dict[str, tuple] = {}

    # Order open/upcoming first so news lookups go to the most relevant IPOs
    raw_ipos.sort(key=lambda x: 0 if x.get('status') in ('open', 'upcoming') else 1)

    written = 0
    news_done = 0
    for ipo in raw_ipos:
        try:
            # Buzz: Google News count (rate-limited; only for active IPOs)
            news_count = 0
            if fetch_news and news_done < max_news_lookups and ipo['status'] in ('open', 'upcoming'):
                news_count = fetch_ipo_news_buzz(ipo['company_name'], days=7)
                news_done += 1
                time.sleep(0.4)  # be polite to Google
            ipo['news_count'] = news_count
            ipo['buzz_score'] = min(100, news_count * 4)  # simple proxy

            # Sector context (cached per sector)
            sec = ipo.get('sector')
            if sec and sec not in sector_cache:
                sector_cache[sec] = _sector_signal_summary(db, sec)
            sector_avg, sector_cnt = sector_cache.get(sec, (None, 0))

            # Compute alpha
            score, conf, factors = compute_ipo_alpha_score(
                ipo,
                sector_avg_alpha=sector_avg,
                sector_signal_count=sector_cnt,
                sector_pe=None,  # could be enriched from sector data later
            )
            ipo['ipo_alpha_score'] = score
            ipo['ipo_confidence'] = conf
            ipo['ipo_factors_json'] = factors_to_json(factors)

            if db.upsert_ipo(ipo):
                written += 1
        except Exception as e:
            logger.warning(f"refresh_ipos: failed to process {ipo.get('symbol')}: {e}")

    elapsed_ms = int((time.time() - started) * 1000)

    # Promote listings into signals table
    promo = promote_listed_ipos(db)

    return {
        'ok': True,
        'fetched': len(raw_ipos),
        'written': written,
        'news_lookups': news_done,
        'elapsed_ms': elapsed_ms,
        'promotions': promo.get('promoted', 0),
    }


def promote_listed_ipos(db) -> Dict:
    """For each listed IPO with a price, insert a signal so it appears in feeds.

    Idempotent via the `promoted_to_signal` flag on the ipos table.
    """
    promoted = 0
    skipped = 0
    try:
        rows = db.get_unpromoted_listed_ipos() or []
    except Exception as e:
        logger.warning(f"get_unpromoted_listed_ipos failed: {e}")
        return {'promoted': 0, 'skipped': 0}

    for r in rows:
        symbol = r['symbol']
        ticker = r.get('ticker') or symbol
        listing_price = r.get('listing_price')
        listing_gain = r.get('listing_gain_pct') or 0
        ipo_alpha = float(r.get('ipo_alpha_score') or 0)
        company = r.get('company_name')

        # Direction inferred from listing gain
        if listing_gain >= 5:
            sentiment = 'bullish'
        elif listing_gain <= -5:
            sentiment = 'bearish'
        else:
            sentiment = 'neutral'

        event_id = f"ipo_listing_{symbol}_{r.get('listing_date') or 'na'}"
        ok = db.upsert_signal(
            event_id=event_id,
            event_type='ipo_listing',
            ticker=ticker,
            alpha_score=ipo_alpha,
            confidence=float(r.get('ipo_confidence') or 0.6),
            regime='sideways_calm',  # IPO-listing isn't regime-sensitive
            entry_price=listing_price,
            sentiment=sentiment,
            company=company,
            magnitude=min(10, abs(listing_gain) / 5.0),
            source='IPO',
            headline=(
                f"{company} listed at ₹{listing_price:.2f} ({listing_gain:+.1f}% vs issue)"
            ),
        )
        if ok:
            db.mark_ipo_promoted(symbol)
            promoted += 1
        else:
            skipped += 1

    return {'promoted': promoted, 'skipped': skipped}


# ============ CLI ============
if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
    except ImportError:
        pass

    from database_schema import TickwaveDB

    db = TickwaveDB()
    db.init_schema()  # ensure ipos table exists

    fetch_news = '--no-news' not in sys.argv
    print(f"Refreshing IPOs (fetch_news={fetch_news})...")
    result = refresh_ipos(db, fetch_news=fetch_news)
    print(f"Result: {result}")

    # Print a summary
    stats = db.get_ipo_stats()
    print(f"\nIPO stats: {stats}")
    db.close()
