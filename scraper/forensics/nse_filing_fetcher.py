"""
NSE filings fetcher — counterpart to bse_filing_fetcher.py for NSE-listed
corporate announcements. (Track 1 Phase B.)

NSE's corporate-announcements endpoint is JSON but blocks any client that
hasn't first fetched the main HTML at nseindia.com and inherited a session
cookie. We do that warmup once per session via requests.Session.

Source:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>

For each ticker:
    1. Hit the announcements endpoint
    2. Classify titles via _filing_common.classify_filing
    3. Download PDF, extract text, persist via shared upsert
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import requests

try:
    from ratelimit import get_bucket  # type: ignore
except Exception:
    get_bucket = lambda: None  # noqa

from forensics._filing_common import (
    classify_filing,
    pdf_to_text,
    download_pdf as _common_download_pdf,
    upsert_filing as _common_upsert_filing,
    store_numbers as _common_store_numbers,
)

logger = logging.getLogger(__name__)

NSE_ANN_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_WARMUP_URL = "https://www.nseindia.com/get-quotes/equity?symbol={sym}"

# NSE rejects clients that don't look browser-ish. The warmup step also sets
# a session cookie (nsit / nseappid) that the JSON endpoint requires.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NSEFilingFetcher:
    """Iterates monitored tickers and pulls NSE corporate announcements."""

    SOURCE = "nse"
    EXCHANGE = "NSE"

    def __init__(self, db, tickers: Iterable[str]):
        self.db = db
        self.tickers = list(tickers)
        self._session: Optional[requests.Session] = None

    def _ensure_session(self, symbol: str) -> requests.Session:
        """Create a session with the warmup cookie if missing. Re-warms on
        symbol change is unnecessary — the session cookie is global."""
        if self._session is not None:
            return self._session
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(NSE_WARMUP_URL.format(sym=symbol), timeout=15)
        except Exception as exc:
            logger.debug("nse warmup failed sym=%s err=%s", symbol, exc)
        self._session = s
        return s

    def _throttle(self) -> bool:
        bucket = get_bucket()
        if bucket is None:
            return True
        try:
            return bool(bucket.acquire("nse", 1.0, max_wait=5.0))
        except Exception:
            return True

    def _fetch_list(self, symbol: str, since: date) -> List[dict]:
        if not self._throttle():
            return []
        sess = self._ensure_session(symbol)
        try:
            resp = sess.get(
                NSE_ANN_URL,
                params={
                    "index":   "equities",
                    "symbol":  symbol,
                    "from_date": since.strftime("%d-%m-%Y"),
                    "to_date":   date.today().strftime("%d-%m-%Y"),
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug("nse ann list %s -> %d", symbol, resp.status_code)
                # Stale session — drop it so the next call re-warms
                self._session = None
                return []
            data = resp.json()
            # Response shape varies: sometimes a bare list, sometimes
            # {"corporate":[...]} or {"data":[...]}
            if isinstance(data, list):
                return data
            return list(data.get("corporate") or data.get("data") or [])
        except Exception as exc:
            logger.debug("nse ann list failed sym=%s err=%s", symbol, exc)
            return []

    def refresh_ticker(self, ticker: str, lookback_days: int = 14,
                       keep_other: bool = False) -> int:
        sym = (ticker or "").upper()
        if not sym:
            return 0
        since = date.today() - timedelta(days=lookback_days)
        rows = self._fetch_list(sym, since)
        inserted = 0
        for r in rows:
            # NSE response keys (observed): "subject", "desc", "an_dt", "attchmntFile"
            title = (r.get("subject") or r.get("desc") or "").strip()
            link = (r.get("attchmntFile") or r.get("attachment") or "").strip()
            if not link:
                continue
            if not link.startswith("http"):
                link = "https://www.nseindia.com" + link
            filed_at = None
            for key in ("an_dt", "exchdisstime", "sort_date"):
                v = r.get(key)
                if not v:
                    continue
                for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
                    try:
                        filed_at = datetime.strptime(str(v)[:20], fmt)
                        break
                    except ValueError:
                        continue
                if filed_at:
                    break

            ftype = classify_filing(title)
            if ftype == "other" and not keep_other:
                continue
            pdf = _common_download_pdf(link, source=self.SOURCE, headers=HEADERS)
            if not pdf:
                continue
            text = pdf_to_text(pdf)
            fid = _common_upsert_filing(
                self.db, sym, ftype, title, link, filed_at, text,
                source=self.SOURCE, exchange=self.EXCHANGE,
            )
            if fid:
                inserted += 1
                if text:
                    _common_store_numbers(self.db, fid, text)
        return inserted

    def refresh_all(self, lookback_days: int = 14, keep_other: bool = False) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tickers:
            try:
                out[t] = self.refresh_ticker(t, lookback_days, keep_other=keep_other)
            except Exception as exc:
                logger.warning("nse filing refresh failed ticker=%s err=%s", t, exc)
                out[t] = 0
        return out

    @classmethod
    def from_universe(cls, db, limit: Optional[int] = None):
        """Build a fetcher over the full NSE universe."""
        try:
            from scraper.stock_universe import get_stock_universe
        except Exception:
            from stock_universe import get_stock_universe  # type: ignore
        u = get_stock_universe() or {}
        tickers = list(u.keys())
        if limit:
            tickers = tickers[:limit]
        return cls(db, tickers)
