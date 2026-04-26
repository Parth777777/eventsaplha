"""
BSE filings fetcher — pulls corporate announcements + PDF text per scrip.

For each monitored ticker, fetches recent corporate announcements, downloads
PDFs, extracts text with pdfplumber, and stores both raw text and extracted
numeric claims for later forensic matching.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import pdfplumber  # type: ignore

    PDF_OK = True
except Exception:
    pdfplumber = None
    PDF_OK = False

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


def _classify_filing(title: str) -> str:
    t = (title or "").lower()
    if "quarterly" in t or "audited" in t and "financial" in t or re.search(r"\bq[1-4]\b", t):
        return "quarterly_result"
    if "order" in t and ("received" in t or "intimation" in t or "won" in t or "contract" in t):
        return "order_intimation"
    if "agm" in t or "annual general meeting" in t:
        return "agm"
    if "bonus" in t or "split" in t or "dividend" in t:
        return "corporate_action"
    return "other"


# Numeric extraction regex library
NUMBER_RE = re.compile(
    r"(?P<label>[A-Za-z ]{3,60}?)\s*[:\-]?\s*(?:₹|Rs\.?|INR|USD|\$)?\s*(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>crore|cr|lakh|million|mn|billion|bn|%|percent)?",
    re.IGNORECASE,
)

FIELD_ALIASES = {
    "revenue": ["revenue from operations", "total revenue", "total income", "net sales", "turnover"],
    "net_profit": ["profit after tax", "net profit", "pat", "profit for the period"],
    "operating_profit": ["ebitda", "operating profit", "operating income"],
    "order_value": ["order value", "contract value", "order worth", "order size"],
    "eps": ["earnings per share", "eps", "basic eps"],
    "margin": ["operating margin", "ebitda margin", "pat margin", "profit margin"],
}


_CANON_SORTED = sorted(
    [(alias, field) for field, aliases in FIELD_ALIASES.items() for alias in aliases],
    key=lambda x: -len(x[0]),
)


def _canonical_field(label: str) -> Optional[str]:
    """Match label to canonical field; longest alias wins to avoid collisions
    like 'ebitda' matching before 'ebitda margin'."""
    lab = label.lower().strip()
    for alias, field in _CANON_SORTED:
        if alias in lab:
            return field
    return None


def _normalize_unit(value: float, unit: Optional[str]) -> Tuple[float, str]:
    unit = (unit or "").lower().strip()
    if unit in ("crore", "cr"):
        return value, "cr"
    if unit in ("lakh",):
        return value / 100.0, "cr"  # 100 lakh = 1 cr
    if unit in ("million", "mn"):
        return value * 10.0 / 83.0, "cr"  # rough INR/USD conversion
    if unit in ("billion", "bn"):
        return value * 1000.0 * 10.0 / 83.0, "cr"
    if unit in ("%", "percent"):
        return value, "pct"
    return value, "raw"


def extract_numbers_from_text(text: str) -> List[Dict]:
    """Pull labelled numbers from free-form filing text."""
    out: List[Dict] = []
    for m in NUMBER_RE.finditer(text):
        label = m.group("label").strip(": -\n\t")
        raw_val = m.group("value").replace(",", "")
        unit = m.group("unit")
        try:
            val = float(raw_val)
        except ValueError:
            continue
        canonical = _canonical_field(label)
        if not canonical:
            continue
        norm_val, norm_unit = _normalize_unit(val, unit)
        out.append({
            "field_name": canonical,
            "value": norm_val,
            "unit": norm_unit,
            "raw_text": m.group(0)[:200],
        })
    return out


class BSEFilingFetcher:
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

    def _download_pdf(self, url: str) -> Optional[bytes]:
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            return None
        try:
            resp = request_with_retry("GET", url, source="bse", headers=HEADERS, timeout=25, attempts=2)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception as exc:
            logger.debug("pdf download failed url=%s err=%s", url, exc)
            return None

    @staticmethod
    def _pdf_to_text(pdf_bytes: bytes) -> str:
        if not PDF_OK:
            return ""
        import io

        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                parts = []
                for page in pdf.pages[:20]:  # cap pages for cost
                    parts.append(page.extract_text() or "")
                return "\n".join(parts)
        except Exception as exc:
            logger.debug("pdfplumber failed: %s", exc)
            return ""

    def refresh_ticker(self, ticker: str, lookback_days: int = 14) -> int:
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
            if not link or ftype == "other":
                continue
            pdf = self._download_pdf(link)
            if not pdf:
                continue
            text = self._pdf_to_text(pdf)
            filing_id = self._upsert_filing(ticker, ftype, title, link, filed_at, text)
            if filing_id:
                inserted += 1
                inc("scraper_filings_total", ticker=ticker, type=ftype)
                if text:
                    self._store_numbers(filing_id, text)
        return inserted

    def _upsert_filing(self, ticker: str, ftype: str, title: str, pdf_url: str,
                        filed_at: Optional[datetime], text: str) -> Optional[int]:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            cursor = self.db.conn.cursor()
            if self.db.is_postgres:
                cursor.execute(
                    """INSERT INTO filings (ticker, filing_type, title, pdf_url, filed_at, source, raw_text)
                       VALUES (%s, %s, %s, %s, %s, 'bse', %s)
                       ON CONFLICT (ticker, pdf_url) DO UPDATE SET raw_text = EXCLUDED.raw_text
                       RETURNING id""",
                    (ticker, ftype, title[:300], pdf_url, filed_at, text[:30000]),
                )
                row = cursor.fetchone()
                fid = row[0] if row else None
            else:
                cursor.execute(
                    f"""INSERT OR REPLACE INTO filings
                         (ticker, filing_type, title, pdf_url, filed_at, source, raw_text)
                         VALUES ({p}, {p}, {p}, {p}, {p}, 'bse', {p})""",
                    (ticker, ftype, title[:300], pdf_url, filed_at.isoformat() if filed_at else None, text[:30000]),
                )
                fid = cursor.lastrowid
            self.db.conn.commit()
            return fid
        except Exception as exc:
            logger.warning("filing upsert failed: %s", exc)
            self.db.conn.rollback()
            return None

    def _store_numbers(self, filing_id: int, text: str) -> None:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        rows = extract_numbers_from_text(text)
        if not rows:
            return
        try:
            cursor = self.db.conn.cursor()
            for r in rows:
                cursor.execute(
                    f"""INSERT INTO filing_numbers (filing_id, field_name, value, unit, raw_text)
                         VALUES ({p}, {p}, {p}, {p}, {p})""",
                    (filing_id, r["field_name"], r["value"], r["unit"], r["raw_text"]),
                )
            self.db.conn.commit()
        except Exception as exc:
            logger.debug("filing numbers insert failed: %s", exc)
            self.db.conn.rollback()

    def refresh_all(self, lookback_days: int = 14) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tickers:
            try:
                out[t] = self.refresh_ticker(t, lookback_days)
            except Exception as exc:
                logger.warning("filing refresh failed ticker=%s err=%s", t, exc)
                out[t] = 0
        return out
