"""
Concall transcript resolver — (Track 1 Phase B).

Doesn't do raw scraping itself. Instead, resolves a "give me the concall
transcripts for ticker X" query by consulting in priority order:

    1. filings table where filing_type='concall_transcript'
       (populated by bse_filing_fetcher / nse_filing_fetcher under Phase B)
    2. ir_pdf_fetcher.IR_PAGES — known company IR landing pages,
       scanned for 'transcript'/'earnings call' anchor text
    3. nothing — caller gets an empty list

Why a thin resolver vs another scraper:
    Phase B made the BSE + NSE fetchers classify concall transcripts into
    `filings.filing_type='concall_transcript'`. So the BSE/NSE pipelines are
    the actual source. This module is the read-side adapter the IR API
    endpoint (/api/stock/<ticker>/ir) calls. Keeps scraping centralised in
    one place and avoids duplicate PDF downloads.

Returns a normalised list:
    [
      {date: 'YYYY-MM-DD', title: str, url: str, source: 'bse'|'nse'|'ir',
       summary: str|None, exchange: str}
    ]
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

from forensics._filing_common import (
    classify_filing,
    download_pdf as _common_download_pdf,
    pdf_to_text,
)

logger = logging.getLogger(__name__)

# Anchor-text patterns that indicate a transcript link on an IR page
_ANCHOR_PATTERNS = [
    re.compile(r"transcript",                re.IGNORECASE),
    re.compile(r"earnings\s+call",           re.IGNORECASE),
    re.compile(r"conference\s+call",         re.IGNORECASE),
    re.compile(r"analyst\s+(meet|call)",     re.IGNORECASE),
]

# Generic anchor regex — picks up <a href="..." ...>...</a>
_LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def from_filings_db(db, ticker: str, limit: int = 12) -> List[Dict]:
    """Query the filings table for previously-scraped concall transcripts."""
    if not db or not ticker:
        return []
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    sql = f"""
        SELECT title, pdf_url, filed_at, source, exchange, summary
          FROM filings
         WHERE ticker = {p}
           AND filing_type = 'concall_transcript'
         ORDER BY filed_at DESC NULLS LAST
         LIMIT {p}
    """
    # SQLite doesn't support `NULLS LAST` — fall back to simple DESC there
    if not getattr(db, "is_postgres", False):
        sql = sql.replace("DESC NULLS LAST", "DESC")
    try:
        cur = db.conn.cursor()
        cur.execute(sql, (ticker.upper(), limit))
        rows = cur.fetchall() or []
    except Exception as exc:
        logger.debug("concall db query failed ticker=%s err=%s", ticker, exc)
        return []
    out: List[Dict] = []
    for r in rows:
        title, url, filed_at, source, exchange, summary = (
            r[0], r[1], r[2], r[3], (r[4] if len(r) > 4 else None),
            (r[5] if len(r) > 5 else None),
        )
        date_str = ""
        try:
            date_str = filed_at.strftime("%Y-%m-%d") if hasattr(filed_at, "strftime") else str(filed_at)[:10]
        except Exception:
            date_str = str(filed_at)[:10] if filed_at else ""
        out.append({
            "date":     date_str,
            "title":    title or "",
            "url":      url or "",
            "source":   source or "filings",
            "exchange": exchange or "",
            "summary":  summary,
        })
    return out


def from_ir_page(ticker: str, max_links: int = 5) -> List[Dict]:
    """Scan a company's known IR landing page for transcript-shaped links.

    Best-effort. Companies' IR sites are inconsistent; we return whatever
    matches obvious anchor-text patterns. URLs are normalised to absolute.
    """
    try:
        from forensics.ir_pdf_fetcher import IR_PAGES, IRPageFetcher  # type: ignore
    except Exception:
        return []
    base = IR_PAGES.get((ticker or "").upper())
    if not base:
        return []
    try:
        # IRPageFetcher's _fetch_html does the rate-limited GET — borrow it
        f = IRPageFetcher(db=None, tickers=[])
        html = f._fetch_html(base)
    except Exception as exc:
        logger.debug("ir fetch failed ticker=%s err=%s", ticker, exc)
        return []
    if not html:
        return []
    out: List[Dict] = []
    seen_urls: set[str] = set()
    for m in _LINK_RE.finditer(html):
        href, label = m.group(1), m.group(2)
        # Strip nested HTML out of the anchor's visible text
        text = re.sub(r"<[^>]+>", "", label or "").strip()
        if not text:
            continue
        if not any(p.search(text) for p in _ANCHOR_PATTERNS):
            continue
        url = urljoin(base, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "date":     "",
            "title":    text[:200],
            "url":      url,
            "source":   "ir",
            "exchange": "",
            "summary":  None,
        })
        if len(out) >= max_links:
            break
    return out


def resolve(db, ticker: str, limit: int = 12) -> List[Dict]:
    """Combined: db-first, then IR-page fallback. Deduped on URL."""
    seen: set[str] = set()
    out: List[Dict] = []
    for item in from_filings_db(db, ticker, limit=limit):
        if item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    if len(out) >= limit:
        return out[:limit]
    for item in from_ir_page(ticker, max_links=limit - len(out)):
        if item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out[:limit]


def fetch_text(url: str, source: str = "ir") -> str:
    """Download the transcript PDF and return extracted text. Convenience for
    AI-research / summary pipelines that need the raw content."""
    pdf = _common_download_pdf(url, source=source)
    if not pdf:
        return ""
    return pdf_to_text(pdf, max_pages=40)
