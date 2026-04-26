"""
SEBI PIT + SAST disclosures — reuses BSE/NSE republished feeds.

The same BSE corporate-announcements feed we use for filings + promoter
events classifies Reg 7(2) (PIT) and Reg 29 (SAST) disclosures. This module
extracts them into the sebi_disclosures table with person/qty/%-change fields.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from net import request_with_retry
from ratelimit import get_bucket
from metrics import inc
from fundamentals.bse_scrip_map import get_scrip

logger = logging.getLogger(__name__)

BSE_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
HEADERS = {
    "User-Agent": "Mozilla/5.0 alphaevent/0.1",
    "Referer": "https://www.bseindia.com/",
}


DISCLOSURE_RE = {
    "pit_reg7": re.compile(r"Reg\.?\s*7\s*\(2\)|insider trading|pit\b", re.IGNORECASE),
    "sast_reg29": re.compile(r"Reg\.?\s*29|sast|substantial acquisition", re.IGNORECASE),
}

PERSON_RE = re.compile(r"by\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Shri)\s+([A-Z][A-Za-z.'\- ]{2,80})")
QTY_RE = re.compile(r"(\d[\d,]{1,})\s*(?:shares|equity)", re.IGNORECASE)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
TXN_RE = re.compile(r"\b(acquisition|acquired|buy|bought|purchase|sale|sold|disposal|dispose|pledge|release)\b",
                    re.IGNORECASE)


def _classify(title: str) -> Optional[str]:
    for kind, rx in DISCLOSURE_RE.items():
        if rx.search(title or ""):
            return kind
    return None


def _extract_txn(title: str) -> str:
    m = TXN_RE.search(title or "")
    if not m:
        return "unknown"
    kw = m.group(1).lower()
    if kw in ("sale", "sold", "disposal", "dispose"):
        return "sell"
    if kw in ("acquisition", "acquired", "buy", "bought", "purchase"):
        return "buy"
    if kw == "pledge":
        return "pledge"
    if kw == "release":
        return "release"
    return "unknown"


class SEBIDisclosuresFetcher:
    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)

    def _fetch(self, scrip: str, since: date) -> List[dict]:
        bucket = get_bucket()
        if not bucket.acquire("sebi", 1.0, max_wait=5.0):
            inc("scraper_ratelimit_skips_total", source="sebi")
            return []
        try:
            resp = request_with_retry(
                "GET",
                BSE_ANN_API,
                source="sebi",
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
                attempts=2,
                timeout=15,
            )
            data = resp.json()
            return list(data.get("Table") or data.get("table") or [])
        except Exception as exc:
            logger.debug("sebi disclosure fetch failed scrip=%s err=%s", scrip, exc)
            return []

    def refresh_ticker(self, ticker: str, lookback_days: int = 14) -> int:
        scrip = get_scrip(ticker)
        if not scrip:
            return 0
        since = date.today() - timedelta(days=lookback_days)
        rows = self._fetch(scrip, since)
        inserted = 0
        for row in rows:
            title = (row.get("HEADLINE") or row.get("NEWSSUB") or "").strip()
            kind = _classify(title)
            if not kind:
                continue
            dt_raw = row.get("NEWS_DT") or row.get("NEWSDATE") or row.get("DT_TM")
            try:
                disc_dt = datetime.strptime(str(dt_raw)[:19], "%Y-%m-%dT%H:%M:%S") if dt_raw else None
            except Exception:
                try:
                    disc_dt = datetime.strptime(str(dt_raw)[:10], "%Y-%m-%d") if dt_raw else None
                except Exception:
                    disc_dt = None
            link = row.get("ATTACHMENTNAME") or row.get("ANNOUNCEMENT_URL") or ""
            if link and not link.startswith("http"):
                link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{link}"
            person = None
            m_p = PERSON_RE.search(title)
            if m_p:
                person = m_p.group(1).strip().rstrip(".,")
            qty = None
            m_q = QTY_RE.search(title)
            if m_q:
                try:
                    qty = float(m_q.group(1).replace(",", ""))
                except ValueError:
                    qty = None
            pcts = [float(m.group(1)) for m in PCT_RE.finditer(title)]
            pct_before = pcts[0] if len(pcts) >= 2 else None
            pct_after = pcts[-1] if pcts else None
            txn = _extract_txn(title)
            if self._upsert(ticker, kind, person, txn, qty, pct_before, pct_after, disc_dt, link):
                inserted += 1
                inc("scraper_sebi_disclosures_total", kind=kind, txn=txn)
        return inserted

    def _upsert(self, ticker: str, kind: str, person: Optional[str], txn: str,
                qty: Optional[float], pct_before: Optional[float], pct_after: Optional[float],
                disc_dt: Optional[datetime], link: str) -> bool:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            if self.db.is_postgres:
                self.db.conn.cursor().execute(
                    """INSERT INTO sebi_disclosures
                        (ticker, disclosure_type, person_name, transaction_type, quantity,
                         pct_before, pct_after, transaction_date, disclosed_at, source, link)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'bse', %s)
                        ON CONFLICT (ticker, disclosure_type, person_name, transaction_date, quantity)
                        DO NOTHING""",
                    (ticker, kind, person, txn, qty, pct_before, pct_after,
                     disc_dt.date() if disc_dt else None, disc_dt, link),
                )
            else:
                self.db.conn.execute(
                    f"""INSERT OR IGNORE INTO sebi_disclosures
                         (ticker, disclosure_type, person_name, transaction_type, quantity,
                          pct_before, pct_after, transaction_date, disclosed_at, source, link)
                          VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'bse', {p})""",
                    (ticker, kind, person, txn, qty, pct_before, pct_after,
                     disc_dt.date().isoformat() if disc_dt else None,
                     disc_dt.isoformat() if disc_dt else None, link),
                )
            self.db.conn.commit()
            return True
        except Exception as exc:
            logger.debug("sebi upsert failed: %s", exc)
            self.db.conn.rollback()
            return False

    def refresh_all(self, lookback_days: int = 14) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tickers:
            try:
                out[t] = self.refresh_ticker(t, lookback_days)
            except Exception as exc:
                logger.warning("sebi refresh failed ticker=%s err=%s", t, exc)
                out[t] = 0
        return out
