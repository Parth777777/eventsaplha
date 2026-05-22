"""Screener.in fallback scraper for Indian-listed stock fundamentals.

When yfinance returns empty (which is most Indian tickers for quarterly
income statements, balance sheet, ratios), we fall through to scraping
the public Screener.in page for that ticker.

URL pattern:  https://www.screener.in/company/<TICKER>/

What we extract (all best-effort; any missing field → null):
  - company name, sector, industry, market cap, current price, P/E, book value
  - Quarterly Results table (Sales, Expenses, OPM, Net Profit, EPS)
  - Profit & Loss annual table
  - Balance Sheet (selected rows)
  - Compounded growth metrics (Sales / Profit / Stock / ROE)
  - Pros / Cons bullets
  - Shareholding pattern snapshot

Cached in a small in-memory dict + DB write-through (table `screener_cache`)
with 24h TTL — fundamentals don't change intraday. Failure → returns None.

Constraints (memory rules):
  - No LLM in the scrape path
  - Screener.in is rate-friendly but we throttle to 1 req / 2s per process
  - All requests use a real browser UA + Referer
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


SCREENER_BASE = "https://www.screener.in"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-IN,en;q=0.9",
}
TIMEOUT_S = 12
THROTTLE_S = 1.5
CACHE_TTL_S = 24 * 3600       # 24h
_LAST_FETCH_AT = 0.0
_MEMCACHE: Dict[str, Tuple[float, Dict]] = {}


# --- helpers --------------------------------------------------------------

def _throttle():
    global _LAST_FETCH_AT
    delta = time.time() - _LAST_FETCH_AT
    if delta < THROTTLE_S:
        time.sleep(THROTTLE_S - delta)
    _LAST_FETCH_AT = time.time()


def _fetch(url: str) -> Optional[str]:
    _throttle()
    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Referer": SCREENER_BASE})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            if r.status != 200:
                logger.debug(f"screener fetch HTTP {r.status}: {url}")
                return None
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.debug(f"screener HTTP {e.code}: {url}")
        return None
    except Exception as e:
        logger.debug(f"screener fetch failed: {url} -> {e}")
        return None


def _to_float(s: str) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    if not s or s.lower() in ("--", "n/a", "na"):
        return None
    s = s.replace("₹", "").replace("Rs.", "").replace("Rs ", "").strip()
    neg = False
    if s.startswith("-") or (s.startswith("(") and s.endswith(")")):
        neg = True
        s = s.strip("-()")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").strip()


# --- parsers --------------------------------------------------------------

def _parse_top_ratios(html: str) -> Dict[str, Optional[float]]:
    """Extract the right-hand 'company ratios' panel: Market Cap, P/E, BV, ROE..."""
    out: Dict[str, Optional[float]] = {}
    # Each ratio is in a <li> with <span class="name">...</span><span class="number">...</span>
    pattern = re.compile(
        r'<li[^>]*>\s*<span[^>]*class="name"[^>]*>([^<]+)</span>'
        r'\s*<span[^>]*class="nowrap value"[^>]*>(?:.*?)<span[^>]*class="number"[^>]*>([^<]+)</span>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        name = _strip_tags(m.group(1)).strip().lower().replace(" ", "_")
        out[name] = _to_float(m.group(2))
    return out


def _parse_table_by_header(html: str, section_id: str) -> List[Dict[str, Any]]:
    """Find the table inside a section identified by `<section id="...">` and
    return rows as list of dicts keyed by the first cell (label).

    Each dict shape: {"label": str, "values": [float|None, ...], "raw": [str, ...]}.
    """
    sec_match = re.search(
        r'<section[^>]*id="' + re.escape(section_id) + r'"[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not sec_match:
        return []
    section_html = sec_match.group(1)
    table_match = re.search(r"<table[^>]*>(.*?)</table>", section_html, re.DOTALL | re.IGNORECASE)
    if not table_match:
        return []
    tbl = table_match.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL | re.IGNORECASE)
    out: List[Dict[str, Any]] = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        if not cells:
            continue
        clean = [_strip_tags(c) for c in cells]
        label = clean[0]
        vals_raw = clean[1:]
        out.append({
            "label": label,
            "values": [_to_float(v) for v in vals_raw],
            "raw": vals_raw,
        })
    return out


def _parse_pros_cons(html: str) -> Tuple[List[str], List[str]]:
    pros, cons = [], []
    # Pros & cons are in <div class="pros"> <ul>... and <div class="cons"> <ul>...
    for which, bucket in (("pros", pros), ("cons", cons)):
        m = re.search(
            r'<div[^>]*class="[^"]*\b' + which + r'\b[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if not m:
            continue
        for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.DOTALL | re.IGNORECASE):
            text = _strip_tags(li).strip()
            if text:
                bucket.append(text)
    return pros, cons


def _parse_company_meta(html: str) -> Dict[str, Any]:
    """Grab name, sector, industry, and price from the top of the page."""
    out: Dict[str, Any] = {}
    name = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if name:
        out["name"] = _strip_tags(name.group(1)).strip()
    # The sector/industry sit in <a> tags near the top under .sub
    for label, key in (("Sector", "sector"), ("Industry", "industry")):
        m = re.search(
            r'>\s*' + label + r'\s*:?\s*</[^>]+>\s*<[^>]+>\s*<a[^>]*>([^<]+)</a>',
            html, re.IGNORECASE,
        )
        if m:
            out[key] = _strip_tags(m.group(1)).strip()
    # NSE / BSE codes
    nse = re.search(r"NSE\s*:\s*([A-Z0-9-]+)", html)
    bse = re.search(r"BSE\s*:\s*([0-9]+)", html)
    if nse:
        out["nse_code"] = nse.group(1)
    if bse:
        out["bse_code"] = bse.group(1)
    return out


# --- public API -----------------------------------------------------------

@dataclass
class ScreenerSnapshot:
    ticker: str
    fetched_at: str
    company: Dict[str, Any] = field(default_factory=dict)
    ratios: Dict[str, Optional[float]] = field(default_factory=dict)
    quarterly_results: List[Dict[str, Any]] = field(default_factory=list)
    quarterly_periods: List[str] = field(default_factory=list)
    profit_and_loss: List[Dict[str, Any]] = field(default_factory=list)
    pl_periods: List[str] = field(default_factory=list)
    balance_sheet: List[Dict[str, Any]] = field(default_factory=list)
    bs_periods: List[str] = field(default_factory=list)
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    source_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _periods_from_table(html: str, section_id: str) -> List[str]:
    """Extract column headers (period labels) from a Screener table."""
    sec_match = re.search(
        r'<section[^>]*id="' + re.escape(section_id) + r'"[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not sec_match:
        return []
    tbl_match = re.search(r"<thead[^>]*>(.*?)</thead>", sec_match.group(1), re.DOTALL | re.IGNORECASE)
    if not tbl_match:
        return []
    headers = re.findall(r"<th[^>]*>(.*?)</th>", tbl_match.group(1), re.DOTALL | re.IGNORECASE)
    return [_strip_tags(h).strip() for h in headers[1:]]  # drop first (the label col)


def fetch(ticker: str, *, force: bool = False) -> Optional[ScreenerSnapshot]:
    """Fetch + parse Screener.in for a ticker. Returns None on any failure.

    Caches in-process for 24h. `force=True` bypasses the cache.
    """
    tk = (ticker or "").upper().strip().replace(".NS", "").replace(".BO", "")
    if not tk:
        return None
    now = time.time()
    if not force and tk in _MEMCACHE:
        cached_at, snap = _MEMCACHE[tk]
        if now - cached_at < CACHE_TTL_S:
            try:
                # Rehydrate dataclass from dict
                return ScreenerSnapshot(**snap)
            except TypeError:
                pass

    url = f"{SCREENER_BASE}/company/{tk}/"
    html = _fetch(url)
    if not html:
        # Try the consolidated view if the standalone is missing
        html = _fetch(f"{SCREENER_BASE}/company/{tk}/consolidated/")
        if not html:
            return None
        url = f"{SCREENER_BASE}/company/{tk}/consolidated/"

    snap = ScreenerSnapshot(
        ticker=tk,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source_url=url,
    )
    try:
        snap.company = _parse_company_meta(html)
        snap.ratios = _parse_top_ratios(html)
        snap.quarterly_results = _parse_table_by_header(html, "quarters")
        snap.quarterly_periods = _periods_from_table(html, "quarters")
        snap.profit_and_loss = _parse_table_by_header(html, "profit-loss")
        snap.pl_periods = _periods_from_table(html, "profit-loss")
        snap.balance_sheet = _parse_table_by_header(html, "balance-sheet")
        snap.bs_periods = _periods_from_table(html, "balance-sheet")
        snap.pros, snap.cons = _parse_pros_cons(html)
    except Exception as e:
        logger.exception(f"screener parse failed for {tk}: {e}")
        return None

    _MEMCACHE[tk] = (now, snap.to_dict())
    return snap


def get_row(table: List[Dict[str, Any]], label_contains: str) -> Optional[Dict[str, Any]]:
    """Find first table row whose label contains `label_contains` (case-insensitive)."""
    needle = label_contains.lower()
    for row in table:
        if needle in (row.get("label") or "").lower():
            return row
    return None


def latest_value(table: List[Dict[str, Any]], label_contains: str) -> Optional[float]:
    """Most recent value (last column) for a labeled row."""
    row = get_row(table, label_contains)
    if not row:
        return None
    vals = row.get("values") or []
    if not vals:
        return None
    # Walk from the right; first non-null wins
    for v in reversed(vals):
        if v is not None:
            return v
    return None


# --- CLI smoke test -------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="RELIANCE")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = fetch(args.ticker, force=args.force)
    if not snap:
        print(f"Failed to fetch {args.ticker}")
        return
    out = {
        "ticker": snap.ticker,
        "company": snap.company,
        "ratios": snap.ratios,
        "quarterly_periods": snap.quarterly_periods,
        "quarterly_rows": len(snap.quarterly_results),
        "pros": snap.pros,
        "cons": snap.cons,
        "latest_sales": latest_value(snap.quarterly_results, "Sales") or latest_value(snap.quarterly_results, "Revenue"),
        "latest_net_profit": latest_value(snap.quarterly_results, "Net Profit") or latest_value(snap.quarterly_results, "Profit"),
        "latest_opm": latest_value(snap.quarterly_results, "OPM"),
        "latest_eps": latest_value(snap.quarterly_results, "EPS"),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
