"""
BSE filings fetcher — pulls corporate announcements + PDF text per scrip.

For each monitored ticker, fetches recent corporate announcements, downloads
PDFs, extracts text with pdfplumber, and stores both raw text and extracted
numeric claims for later forensic matching.

Phase B refactor: shared classification, PDF, number-extraction, and upsert
helpers now live in scraper/forensics/_filing_common.py. This module focuses
on the BSE-specific list API + iteration loop.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from net import request_with_retry
from ratelimit import get_bucket
from fundamentals.bse_scrip_map import get_scrip

from forensics._filing_common import (
    classify_filing as _classify_filing,
    extract_numbers_from_text,
    pdf_to_text,
    download_pdf as _common_download_pdf,
    upsert_filing as _common_upsert_filing,
    store_numbers as _common_store_numbers,
)

logger = logging.getLogger(__name__)

BSE_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
HEADERS = {
    "User-Agent": "Mozilla/5.0 alphaevent/0.2",
    "Referer": "https://www.bseindia.com/",
}


class BSEFilingFetcher:
    """Iterates monitored tickers and pulls BSE corporate announcements.

    Phase B: extended to recognize concall transcripts, investor presentations,
    board outcomes, credit ratings, etc. (see _filing_common.classify_filing).
    No-longer drops 'other' bucket — keeps everything so downstream can decide.
    """

    SOURCE = "bse"
    EXCHANGE = "BSE"

    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)

    def _fetch_list(self, scrip: str, since: date) -> List[dict]:
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            return []
        try:
            resp = request_with_retry(
                "GET",
                BSE_ANN_API,
                source="bse",
                params={
                    "pageno": 1,
                    "strCat": "-1",
                    "strPrevDate": since.strftime("%Y%m%d"),
                    "strScrip": scrip,
                    "strSearch": "P",
                    "strToDate": date.today().strftime("%Y%m%d"),
                    "strType": "C",
                    "subcategory": "-1",
                },
                headers=HEADERS,
                timeout=15,
                attempts=2,
            )
            data = resp.json()
            return list(data.get("Table") or data.get("table") or [])
        except Exception as exc:
            logger.debug("bse ann list failed scrip=%s err=%s", scrip, exc)
            return []

    @classmethod
    def from_universe(cls, db, group: str = "A", limit: Optional[int] = None):
        """Build a fetcher over the full BSE universe (Phase A → Phase B bridge).

        group: BSE group filter ('A'=large-cap, 'B'=mid, 'T'=trade-to-trade).
        limit: cap the iteration (useful for cron jobs running on a budget).
        """
        try:
            from scraper.bse_stock_universe import get_bse_universe
        except Exception:
            from bse_stock_universe import get_bse_universe  # type: ignore
        u = get_bse_universe() or {}
        tickers = [tk for tk, meta in u.items() if (meta.get("group") or "").upper() == group.upper()]
        if limit:
            tickers = tickers[:limit]
        return cls(db, tickers)

    def refresh_ticker(self, ticker: str, lookback_days: int = 14,
                       keep_other: bool = False) -> int:
        """Pull recent filings for one ticker. Returns count inserted/updated.

        keep_other=True persists rows whose classifier hit 'other' — useful
        for the BSE-universe sweep that wants to retain everything for later
        re-classification.
        """
        scrip = get_scrip(ticker)
        if not scrip:
            return 0
        since = date.today() - timedelta(days=lookback_days)
        rows = self._fetch_list(scrip, since)
        inserted = 0
        for r in rows:
            title = (r.get("HEADLINE") or r.get("NEWSSUB") or "").strip()
            link = r.get("ATTACHMENTNAME") or r.get("ANNOUNCEMENT_URL") or ""
            if link and not link.startswith("http"):
                link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{link}"
            filed_at_raw = r.get("NEWS_DT") or r.get("NEWSDATE") or r.get("DT_TM")
            filed_at = None
            if filed_at_raw:
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        filed_at = datetime.strptime(str(filed_at_raw)[:19], fmt)
                        break
                    except ValueError:
                        continue
            ftype = _classify_filing(title)
            if not link:
                continue
            if ftype == "other" and not keep_other:
                continue
            pdf = _common_download_pdf(link, source=self.SOURCE, headers=HEADERS)
            if not pdf:
                continue
            text = pdf_to_text(pdf)
            filing_id = _common_upsert_filing(
                self.db, ticker, ftype, title, link, filed_at, text,
                source=self.SOURCE, exchange=self.EXCHANGE,
            )
            if filing_id:
                inserted += 1
                if text:
                    _common_store_numbers(self.db, filing_id, text)
        return inserted

    def refresh_all(self, lookback_days: int = 14, keep_other: bool = False) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tickers:
            try:
                out[t] = self.refresh_ticker(t, lookback_days, keep_other=keep_other)
            except Exception as exc:
                logger.warning("filing refresh failed ticker=%s err=%s", t, exc)
                out[t] = 0
        return out
