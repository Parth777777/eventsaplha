"""
Tickwave - Complete BSE Stock Universe (Track 1 Phase A)

Counterpart to scraper/stock_universe.py. Fetches the full list of active
BSE-listed equities, including BSE-exclusive stocks not on NSE, so the app
universe is no longer NSE-only (trader feedback 2026-05-17).

Source:
    The BSE search API at api.bseindia.com is unauthenticated but rate-limits
    aggressively (~30 req/min observed). We pull GROUP A first (large-cap,
    ~500 names), then B, T as we need them — the full A+B+T set is ~5000
    scrips. Cached as JSON on disk for CACHE_MAX_AGE_HOURS (default weekly).

Output schema (matches stock_universe.py for downstream compatibility):
    {
        "TICKER":  {                    # NSE symbol when dual-listed
            "name":         str,
            "sector":       str,         # via _classify_sector reuse
            "exchange":     "BSE" | "DUAL",
            "scrip_code":   "500325",   # numeric, required for BSE endpoints
            "group":        "A" | "B" | "T",
            "isin":         "INE002A01018",
        }
    }

When a BSE scrip is dual-listed on NSE (matched on ISIN), the key uses the
NSE ticker so existing consumers continue to work. BSE-exclusive scrips key
on the BSE security ID (often identical to NSE-style ticker).

Run:
    python -m scraper.bse_stock_universe              # download + cache
    python -m scraper.bse_stock_universe --group A    # just GROUP A
"""

import os
import json
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import requests

try:
    # Reuse sector classifier so we don't duplicate 500 lines of keyword maps
    from scraper.stock_universe import _classify_sector, get_stock_universe
except Exception:
    _classify_sector = lambda name, ticker=None: 'OTHER'  # noqa
    get_stock_universe = lambda: {}

try:
    # Central rate-budget bucket; falls back to a no-op delay if absent
    from scraper.ratelimit import get_bucket  # type: ignore
    _BSE_BUCKET = get_bucket('bse')
except Exception:
    _BSE_BUCKET = None

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
CACHE_FILE = DATA_DIR / 'bse_universe.json'
CACHE_MAX_AGE_HOURS = 168  # weekly refresh, same cadence as NSE

# BSE group → priority. A = large-caps (refreshed most aggressively).
GROUPS = ('A', 'B', 'T')

# BSE list-of-scrips endpoint. Public, no auth, but rate-limited.
# strType=Active filters out delisted/suspended scrips.
BSE_LIST_URL = (
    'https://api.bseindia.com/BseIndiaAPI/api/ListOfScripCd/w'
    '?Group={group}&Scripcode=&industry=&segment=Equity&status=Active'
)

# Required to mimic a browser — BSE blocks bare curl-style requests
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.bseindia.com/',
    'Origin': 'https://www.bseindia.com',
}


def _throttle():
    """Honor central rate-budget if available, else sleep 1.5s."""
    if _BSE_BUCKET is not None:
        try:
            _BSE_BUCKET.acquire()
            return
        except Exception:
            pass
    time.sleep(1.5)


def _fetch_group(group: str):
    """Download one BSE group (A / B / T). Returns list of raw dicts."""
    url = BSE_LIST_URL.format(group=group)
    _throttle()
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=25)
        resp.raise_for_status()
        body = resp.json()
        # BSE returns either a plain list, or a dict with the list under 'Table'
        if isinstance(body, dict) and 'Table' in body:
            return body['Table'] or []
        return body if isinstance(body, list) else []
    except Exception as e:
        logger.warning(f'[bse_universe] group {group} fetch failed: {e}')
        return []


def _normalize(row: dict, group: str) -> dict:
    """Map a BSE API row to our common shape. Defensive about missing fields."""
    # Field names observed in the BSE response (sometimes lowercase, sometimes
    # title-case depending on endpoint version) — coalesce.
    code = str(row.get('Scrip_Cd') or row.get('SCRIP_CD') or row.get('scrip_cd')
               or row.get('scrip_code') or '').strip()
    sec_id = str(row.get('scrip_id') or row.get('Scrip_Id') or row.get('SCRIP_ID')
                 or row.get('symbol') or '').strip().upper()
    name = (row.get('Scrip_Name') or row.get('SCRIP_NAME') or row.get('scrip_name')
            or row.get('company_name') or '').strip()
    isin = (row.get('ISIN_NUMBER') or row.get('ISIN') or row.get('isin_number') or '').strip()

    return {
        'scrip_code': code,
        'security_id': sec_id,  # the BSE "symbol" — usually matches NSE ticker
        'name': name,
        'isin': isin,
        'group': group,
    }


def download_bse_stocks(groups=GROUPS) -> dict:
    """Download requested BSE groups and merge with NSE universe via ISIN join.

    Returns the combined universe dict (BSE + dual-listed). Caches to disk.
    """
    # Build NSE ISIN → ticker index by re-reading the NSE cache. If the cache
    # doesn't yet expose ISIN we still produce a BSE-keyed universe; the next
    # NSE refresh can populate the join.
    nse = get_stock_universe() or {}
    nse_by_isin = {}
    for tk, meta in nse.items():
        isin = (meta.get('isin') or '').strip().upper()
        if isin:
            nse_by_isin[isin] = tk

    universe = {}
    for g in groups:
        raw = _fetch_group(g)
        logger.info(f'[bse_universe] group {g}: {len(raw)} raw rows')
        for row in raw:
            r = _normalize(row, g)
            if not r['scrip_code'] or not r['name']:
                continue

            # Prefer NSE ticker as the key when the ISIN matches a dual-listed
            # stock — keeps downstream code (which keys on NSE ticker) working.
            nse_ticker = nse_by_isin.get(r['isin'].upper()) if r['isin'] else None
            key = nse_ticker or r['security_id'] or r['scrip_code']
            if not key:
                continue

            sector = _classify_sector(r['name'], key)
            universe[key] = {
                'name':       r['name'],
                'sector':     sector,
                'exchange':   'DUAL' if nse_ticker else 'BSE',
                'scrip_code': r['scrip_code'],
                'group':      r['group'],
                'isin':       r['isin'],
            }

    if not universe:
        logger.warning('[bse_universe] nothing downloaded — keeping prior cache')
        return _load_cache() or {}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        'downloaded_at': datetime.now().isoformat(),
        'groups':        list(groups),
        'count':         len(universe),
        'stocks':        universe,
    }
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    logger.info(f'[bse_universe] cached {len(universe)} BSE+DUAL stocks')
    return universe


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        downloaded = datetime.fromisoformat(data['downloaded_at'])
        if datetime.now() - downloaded > timedelta(hours=CACHE_MAX_AGE_HOURS):
            return None
        return data.get('stocks') or {}
    except Exception:
        return None


def get_bse_universe() -> dict:
    """Public accessor — cache-first, falls back to fresh download."""
    cached = _load_cache()
    if cached:
        return cached
    return download_bse_stocks() or {}


def get_combined_universe() -> dict:
    """Merged NSE + BSE universe — NSE keys win on collision (dual-listed)."""
    bse = get_bse_universe() or {}
    nse = get_stock_universe() or {}
    out = dict(bse)
    for tk, meta in nse.items():
        if tk in out:
            # Dual-listed: keep NSE metadata, attach BSE scrip_code/group
            out[tk] = {
                **meta,
                'exchange':   'DUAL',
                'scrip_code': out[tk].get('scrip_code'),
                'group':      out[tk].get('group'),
                'isin':       meta.get('isin') or out[tk].get('isin'),
            }
        else:
            out[tk] = {**meta}
    return out


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', help='Single group to fetch (A/B/T). Default: all.')
    args = ap.parse_args()

    groups = (args.group.upper(),) if args.group else GROUPS
    u = download_bse_stocks(groups=groups)
    print(f'Cached {len(u)} BSE+DUAL stocks to {CACHE_FILE}')
    sample = list(u.items())[:5]
    for tk, meta in sample:
        print(f'  {tk:14s} {meta["scrip_code"]:<8s} {meta["exchange"]:<5s} {meta["name"]}')
