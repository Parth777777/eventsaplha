"""Run each broken scraper once and capture the real failure mode.

Usage: python tools/probe_scrapers.py [scraper_name]
       (with no arg, runs all)
"""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _connect():
    """Get a sqlite connection to the live DB the app uses."""
    import sqlite3
    db_path = ROOT / "data" / "tickwave.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    class _ShimDB:
        def __init__(self, c):
            self.conn = c

    return _ShimDB(conn)


def probe_bulk_deals():
    print("\n=== BULK_DEALS ===")
    from bulk_deals import fetch_nse_bulk_deals, fetch_nse_block_deals, fetch_sebi_bans
    db = _connect()
    try:
        a = fetch_nse_bulk_deals(db)
        b = fetch_nse_block_deals(db)
        c = fetch_sebi_bans(db)
        print(f"  bulk={a}  block={b}  sebi_bans={c}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()


def probe_fo_unusual():
    print("\n=== FO_UNUSUAL ===")
    try:
        from fo_signals import snapshot_and_persist
    except Exception as e:
        print(f"  IMPORT FAIL: {e}")
        return
    db = _connect()
    universe = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK']
    try:
        result = snapshot_and_persist(db, universe, is_index=False)
        print(f"  result: {result}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()


def probe_promoter_events():
    print("\n=== PROMOTER_EVENTS ===")
    try:
        # Try several known module names
        candidates = [
            ("promoter_intelligence", "fetch_promoter_events_daily"),
            ("promoter_intelligence", "refresh_promoter_events"),
            ("promoter_intelligence", "refresh_all"),
        ]
        for mod_name, fn_name in candidates:
            try:
                mod = __import__(mod_name)
                fn = getattr(mod, fn_name, None)
                if fn:
                    print(f"  trying {mod_name}.{fn_name}()")
                    db = _connect()
                    try:
                        r = fn(db) if "db" in fn.__code__.co_varnames else fn()
                    except TypeError:
                        r = fn()
                    print(f"  result: {r}")
                    return
            except ImportError:
                continue
        print("  no promoter_events module/function found")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()


def probe_sebi_disclosures():
    print("\n=== SEBI_DISCLOSURES ===")
    try:
        from forensics.sebi_disclosures import SEBIDisclosuresFetcher
    except Exception as e:
        print(f"  IMPORT FAIL: {e}")
        return
    db = _connect()
    try:
        try:
            from config import MONITORED_STOCKS
        except Exception:
            MONITORED_STOCKS = ['RELIANCE', 'TCS', 'INFY']
        f = SEBIDisclosuresFetcher(db, MONITORED_STOCKS[:5])
        r = f.refresh_all()
        print(f"  result: {r}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()


def probe_nse_filings():
    print("\n=== NSE_FILINGS ===")
    try:
        from forensics.nse_filing_fetcher import NSEFilingFetcher
    except Exception as e:
        print(f"  IMPORT FAIL: {e}")
        return
    db = _connect()
    try:
        try:
            from config import MONITORED_STOCKS
            monitored = MONITORED_STOCKS[:3]
        except Exception:
            monitored = ['RELIANCE', 'TCS', 'INFY']
        f = NSEFilingFetcher(db, monitored)
        r = f.refresh_all() if hasattr(f, 'refresh_all') else None
        print(f"  result: {r}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()


SCRAPERS = {
    "bulk_deals": probe_bulk_deals,
    "fo_unusual": probe_fo_unusual,
    "promoter_events": probe_promoter_events,
    "sebi_disclosures": probe_sebi_disclosures,
    "nse_filings": probe_nse_filings,
}


if __name__ == "__main__":
    targets = sys.argv[1:] or list(SCRAPERS.keys())
    for name in targets:
        fn = SCRAPERS.get(name)
        if not fn:
            print(f"unknown scraper: {name} (have: {list(SCRAPERS)})")
            continue
        try:
            fn()
        except Exception as e:
            print(f"{name} crashed at top level: {e}")
            traceback.print_exc()
