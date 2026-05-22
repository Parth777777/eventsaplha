"""Wire-style corporate news feed — broker-app parity.

The existing /api/stock/<t>/news pulls a mix of RSS feeds, NewsAPI, and
yfinance "news" (mostly low-signal Yahoo aggregator chatter). What our
users actually want is the kind of feed they see on Zerodha Kite / Kotak
Neo / Groww:

    "PFC: BOARD APPROVES IN-PRINCIPLE MERGER OF PFC AND REC FOLLOWING
     UNION BUDGET 2026-27 ANNOUNCEMENT, SUBJECT TO REQUISITE APPROVALS..."

    "POWER FINANCE CORP: Q3 SL NET PROFIT 47.6B RUPEES VS 41.5B (YOY); EST 51B"

    "Power Finance: Transfers South Kalamb Power Transmission Contract
     To Adani Energy For ₹125.3 Crore"

These come from BSE/NSE corporate filings, SEBI disclosures, and earnings
wires — sources our scrapers already write to the events/signals tables.
This module:

    1. Pulls events for a ticker from the local DB (filings + disclosures)
    2. Categorizes each into one of 9 wire-style event types via regex
    3. Filters out low-signal generic news
    4. Returns a clean feed the UI can render with type chips

Public API:
    categorize(headline: str) -> CategoryTag | None
    fetch_corporate_news(db, ticker, limit=50) -> List[Dict]
    register(app, get_db)  → mounts /api/stock/<ticker>/news-pro
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Wire-style event categories ─────────────────────────────────────────
# Order matters: earlier patterns win when multiple match.
#
# Each tuple: (slug, label, color_var, patterns)
CATEGORIES: List[Tuple[str, str, str, List[str]]] = [
    ("results", "Results", "info", [
        r"\bQ[1-4]\s+(?:SL\s+)?(?:net\s+profit|revenue|profit|ebitda|loss|sales|consolidated|standalone)\b",
        r"\b(?:revenue|profit|loss|net\s+income|sales)\s+(?:of\s+)?₹?[\d,]+\s*(?:b|bn|billion|m|mn|million|cr|crore|crores)?\s+(?:rupees\s+)?(?:vs|versus|estimate|YoY|y-o-y)\b",
        r"\b(?:beats?|misses?|in\s+line\s+with)\s+(?:consensus|estimate|expectations)\b",
        r"\bEPS\s+(?:of\s+)?₹?[\d.]+\s+(?:vs|versus)\b",
        r"\bquarterly\s+results?\b",
        r"\bearnings\s+(?:beat|miss|surprise|disappoint)",
    ]),
    ("m_and_a", "M&A", "info", [
        r"\b(?:merger|acquisition|acquires?|demerger|spin-off|spinoff|takeover|stake\s+sale|stake\s+acquisition)\b",
        r"\bBOARD\s+APPROVES?\s+(?:IN-PRINCIPLE\s+)?MERGER",
        r"\bopen\s+offer\b",
        r"\bjoint\s+venture\s+with\b",
    ]),
    ("contract", "Contract", "bull", [
        r"\bwins?\s+(?:contract|order|tender|bid|project)\b",
        r"\bcontract\s+(?:awarded|won|of|worth)\b",
        r"\border\s+(?:worth|of|received|book)\b",
        r"\bbidding\s+process\b",
        r"\bL1\s+bidder\b",
    ]),
    ("transfer", "Transfer", "info", [
        r"\btransfers?\s+(?:subsidiary|unit|business|stake|undertaking|division|asset)\b",
        r"\btransfers?\s+[\w\s]+\s+(?:to|for)\s+₹?[\d,.]+",
        r"\bslump\s+sale\b",
    ]),
    ("spv_formation", "SPV", "info", [
        r"\bforms?\s+(?:special\s+purpose\s+vehicle|SPV|subsidiary|company)\b",
        r"\bincorporates?\s+(?:special\s+purpose|SPV|new\s+subsidiary|wholly[-\s]owned)\b",
        r"\b(?:new\s+)?subsidiary\s+incorporated\b",
    ]),
    ("exec_change", "Executive", "caution", [
        r"\b(?:names?|appoints?|elevates?|elects?|hires?)\b[^.]{0,80}\b(?:CEO|CFO|MD|managing\s+director|chairman|chief\s+executive|chief\s+financial)\b",
        r"\b(?:CEO|CFO|MD|chairman|director)\s+(?:resigns?|steps?\s+down|exits|quits|retires)\b",
        r"\b(?:resignation|exit|departure)\s+of\s+(?:the\s+)?(?:CEO|CFO|MD|chairman)\b",
        r"\bjoins?\s+as\s+(?:CEO|CFO|MD|chief)\b",
        r"\bnew\s+(?:CEO|CFO|MD|chairman)\b",
    ]),
    ("dividend", "Dividend", "bull", [
        r"\b(?:interim\s+|final\s+|special\s+)?dividend\s+of\s+₹?[\d.]+",
        r"\brecord\s+date.{0,40}\bdividend\b",
        r"\bannounces?\s+dividend\b",
        r"\bdividend\s+payout\b",
    ]),
    ("fundraising", "Fundraising", "info", [
        r"\bfund(?:raising|raise)\b",
        r"\bQIP\b|\bqualified\s+institutional\s+placement\b",
        r"\brights\s+issue\b",
        r"\bpref(?:erential)?\s+(?:allotment|issue)\b",
        r"\bbond\s+(?:issue|issuance)\b",
        r"\bdebenture\s+(?:issue|issuance)\b",
        r"\bNCD\s+(?:issue|allotment)\b",
        r"\b(?:approves?|sanctions?)\s+fund(?:raising|raise)\s+(?:of\s+)?(?:up\s+to\s+)?₹?[\d,.]+",
    ]),
    ("buyback", "Buyback", "bull", [
        r"\bshare\s+buyback\b",
        r"\bbuyback\s+(?:of\s+|approval|approved)\b",
    ]),
    ("capex", "Capex", "info", [
        r"\bcapex\s+(?:plan|expansion|of)\b",
        r"\bcapital\s+expenditure\s+(?:plan|of)\b",
        r"\bexpansion\s+plan(?:s|ned)?\b",
        r"\bnew\s+plant\s+(?:at|in)\b",
        r"\bcommiss(?:ions|ioned)\s+(?:new\s+)?(?:plant|facility|unit)\b",
    ]),
    ("rating", "Rating", "caution", [
        r"\bcredit\s+rating\b",
        r"\b(?:CRISIL|ICRA|CARE|India\s+Ratings|S&P|Moody'?s|Fitch)\s+(?:upgrades?|downgrades?|affirms?|assigns?)\b",
        r"\b(?:upgraded|downgraded)\s+to\s+(?:AAA|AA|A\+|BBB|BB|B|C|D|stable|positive|negative)\b",
    ]),
    ("legal", "Legal/Regulatory", "bear", [
        r"\bSEBI\s+(?:order|notice|penalty|fine|action)\b",
        r"\bCBI\b|\bED\b\s+(?:raid|investigation|notice)",
        r"\b(?:NCLT|NCLAT|Supreme\s+Court|High\s+Court)\s+(?:order|ruling|judgment)\b",
        r"\bshow\s+cause\s+notice\b",
        r"\b(?:fined|penalty\s+of)\s+₹?[\d,.]+",
    ]),
    ("agm", "AGM", "info", [
        r"\b(?:AGM|annual\s+general\s+meeting)\b",
        r"\bEGM\b|\bextraordinary\s+general\s+meeting\b",
        r"\bpostal\s+ballot\b",
    ]),
    ("board", "Board", "info", [
        r"\bboard\s+meeting\s+(?:on|to\s+consider|scheduled)\b",
        r"\bboard\s+(?:approves?|sanctions?|recommends?|considers?)\b",
    ]),
]


CATEGORY_DEFAULT = ("other", "Update", "tertiary", [])


def categorize(headline: str, source: str = '') -> Dict[str, str]:
    """Match the headline against the regex catalog. Returns a dict with
    `slug`, `label`, and `color` keys. Falls back to 'other' if no match.
    """
    if not headline:
        return {"slug": CATEGORY_DEFAULT[0], "label": CATEGORY_DEFAULT[1], "color": CATEGORY_DEFAULT[2]}
    h = headline.strip()
    for slug, label, color, patterns in CATEGORIES:
        for pat in patterns:
            if re.search(pat, h, flags=re.IGNORECASE):
                return {"slug": slug, "label": label, "color": color}
    return {"slug": CATEGORY_DEFAULT[0], "label": CATEGORY_DEFAULT[1], "color": CATEGORY_DEFAULT[2]}


# ── Material-event filter ──────────────────────────────────────────────
# Skip headlines that are obvious aggregator noise. Wire-style corp news
# is short, fact-rich, and starts with a verb or the ticker.

_LOW_SIGNAL_PATTERNS = [
    r"\bstocks?\s+to\s+(?:watch|buy|sell)\b",
    r"\btop\s+(?:gainers?|losers?)\s+today\b",
    r"\b5\s+best\s+stocks?\b|\bbest\s+\d+\s+(?:large|mid|small)\s+cap\s+stocks\b",
    r"\bshould\s+you\s+(?:buy|sell|invest)\b",
    r"\bopinion:\s",
    r"\bmarket\s+wrap\b",
    r"\bsensex\s+today\b|\bnifty\s+today\b",
    r"^\s*(?:nse|bse)\s+\d{2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
]


def is_material(headline: str, source: str = '') -> bool:
    """Return True if this looks like a wire-style corp event vs aggregator noise."""
    if not headline or len(headline.strip()) < 12:
        return False
    h = headline.lower()
    for pat in _LOW_SIGNAL_PATTERNS:
        if re.search(pat, h, flags=re.IGNORECASE):
            return False
    return True


# ── Source quality boost ───────────────────────────────────────────────
# Filings and disclosures rank above generic news. We surface them first.

_HIGH_QUALITY_SOURCE_PATTERNS = [
    r"\bBSE\b", r"\bNSE\b", r"\bSEBI\b",
    r"\bcorporate\s+(?:filing|disclosure|announcement)\b",
    r"\bReuters\b", r"\bBloomberg\b",
    r"\bPIB\b", r"\bRBI\b",
]


def source_rank(source: str) -> int:
    """Higher = better. Use as secondary sort after time."""
    if not source:
        return 0
    s = source.lower()
    for pat in _HIGH_QUALITY_SOURCE_PATTERNS:
        if re.search(pat, s, flags=re.IGNORECASE):
            return 2
    if 'feed' in s or 'rss' in s or 'aggregator' in s:
        return 0
    return 1


# ── Fetch + assemble the pro feed ──────────────────────────────────────

def fetch_corporate_news(db, ticker: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Pull from local DB (events table + signals) for the given ticker.
    Filter to material events. Categorize. Return wire-style feed.
    """
    ticker = (ticker or '').upper().strip()
    if not ticker:
        return []

    rows: List[Dict[str, Any]] = []

    # 1. Events table (catches BSE/NSE filings, SEBI disclosures + scraped wires)
    try:
        cur = db.conn.cursor()
        result = cur.execute(
            """SELECT event_id, title, summary, source, link, event_type, sentiment,
                      magnitude, impact_score, published_at,
                      COALESCE(ai_summary, '') AS ai_summary
                 FROM events
                WHERE (companies LIKE ? OR companies LIKE ? OR companies LIKE ?
                       OR title LIKE ? OR title LIKE ?)
                ORDER BY published_at DESC LIMIT ?""",
            (
                f'%{ticker}%', f'{ticker},%', f'%,{ticker}',
                f'%{ticker}%', f'%{ticker.title()}%',
                int(limit) * 3,  # over-fetch, we'll trim after filtering
            ),
        ).fetchall()
        for r in result:
            try:
                d = dict(r)
            except (TypeError, ValueError):
                d = {
                    'event_id': r[0], 'title': r[1], 'summary': r[2],
                    'source': r[3], 'link': r[4], 'event_type': r[5],
                    'sentiment': r[6], 'magnitude': r[7], 'impact_score': r[8],
                    'published_at': r[9], 'ai_summary': r[10],
                }
            rows.append(d)
    except Exception as e:
        logger.debug('events lookup failed for %s: %s', ticker, e)

    # 2. Signals (in case scraper logged a high-alpha signal without an event row)
    try:
        cur = db.conn.cursor()
        result = cur.execute(
            """SELECT event_id, headline AS title, source, link, event_type,
                      sentiment, alpha_score, created_at AS published_at,
                      COALESCE(explanation, '') AS ai_summary
                 FROM signals
                WHERE ticker = ?
                ORDER BY created_at DESC LIMIT ?""",
            (ticker, int(limit) * 2),
        ).fetchall()
        seen_titles = {(r.get('title') or '').strip().lower() for r in rows}
        for r in result:
            try:
                d = dict(r)
            except (TypeError, ValueError):
                d = {
                    'event_id': r[0], 'title': r[1], 'source': r[2],
                    'link': r[3], 'event_type': r[4], 'sentiment': r[5],
                    'alpha_score': r[6], 'published_at': r[7], 'ai_summary': r[8],
                }
            tt = (d.get('title') or '').strip().lower()
            if tt and tt not in seen_titles:
                seen_titles.add(tt)
                rows.append(d)
    except Exception as e:
        logger.debug('signals lookup failed for %s: %s', ticker, e)

    # Categorize + filter
    out: List[Dict[str, Any]] = []
    for r in rows:
        title = (r.get('title') or '').strip()
        if not is_material(title, r.get('source') or ''):
            continue
        cat = categorize(title, r.get('source') or '')
        out.append({
            'event_id':     r.get('event_id'),
            'title':        title,
            'summary':      (r.get('ai_summary') or r.get('summary') or '')[:600],
            'source':       r.get('source'),
            'link':         r.get('link'),
            'event_type':   r.get('event_type'),
            'sentiment':    r.get('sentiment'),
            'alpha_score':  r.get('alpha_score'),
            'published_at': r.get('published_at'),
            'category':     cat,  # {slug, label, color}
            'source_rank':  source_rank(r.get('source') or ''),
        })

    # Sort: published_at DESC, source_rank DESC as tiebreaker
    def _sort_key(x):
        return (x.get('published_at') or '', x.get('source_rank') or 0)
    out.sort(key=_sort_key, reverse=True)

    # Dedup by title (case-insensitive) — different sources can repost
    seen = set()
    deduped = []
    for x in out:
        k = (x.get('title') or '').strip().lower()
        if k and k not in seen:
            seen.add(k)
            deduped.append(x)
    return deduped[:int(limit)]


def category_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Used by the UI to render filter chips with counts."""
    counts: Dict[str, int] = {}
    for it in items:
        slug = (it.get('category') or {}).get('slug') or 'other'
        counts[slug] = counts.get(slug, 0) + 1
    return counts


# ── Route registration ─────────────────────────────────────────────────

def register(app, get_db: Callable):
    from flask import jsonify, request

    @app.route('/api/stock/<ticker>/news-pro', methods=['GET'])
    def stock_news_pro(ticker):
        db = get_db()
        limit = max(5, min(200, int(request.args.get('limit', 50))))
        only = (request.args.get('only') or '').strip().lower() or None
        items = fetch_corporate_news(db, ticker, limit=limit)
        if only:
            items = [x for x in items if (x.get('category') or {}).get('slug') == only]
        return jsonify({
            'success': True,
            'ticker':  ticker.upper(),
            'count':   len(items),
            'counts':  category_counts(items),
            'data':    items,
        })

    logger.info("stock_news_pro registered: /api/stock/<ticker>/news-pro")
