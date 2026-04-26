"""
Intra-quarter promoter events — SAST Reg 29 and PIT Reg 7(2) disclosures.

Both are republished by BSE/NSE in corporate-filings indices. We pull daily.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional

from net import request_with_retry
from ratelimit import get_bucket
from metrics import inc
from fundamentals.bse_scrip_map import get_scrip

logger = logging.getLogger(__name__)

BSE_CORP_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
HEADERS = {
    "User-Agent": "Mozilla/5.0 alphaevent/0.1",
    "Referer": "https://www.bseindia.com/",
}


def _classify_disclosure(title: str) -> Optional[str]:
    t = (title or "").lower()
    if "reg. 29" in t or "reg 29" in t or "sast" in t:
        if "disposal" in t or "sell" in t:
            return "sast_dispose"
        return "sast_acquire"
    if "reg. 7" in t or "reg 7" in t or "insider trading" in t or "pit" in t:
        if "sell" in t or "disposal" in t:
            return "pit_sell"
        return "pit_buy"
    if "pledge" in t:
        if "release" in t or "invocation" in t:
            return "release"
        return "pledge"
    return None


class PromoterEventsFetcher:
    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)

    def _fetch_announcements(self, scrip_code: str, since: date) -> List[dict]:
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            inc("scraper_ratelimit_skips_total", source="bse")
            return []
        try:
            resp = request_with_retry(
                "GET",
                BSE_CORP_ANN_API,
                source="bse",
                params={
                    "pageno": 1,
                    "strCat": "-1",
                    "strPrevDate": since.strftime("%Y%m%d"),
                    "strScrip": scrip_code,
                    "strSearch": "P",
                    "strToDate": date.today().strftime("%Y%m%d"),
                    "strType": "C",
                    "subcategory": "-1",
                },
                headers=HEADERS,
                attempts=2,
                timeout=15,
            )
            data = resp.json()
            rows = data.get("Table") or data.get("table") or []
            return list(rows)
        except Exception as exc:
            logger.debug("bse ann fetch failed scrip=%s err=%s", scrip_code, exc)
            return []

    def refresh_ticker(self, ticker: str, lookback_days: int = 7) -> int:
        scrip = get_scrip(ticker)
        if not scrip:
            return 0
        since = date.today() - timedelta(days=lookback_days)
        rows = self._fetch_announcements(scrip, since)
        inserted = 0
        for row in rows:
            title = (row.get("HEADLINE") or row.get("NEWSSUB") or "").strip()
            if not title:
                continue
            kind = _classify_disclosure(title)
            if not kind:
                continue
            event_date = None
            ds = row.get("NEWS_DT") or row.get("NEWSDATE") or row.get("DT_TM")
            if ds:
                try:
                    event_date = datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
                except ValueError:
                    try:
                        event_date = datetime.strptime(str(ds)[:10], "%d-%m-%Y").date()
                    except ValueError:
                        event_date = None
            if not event_date:
                continue
            link = row.get("ATTACHMENTNAME") or row.get("ANNOUNCEMENT_URL") or ""
            if link and not link.startswith("http"):
                link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{link}"
            if self._upsert_event(ticker, event_date, kind, title, link):
                inserted += 1
                inc("scraper_promoter_events_total", kind=kind)
        return inserted

    def _upsert_event(self, ticker: str, event_date: date, kind: str, title: str, link: str) -> bool:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            if self.db.is_postgres:
                self.db.conn.cursor().execute(
                    """INSERT INTO promoter_events (ticker, event_date, event_type, reason, source, link)
                       VALUES (%s, %s, %s, %s, 'bse', %s)
                       ON CONFLICT (ticker, event_date, event_type, person_name) DO NOTHING""",
                    (ticker, event_date, kind, title[:300], link),
                )
            else:
                self.db.conn.execute(
                    f"""INSERT OR IGNORE INTO promoter_events
                        (ticker, event_date, event_type, reason, source, link, person_name)
                        VALUES ({p}, {p}, {p}, {p}, 'bse', {p}, NULL)""",
                    (ticker, event_date.isoformat(), kind, title[:300], link),
                )
            self.db.conn.commit()
            return True
        except Exception as exc:
            logger.debug("event upsert failed: %s", exc)
            self.db.conn.rollback()
            return False

    def refresh_all(self, lookback_days: int = 7) -> dict:
        totals = {}
        for t in self.tickers:
            try:
                totals[t] = self.refresh_ticker(t, lookback_days)
            except Exception as exc:
                logger.warning("event refresh failed ticker=%s err=%s", t, exc)
                totals[t] = 0
        return totals
