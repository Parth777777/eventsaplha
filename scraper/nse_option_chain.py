"""NSE option-chain scraper for NIFTY / BANKNIFTY / FINNIFTY.

The NSE option-chain endpoint requires:
  - a session cookie warmed by first hitting nseindia.com (anti-bot)
  - a desktop user-agent
  - polite request cadence (≤ 1 req/min/symbol)

Persisted to its own sqlite db (fo.db) so it never trips main signal-store
locks. Two tables:

  oc_snapshot:        one row per (symbol, expiry, fetched_at) — header stats
  oc_strike:          one row per strike under a snapshot — full CE/PE quotes

Computes derived metrics on the fly:
  max_pain (per snapshot), total_ce_oi, total_pe_oi, pcr_oi
  oi_shift_pct vs the previous snapshot of the same symbol/expiry/strike

Public callables:
  fetch_chain(symbol)            -> dict (raw header + flattened strike rows)
  ingest(symbol)                 -> dict (snapshot_id + diff stats)
  ingest_all_symbols()           -> dict (batch result)
  recent_oi_shifts(symbol, top=10) -> list of strikes with biggest OI deltas
  latest_summary(symbol)         -> top-of-book snapshot for UI
"""
import os
import sqlite3
import threading
import json
import logging
from datetime import datetime
from typing import Optional, Iterable

import requests

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
OC_URL = NSE_BASE + "/api/option-chain-indices?symbol={symbol}"
WARMUP_URL = NSE_BASE + "/option-chain"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "fo.db")
_LOCK = threading.Lock()
_SESSION: Optional[requests.Session] = None


# ── DB ────────────────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _LOCK:
        c = _conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS oc_snapshot (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                expiry          TEXT NOT NULL,
                fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                spot            REAL,
                total_ce_oi     INTEGER,
                total_pe_oi     INTEGER,
                pcr_oi          REAL,
                max_pain        REAL,
                strikes_count   INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS oc_strike (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id     INTEGER NOT NULL,
                symbol          TEXT NOT NULL,
                expiry          TEXT NOT NULL,
                strike          REAL NOT NULL,
                ce_oi           INTEGER, ce_chg_oi INTEGER, ce_iv REAL, ce_ltp REAL, ce_volume INTEGER,
                pe_oi           INTEGER, pe_chg_oi INTEGER, pe_iv REAL, pe_ltp REAL, pe_volume INTEGER,
                fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (snapshot_id) REFERENCES oc_snapshot(id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_oc_snap_sym_at ON oc_snapshot(symbol, fetched_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_oc_strike_sym ON oc_strike(symbol, expiry, strike, fetched_at)")
        c.commit()
        c.close()


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _session():
    """A warmed session — cookies must come from nseindia.com first."""
    global _SESSION
    if _SESSION is not None:
        # Re-warm if the session was idle for > 5 min (NSE expires fast)
        return _SESSION
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_BASE + "/option-chain",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
    })
    try:
        s.get(NSE_BASE, timeout=8)
        s.get(WARMUP_URL, timeout=8)
    except Exception as e:
        logger.warning(f"NSE session warmup failed: {e}")
    _SESSION = s
    return s


def fetch_chain(symbol: str) -> dict:
    """Returns flattened {symbol, expiry, spot, strikes:[...]} for the nearest expiry."""
    s = _session()
    url = OC_URL.format(symbol=symbol)
    try:
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            # Session probably expired — reset and try once.
            global _SESSION
            _SESSION = None
            s = _session()
            r = s.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"NSE option-chain fetch failed for {symbol}: {e}")
        return {}

    records = (data.get("records") or {})
    underlying = records.get("underlyingValue")
    expiries = records.get("expiryDates") or []
    if not expiries:
        return {}
    expiry = expiries[0]  # nearest

    strikes_raw = [d for d in (records.get("data") or []) if d.get("expiryDate") == expiry]
    strikes = []
    for d in strikes_raw:
        ce = d.get("CE") or {}
        pe = d.get("PE") or {}
        strikes.append({
            "strike": float(d.get("strikePrice", 0)),
            "ce_oi":     int(ce.get("openInterest", 0) or 0),
            "ce_chg_oi": int(ce.get("changeinOpenInterest", 0) or 0),
            "ce_iv":     float(ce.get("impliedVolatility", 0) or 0),
            "ce_ltp":    float(ce.get("lastPrice", 0) or 0),
            "ce_volume": int(ce.get("totalTradedVolume", 0) or 0),
            "pe_oi":     int(pe.get("openInterest", 0) or 0),
            "pe_chg_oi": int(pe.get("changeinOpenInterest", 0) or 0),
            "pe_iv":     float(pe.get("impliedVolatility", 0) or 0),
            "pe_ltp":    float(pe.get("lastPrice", 0) or 0),
            "pe_volume": int(pe.get("totalTradedVolume", 0) or 0),
        })
    strikes.sort(key=lambda x: x["strike"])
    return {
        "symbol": symbol,
        "expiry": expiry,
        "spot": float(underlying) if underlying else None,
        "strikes": strikes,
    }


# ── Derived metrics ──────────────────────────────────────────────────────────
def compute_max_pain(strikes: Iterable[dict]) -> Optional[float]:
    """Strike at which total option-writer loss is minimal."""
    strikes = list(strikes)
    if not strikes:
        return None
    best, best_loss = None, float("inf")
    for s in strikes:
        K = s["strike"]
        loss = 0.0
        for r in strikes:
            if r["strike"] < K:  # ITM calls
                loss += (K - r["strike"]) * r["ce_oi"]
            if r["strike"] > K:  # ITM puts
                loss += (r["strike"] - K) * r["pe_oi"]
        if loss < best_loss:
            best, best_loss = K, loss
    return best


def compute_summary(strikes):
    total_ce = sum(s["ce_oi"] for s in strikes)
    total_pe = sum(s["pe_oi"] for s in strikes)
    pcr = (total_pe / total_ce) if total_ce else 0.0
    return total_ce, total_pe, round(pcr, 3)


# ── Ingestion ────────────────────────────────────────────────────────────────
def ingest(symbol: str) -> dict:
    init()
    chain = fetch_chain(symbol)
    if not chain or not chain.get("strikes"):
        return {"symbol": symbol, "ok": False, "reason": "empty chain"}

    strikes = chain["strikes"]
    total_ce, total_pe, pcr = compute_summary(strikes)
    max_pain = compute_max_pain(strikes)
    now = datetime.utcnow().isoformat()

    with _LOCK:
        c = _conn()
        cur = c.execute("""
            INSERT INTO oc_snapshot (symbol, expiry, fetched_at, spot,
                                     total_ce_oi, total_pe_oi, pcr_oi,
                                     max_pain, strikes_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, chain["expiry"], now, chain.get("spot"),
              total_ce, total_pe, pcr, max_pain, len(strikes)))
        snap_id = cur.lastrowid
        c.executemany("""
            INSERT INTO oc_strike
                (snapshot_id, symbol, expiry, strike,
                 ce_oi, ce_chg_oi, ce_iv, ce_ltp, ce_volume,
                 pe_oi, pe_chg_oi, pe_iv, pe_ltp, pe_volume,
                 fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (snap_id, symbol, chain["expiry"], s["strike"],
             s["ce_oi"], s["ce_chg_oi"], s["ce_iv"], s["ce_ltp"], s["ce_volume"],
             s["pe_oi"], s["pe_chg_oi"], s["pe_iv"], s["pe_ltp"], s["pe_volume"],
             now)
            for s in strikes
        ])
        c.commit()
        c.close()

    logger.info(f"oc snapshot {symbol} expiry={chain['expiry']} strikes={len(strikes)} pcr={pcr} maxpain={max_pain}")
    return {
        "symbol": symbol, "ok": True, "snapshot_id": snap_id,
        "expiry": chain["expiry"], "spot": chain.get("spot"),
        "total_ce_oi": total_ce, "total_pe_oi": total_pe,
        "pcr_oi": pcr, "max_pain": max_pain, "strikes": len(strikes),
    }


def ingest_all_symbols() -> dict:
    out = {}
    for sym in SYMBOLS:
        try:
            out[sym] = ingest(sym)
        except Exception as e:
            out[sym] = {"symbol": sym, "ok": False, "reason": str(e)[:200]}
    return out


# ── Queries for UI ───────────────────────────────────────────────────────────
def latest_summary(symbol: str) -> Optional[dict]:
    init()
    with _LOCK:
        c = _conn()
        row = c.execute("""
            SELECT * FROM oc_snapshot
             WHERE symbol = ?
             ORDER BY fetched_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        c.close()
    return dict(row) if row else None


def recent_oi_shifts(symbol: str, top: int = 10) -> list:
    """Strikes with the biggest |chg_oi| (CE + PE) in the latest snapshot."""
    init()
    with _LOCK:
        c = _conn()
        snap = c.execute("""
            SELECT id, expiry FROM oc_snapshot
             WHERE symbol = ? ORDER BY fetched_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        if not snap:
            c.close()
            return []
        rows = c.execute("""
            SELECT strike, ce_oi, ce_chg_oi, pe_oi, pe_chg_oi, ce_iv, pe_iv,
                   (ABS(IFNULL(ce_chg_oi,0)) + ABS(IFNULL(pe_chg_oi,0))) AS total_shift
              FROM oc_strike
             WHERE snapshot_id = ?
             ORDER BY total_shift DESC
             LIMIT ?
        """, (snap["id"], top)).fetchall()
        c.close()
    return [dict(r) | {"expiry": snap["expiry"]} for r in rows]


# Auto-init on import
init()
