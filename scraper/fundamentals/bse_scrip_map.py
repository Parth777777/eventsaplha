"""
Ticker → BSE scrip code map (dynamic, full-universe).

BSE endpoints take numeric scrip codes, not NSE tickers. This module is now
backed by the BSE universe cache produced by scraper/bse_stock_universe.py
(~5000 active scrips), instead of the prior hand-curated 32-ticker dict.

Public API kept stable so existing consumers (bse_filing_fetcher.py, etc.)
keep working — `get_scrip(ticker) -> str` returns the BSE numeric code or ''.

New helpers:
    get_yf_symbol(ticker) -> str
        Best yfinance suffix to use. Returns '<ticker>.NS' when the ticker
        appears in the NSE universe (preferred — higher liquidity / better
        intraday), else '<scrip>.BO' when only BSE-listed.

    is_bse_only(ticker) -> bool
        True iff the ticker is NOT on NSE (BSE-exclusive).

The cache may not be populated on first import — fall back to the legacy
hand-curated map for the historical 32 monitored stocks so we never regress.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Legacy hand-curated fallback — used only when the dynamic cache is empty
# (first-ever run before scraper/bse_stock_universe.py has executed once).
_LEGACY_FALLBACK = {
    "INFY": "500209", "TCS": "532540", "WIPRO": "507685", "LT": "500510",
    "RELIANCE": "500325", "HDFCBANK": "500180", "ICICIBANK": "532174",
    "SBIN": "500112", "BAJAJFINSV": "532978", "MARUTI": "532500",
    "TATASTEEL": "500470", "JSWSTEEL": "500228", "ADANIGREEN": "541450",
    "ADANIPORTS": "532921", "SUNPHARMA": "524715", "DIVISLAB": "532488",
    "HINDUNILVR": "500696", "ITC": "500875", "NESTLEIND": "500790",
    "AXISBANK": "532215", "KNRCON": "532942", "TECHM": "532755",
    "HCLTECH": "532281", "BAJAJ-AUTO": "532977", "BHARTIARTL": "532454",
    "CIPLA": "500087", "LUPIN": "500257", "POWERGRID": "532898",
    "NTPC": "532555", "COALINDIA": "533278", "IOC": "530965",
    "TATAPOWER": "500400",
}

# Module-level cache — lazily populated on first access. {ticker → scrip_code}
_BSE_BY_TICKER: dict[str, str] | None = None
_NSE_TICKERS: set[str] | None = None


def _load_full_map() -> None:
    """One-time load of the dynamic map from the BSE + NSE universe caches.

    Idempotent — repeated calls are cheap (no-op after the first success).
    Defensive — if either cache import fails we keep the legacy fallback so
    the historically-monitored 32 stocks always resolve.
    """
    global _BSE_BY_TICKER, _NSE_TICKERS
    if _BSE_BY_TICKER is not None:
        return

    bse: dict[str, dict] = {}
    nse: dict[str, dict] = {}
    try:
        from scraper.bse_stock_universe import get_bse_universe
        bse = get_bse_universe() or {}
    except Exception as e:
        logger.warning(f'[bse_scrip_map] BSE universe import failed: {e}')

    try:
        from scraper.stock_universe import get_stock_universe
        nse = get_stock_universe() or {}
    except Exception as e:
        logger.warning(f'[bse_scrip_map] NSE universe import failed: {e}')

    by_ticker: dict[str, str] = dict(_LEGACY_FALLBACK)
    for tk, meta in bse.items():
        code = (meta.get('scrip_code') or '').strip()
        if code:
            by_ticker[tk.upper()] = code

    _BSE_BY_TICKER = by_ticker
    _NSE_TICKERS = {t.upper() for t in nse.keys()}
    logger.info(
        f'[bse_scrip_map] loaded {len(by_ticker)} BSE scrips '
        f'({len(_NSE_TICKERS)} NSE tickers known)'
    )


def get_scrip(ticker: str) -> str:
    """Return the BSE numeric scrip code for a ticker, or '' if unknown.

    Kept signature-stable for existing callers (bse_filing_fetcher.py:27,186).
    """
    if not ticker:
        return ''
    _load_full_map()
    return (_BSE_BY_TICKER or {}).get(ticker.upper(), '')


def get_yf_symbol(ticker: str) -> str:
    """Best yfinance suffix for the ticker.

    Prefers '.NS' (NSE) when the ticker is in the NSE universe — NSE has
    deeper liquidity and better intraday coverage in yfinance. Falls back
    to '.BO' when BSE-exclusive. Returns just the ticker if neither known
    (lets caller decide).
    """
    if not ticker:
        return ticker
    _load_full_map()
    t = ticker.upper()
    if _NSE_TICKERS and t in _NSE_TICKERS:
        return f'{t}.NS'
    if (_BSE_BY_TICKER or {}).get(t):
        return f'{t}.BO'
    # Default to NSE suffix — preserves historical behavior for unmapped names
    return f'{t}.NS'


def is_bse_only(ticker: str) -> bool:
    """True iff the ticker is in BSE universe but NOT in NSE universe."""
    if not ticker:
        return False
    _load_full_map()
    t = ticker.upper()
    in_bse = bool((_BSE_BY_TICKER or {}).get(t))
    in_nse = bool(_NSE_TICKERS and t in _NSE_TICKERS)
    return in_bse and not in_nse


# Back-compat alias — some legacy callers reference BSE_SCRIP_CODES directly.
# Resolves lazily via __getattr__ so import side-effects stay cheap.
def __getattr__(name: str):
    if name == 'BSE_SCRIP_CODES':
        _load_full_map()
        return _BSE_BY_TICKER or {}
    raise AttributeError(name)
