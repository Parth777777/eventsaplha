"""
IR-page PDF fetcher — pulls investor-presentation PDFs from each company's
IR page and extracts text/tables.

Requires a per-ticker IR page URL map (seeded for the 32 monitored tickers).
Expanded on demand as new tickers are added.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

try:
    import pdfplumber  # type: ignore

    PDF_OK = True
except Exception:
    pdfplumber = None
    PDF_OK = False

try:
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore

    OCR_OK = True
except Exception:
    pytesseract = None
    OCR_OK = False

from net import request_with_retry
from ratelimit import get_bucket
from metrics import inc
from forensics.bse_filing_fetcher import extract_numbers_from_text

logger = logging.getLogger(__name__)


# Investor-relations page URL per ticker.
# When a ticker is absent, IR fetch is skipped gracefully.
IR_PAGES = {
    "INFY": "https://www.infosys.com/investors/reports-filings/quarterly-results.html",
    "TCS": "https://www.tcs.com/investor-relations",
    "WIPRO": "https://www.wipro.com/investors/",
    "RELIANCE": "https://www.ril.com/InvestorRelations.aspx",
    "HDFCBANK": "https://www.hdfcbank.com/personal/about-us/investor-relations",
    "ICICIBANK": "https://www.icicibank.com/about-us/bcd.page",
    "MARUTI": "https://www.marutisuzuki.com/corporate/investors",
    "LT": "https://investors.larsentoubro.com/Financials.aspx",
    "TATASTEEL": "https://www.tatasteel.com/investors/",
    "SUNPHARMA": "https://sunpharma.com/investors/",
    "HINDUNILVR": "https://www.hul.co.in/investor-relations/",
    "ITC": "https://www.itcportal.com/about-itc/shareholder-value/default.aspx",
    "AXISBANK": "https://www.axisbank.com/shareholders-corner/annual-reports",
    "BHARTIARTL": "https://www.airtel.in/about-bharti/equity/results",
}


class IRPageFetcher:
    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)

    def _fetch_html(self, url: str) -> str:
        bucket = get_bucket()
        if not bucket.acquire("ir", 1.0, max_wait=5.0):
            return ""
        try:
            resp = request_with_retry(
                "GET", url, source="ir", timeout=15,
                headers={"User-Agent": "Mozilla/5.0 alphaevent/0.1"},
                attempts=2,
            )
            return resp.text or ""
        except Exception as exc:
            logger.debug("ir page fetch failed url=%s err=%s", url, exc)
            return ""

    def _extract_pdf_links(self, html: str, base_url: str) -> List[str]:
        if not html:
            return []
        links = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, flags=re.IGNORECASE)
        out: List[str] = []
        for l in links:
            if l.startswith("http"):
                out.append(l)
            else:
                out.append(urljoin(base_url, l))
        # Prioritize quarterly/investor PDFs
        scored = [(l, self._pdf_relevance(l)) for l in out]
        scored.sort(key=lambda x: -x[1])
        return [l for l, _ in scored[:10]]

    @staticmethod
    def _pdf_relevance(url: str) -> int:
        u = url.lower()
        score = 0
        for kw, w in (("quarter", 3), ("q1", 3), ("q2", 3), ("q3", 3), ("q4", 3),
                       ("earnings", 2), ("investor", 2), ("presentation", 2),
                       ("result", 2), ("annual", 1), ("20", 1)):
            if kw in u:
                score += w
        return score

    def _download(self, url: str) -> Optional[bytes]:
        bucket = get_bucket()
        if not bucket.acquire("ir", 1.0, max_wait=5.0):
            return None
        try:
            resp = request_with_retry("GET", url, source="ir", timeout=25, attempts=2)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception as exc:
            logger.debug("ir pdf download failed url=%s err=%s", url, exc)
            return None

    @staticmethod
    def _extract_text(pdf_bytes: bytes) -> str:
        if not PDF_OK:
            return ""
        import io

        text_parts: List[str] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages[:30]:
                    t = page.extract_text() or ""
                    if not t.strip() and OCR_OK:
                        # OCR fallback for image-only pages
                        try:
                            img = page.to_image(resolution=200).original
                            t = pytesseract.image_to_string(img)
                        except Exception:
                            t = ""
                    text_parts.append(t)
        except Exception as exc:
            logger.debug("ir pdf extract failed: %s", exc)
        return "\n".join(text_parts)

    def refresh_ticker(self, ticker: str) -> int:
        ir_url = IR_PAGES.get(ticker)
        if not ir_url:
            return 0
        html = self._fetch_html(ir_url)
        pdf_links = self._extract_pdf_links(html, ir_url)
        inserted = 0
        for pdf_url in pdf_links:
            pdf = self._download(pdf_url)
            if not pdf:
                continue
            text = self._extract_text(pdf)
            if not text:
                continue
            fid = self._upsert_filing(ticker, pdf_url, text)
            if fid:
                inserted += 1
                inc("scraper_ir_filings_total", ticker=ticker)
                numbers = extract_numbers_from_text(text)
                if numbers:
                    self._store_numbers(fid, numbers)
        return inserted

    def _upsert_filing(self, ticker: str, pdf_url: str, text: str) -> Optional[int]:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            cursor = self.db.conn.cursor()
            if self.db.is_postgres:
                cursor.execute(
                    """INSERT INTO filings (ticker, filing_type, title, pdf_url, source, raw_text)
                       VALUES (%s, 'ir_presentation', %s, %s, 'ir_page', %s)
                       ON CONFLICT (ticker, pdf_url) DO UPDATE SET raw_text = EXCLUDED.raw_text
                       RETURNING id""",
                    (ticker, f"IR: {urlparse(pdf_url).path[-60:]}", pdf_url, text[:30000]),
                )
                row = cursor.fetchone()
                fid = row[0] if row else None
            else:
                cursor.execute(
                    f"""INSERT OR REPLACE INTO filings
                         (ticker, filing_type, title, pdf_url, source, raw_text)
                         VALUES ({p}, 'ir_presentation', {p}, {p}, 'ir_page', {p})""",
                    (ticker, f"IR: {urlparse(pdf_url).path[-60:]}", pdf_url, text[:30000]),
                )
                fid = cursor.lastrowid
            self.db.conn.commit()
            return fid
        except Exception as exc:
            logger.debug("ir filing upsert failed: %s", exc)
            self.db.conn.rollback()
            return None

    def _store_numbers(self, filing_id: int, numbers: List[Dict]) -> None:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            cursor = self.db.conn.cursor()
            for n in numbers:
                cursor.execute(
                    f"""INSERT INTO filing_numbers (filing_id, field_name, value, unit, raw_text)
                         VALUES ({p}, {p}, {p}, {p}, {p})""",
                    (filing_id, n["field_name"], n["value"], n["unit"], n["raw_text"]),
                )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()

    def refresh_all(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tickers:
            try:
                out[t] = self.refresh_ticker(t)
            except Exception as exc:
                logger.warning("ir refresh failed ticker=%s err=%s", t, exc)
                out[t] = 0
        return out
