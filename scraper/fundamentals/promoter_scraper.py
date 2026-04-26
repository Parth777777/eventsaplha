"""
Promoter & shareholding scraper.

Combines quarterly XBRL shareholding pattern (primary) with screener.in
fallback (when XBRL isn't yet published for the most recent quarter). Writes
into promoter_holdings and promoters tables.

XBRL extraction: BSE publishes one XBRL per scrip per quarter. The shareholding
pattern is a standardized schema so a targeted XPath-ish walk over tags works
without a full XBRL toolkit.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from net import request_with_retry
from ratelimit import get_bucket
from metrics import inc
from fundamentals.bse_scrip_map import get_scrip

logger = logging.getLogger(__name__)

BSE_SHP_LANDING = "https://www.bseindia.com/corporates/shpPublicShareholder.aspx"
BSE_SHP_LIST_API = "https://api.bseindia.com/BseIndiaAPI/api/CorpShareholdings/w"
BSE_SHP_DETAIL_API = "https://api.bseindia.com/BseIndiaAPI/api/CorpShareholdingsXBRL/w"

HEADERS = {
    "User-Agent": "Mozilla/5.0 alphaevent/0.1",
    "Referer": "https://www.bseindia.com/",
}

# XBRL tag fragments we look for. The real XBRL schema uses fully qualified
# names like "in-capmkt:PromoterAndPromoterGroupPercentageOfTotalSharesHeld".
# We match on the local-name tail to stay robust across schema versions.
XBRL_FIELD_MAP = {
    "promoter_pct": [
        "PromoterAndPromoterGroupPercentageOfTotalSharesHeld",
        "PercentageOfSharesHeldByPromoterAndPromoterGroup",
    ],
    "promoter_pledge_pct": [
        "SharesPledgedAsPercentageOfTotalPromoterShareholding",
        "PercentageOfSharesPledgedByPromoterAndPromoterGroup",
        "PercentageOfSharesEncumbered",
    ],
    "fii_pct": [
        "ForeignPortfolioInvestorPercentageOfTotalSharesHeld",
        "PercentageOfSharesHeldByForeignPortfolioInvestors",
    ],
    "dii_pct": [
        "DomesticInstitutionalInvestorsPercentageOfTotalSharesHeld",
        "PercentageOfSharesHeldByDomesticInstitutions",
    ],
    "public_pct": [
        "PublicShareholdingPercentageOfTotalSharesHeld",
        "PercentageOfPublicShareholding",
    ],
    "mutual_fund_pct": [
        "MutualFundsPercentageOfTotalSharesHeld",
    ],
    "insurance_pct": [
        "InsuranceCompaniesPercentageOfTotalSharesHeld",
    ],
}


def _localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_xbrl_blob(xbrl_bytes: bytes) -> Dict[str, float]:
    """Extract known shareholding fields from raw XBRL bytes.

    Robust to namespace differences — matches on local-name substrings.
    """
    results: Dict[str, float] = {}
    try:
        tree = ET.fromstring(xbrl_bytes)
    except ET.ParseError as exc:
        logger.warning("xbrl parse error: %s", exc)
        return results

    for elem in tree.iter():
        ln = _localname(elem.tag)
        text = (elem.text or "").strip()
        if not text:
            continue
        # Try to coerce to float
        val: Optional[float] = None
        try:
            val = float(text)
        except ValueError:
            m = re.search(r"-?\d+(?:\.\d+)?", text)
            if m:
                try:
                    val = float(m.group(0))
                except ValueError:
                    val = None
        if val is None:
            continue
        for field, needles in XBRL_FIELD_MAP.items():
            if field in results:
                continue
            if any(n in ln for n in needles):
                results[field] = val
                break
    return results


class PromoterScraper:
    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)

    def _list_filings_for_scrip(self, scrip_code: str) -> List[Dict]:
        """Fetch the list of historical shareholding filings for a scrip."""
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            inc("scraper_ratelimit_skips_total", source="bse")
            return []
        try:
            resp = request_with_retry(
                "GET",
                BSE_SHP_LIST_API,
                source="bse",
                params={"scripcode": scrip_code, "qtrid": "0.00"},
                headers=HEADERS,
                attempts=2,
            )
            data = resp.json()
            # Response shape varies; be forgiving
            rows = data if isinstance(data, list) else data.get("Table") or data.get("table") or []
            return list(rows)
        except Exception as exc:
            logger.debug("list filings failed scrip=%s err=%s", scrip_code, exc)
            return []

    def _download_xbrl(self, xbrl_url: str) -> Optional[bytes]:
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            return None
        try:
            resp = request_with_retry("GET", xbrl_url, source="bse", headers=HEADERS, attempts=2, timeout=20)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception as exc:
            logger.debug("xbrl download failed url=%s err=%s", xbrl_url, exc)
            return None

    @staticmethod
    def _parse_quarter_end(row: Dict) -> Optional[date]:
        # Try common fields
        for key in ("Qtr_Ended", "QtrEnded", "QTR_ENDED", "quarter_end", "QUARTER_END", "ToDate"):
            v = row.get(key)
            if v:
                try:
                    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
                except ValueError:
                    try:
                        return datetime.strptime(str(v)[:10], "%d-%m-%Y").date()
                    except ValueError:
                        continue
        return None

    def refresh_ticker(self, ticker: str, max_quarters: int = 8) -> int:
        """Pull up to max_quarters of filings for a ticker. Returns count inserted."""
        scrip = get_scrip(ticker)
        if not scrip:
            return 0
        rows = self._list_filings_for_scrip(scrip)
        inserted = 0
        for row in rows[:max_quarters]:
            q_end = self._parse_quarter_end(row)
            if q_end is None:
                continue
            xbrl_url = row.get("XBRLFile") or row.get("xbrlfile") or row.get("XbrlLink") or ""
            if not xbrl_url:
                continue
            content = self._download_xbrl(xbrl_url)
            if not content:
                continue
            fields = parse_xbrl_blob(content)
            if not fields:
                continue
            if self._upsert_holdings(ticker, q_end, fields, xbrl_url):
                inserted += 1
                inc("scraper_promoter_inserts_total", ticker=ticker)
        return inserted

    def _upsert_holdings(self, ticker: str, quarter_end: date, fields: Dict[str, float], xbrl_url: str) -> bool:
        p = "%s" if getattr(self.db, "is_postgres", False) else "?"
        try:
            if self.db.is_postgres:
                self.db.conn.cursor().execute(
                    """
                    INSERT INTO promoter_holdings (ticker, quarter_end, promoter_pct, promoter_pledge_pct,
                        fii_pct, dii_pct, public_pct, mutual_fund_pct, insurance_pct, raw_xbrl_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, quarter_end) DO UPDATE SET
                        promoter_pct = EXCLUDED.promoter_pct,
                        promoter_pledge_pct = EXCLUDED.promoter_pledge_pct,
                        fii_pct = EXCLUDED.fii_pct,
                        dii_pct = EXCLUDED.dii_pct,
                        public_pct = EXCLUDED.public_pct,
                        mutual_fund_pct = EXCLUDED.mutual_fund_pct,
                        insurance_pct = EXCLUDED.insurance_pct,
                        raw_xbrl_url = EXCLUDED.raw_xbrl_url
                    """,
                    (
                        ticker, quarter_end,
                        fields.get("promoter_pct"),
                        fields.get("promoter_pledge_pct"),
                        fields.get("fii_pct"),
                        fields.get("dii_pct"),
                        fields.get("public_pct"),
                        fields.get("mutual_fund_pct"),
                        fields.get("insurance_pct"),
                        xbrl_url,
                    ),
                )
            else:
                self.db.conn.execute(
                    f"""INSERT OR REPLACE INTO promoter_holdings (ticker, quarter_end, promoter_pct,
                            promoter_pledge_pct, fii_pct, dii_pct, public_pct, mutual_fund_pct,
                            insurance_pct, raw_xbrl_url)
                        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})""",
                    (
                        ticker, quarter_end.isoformat(),
                        fields.get("promoter_pct"),
                        fields.get("promoter_pledge_pct"),
                        fields.get("fii_pct"),
                        fields.get("dii_pct"),
                        fields.get("public_pct"),
                        fields.get("mutual_fund_pct"),
                        fields.get("insurance_pct"),
                        xbrl_url,
                    ),
                )
            self.db.conn.commit()
            return True
        except Exception as exc:
            logger.warning("holdings upsert failed ticker=%s q=%s err=%s", ticker, quarter_end, exc)
            self.db.conn.rollback()
            return False

    def refresh_all(self) -> Dict[str, int]:
        results: Dict[str, int] = {}
        for t in self.tickers:
            try:
                results[t] = self.refresh_ticker(t)
            except Exception as exc:
                logger.warning("promoter refresh failed ticker=%s err=%s", t, exc)
                results[t] = 0
        return results


def compute_trend_flags(rows: List[Dict]) -> Dict[str, bool]:
    """Given chronological list of holdings rows (oldest first), derive trend flags."""
    flags = {
        "pledge_rising": False,
        "promoter_increasing": False,
        "promoter_dilution": False,
        "fii_exit": False,
    }
    if len(rows) < 2:
        return flags
    latest = rows[-1]
    prev = rows[-2]

    def delta(key):
        a = latest.get(key)
        b = prev.get(key)
        if a is None or b is None:
            return None
        return float(a) - float(b)

    d_pledge = delta("promoter_pledge_pct")
    d_prom = delta("promoter_pct")
    d_fii = delta("fii_pct")

    if d_pledge is not None and d_pledge >= 5:
        flags["pledge_rising"] = True
    if d_prom is not None and d_prom >= 0.5:
        flags["promoter_increasing"] = True
    if d_prom is not None and d_prom <= -1.0:
        flags["promoter_dilution"] = True
    if d_fii is not None and d_fii <= -3.0:
        flags["fii_exit"] = True
    return flags
