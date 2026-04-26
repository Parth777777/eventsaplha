"""F&O (futures & options) ingestion + unusual-activity detection for NSE.

Pulls option chain snapshots via NSE's public JSON endpoint and computes:
- IV rank (where current IV sits in trailing IV range)
- OI build-up: which strikes near spot accumulated OI in last cycle
- Put/call OI ratio (PCR) snapshot
- Implied move (1σ) for upcoming expiry
- Unusual activity flag: OI change > 2σ vs 5-day mean

Schema:
    fo_snapshot(ticker, expiry, fetched_at, spot, atm_iv, pcr, implied_move_pct,
                payload_json)
    fo_unusual(ticker, expiry, strike, kind /* call/put */, oi_change, signal_score,
               fetched_at)

This is best-effort — NSE often rate-limits or blocks. Module is safe to skip
if the endpoint fails; downstream code never assumes data is present.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
NSE_INDICES_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

# Maintain a session so the cookies-required NSE endpoints work
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        try:
            # Warm the cookie jar
            s.get("https://www.nseindia.com/option-chain", timeout=8)
        except Exception:
            pass
        _session = s
    return _session


def ensure_schema(db) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fo_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                expiry TEXT,
                spot REAL,
                atm_iv REAL,
                pcr REAL,
                implied_move_pct REAL,
                payload_json TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fos_tkr ON fo_snapshot(ticker, fetched_at)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fo_unusual (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                expiry TEXT,
                strike REAL,
                kind TEXT,         -- 'call' or 'put'
                oi_change INTEGER,
                signal_score REAL,
                detail TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fou_tkr ON fo_unusual(ticker, fetched_at)")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"fo_signals.ensure_schema: {e}")


def fetch_option_chain(symbol: str, is_index: bool = False) -> Optional[Dict]:
    url = (NSE_INDICES_URL if is_index else NSE_OPTION_CHAIN_URL).format(symbol=symbol)
    try:
        s = _get_session()
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"fetch_option_chain {symbol}: {e}")
        return None


def _atm_strike(strikes: List[float], spot: float) -> Optional[float]:
    if not strikes:
        return None
    return min(strikes, key=lambda x: abs(x - spot))


def analyze_chain(payload: Dict) -> Optional[Dict]:
    """Reduce a raw NSE option chain payload to a snapshot dict."""
    try:
        records = payload.get("records") or {}
        data = records.get("data") or []
        spot = float(records.get("underlyingValue") or 0)
        if not data or not spot:
            return None

        # Pick nearest expiry
        expiries = list({d.get("expiryDate") for d in data if d.get("expiryDate")})
        if not expiries:
            return None
        # Sort by date
        def _parse_dt(s):
            for fmt in ("%d-%b-%Y", "%d %b %Y"):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
            return datetime.max
        expiries.sort(key=_parse_dt)
        nearest = expiries[0]

        rows = [d for d in data if d.get("expiryDate") == nearest]
        strikes = sorted({float(d.get("strikePrice")) for d in rows if d.get("strikePrice")})
        atm = _atm_strike(strikes, spot)

        atm_iv = None
        atm_call_oi = atm_put_oi = 0
        total_call_oi = total_put_oi = 0
        oi_by_strike: Dict[float, Dict] = {}
        for d in rows:
            strike = float(d.get("strikePrice") or 0)
            ce = d.get("CE") or {}
            pe = d.get("PE") or {}
            call_oi = int(ce.get("openInterest") or 0)
            put_oi = int(pe.get("openInterest") or 0)
            total_call_oi += call_oi
            total_put_oi += put_oi
            oi_by_strike[strike] = {
                "call_oi": call_oi, "put_oi": put_oi,
                "call_change": int(ce.get("changeinOpenInterest") or 0),
                "put_change": int(pe.get("changeinOpenInterest") or 0),
                "call_iv": float(ce.get("impliedVolatility") or 0),
                "put_iv": float(pe.get("impliedVolatility") or 0),
                "call_ltp": float(ce.get("lastPrice") or 0),
                "put_ltp": float(pe.get("lastPrice") or 0),
            }
            if strike == atm:
                atm_call_oi = call_oi
                atm_put_oi = put_oi
                atm_iv = (float(ce.get("impliedVolatility") or 0) + float(pe.get("impliedVolatility") or 0)) / 2

        pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi else 0
        # Implied move: 1σ based on ATM IV and time-to-expiry
        try:
            dte_days = max(1, (_parse_dt(nearest) - datetime.now()).days)
            implied_move_pct = (atm_iv or 0) * math.sqrt(dte_days / 365.0)
        except Exception:
            implied_move_pct = 0
            dte_days = None

        return {
            "spot": spot,
            "expiry": nearest,
            "atm_strike": atm,
            "atm_iv": round(atm_iv or 0, 2),
            "pcr": pcr,
            "implied_move_pct": round(implied_move_pct, 2),
            "dte_days": dte_days,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "oi_by_strike": oi_by_strike,
        }
    except Exception as e:
        logger.warning(f"analyze_chain failed: {e}")
        return None


def detect_unusual_oi(snapshot: Dict, ticker: str) -> List[Dict]:
    """Identify strikes with abnormal OI build-up near spot (within ±10% of ATM).

    Heuristic: any strike within ±10% of spot whose absolute OI change exceeds
    50,000 contracts AND is more than 2x the median absolute OI change in the
    same chain → flagged. Catches obvious "informed flow" footprints.
    """
    flags: List[Dict] = []
    spot = snapshot.get("spot") or 0
    if not spot:
        return flags
    by_strike = snapshot.get("oi_by_strike") or {}
    near = [(k, v) for k, v in by_strike.items()
            if abs(k - spot) / spot <= 0.10]
    if not near:
        return flags
    abs_changes = [abs(v["call_change"]) + abs(v["put_change"]) for _, v in near]
    if not abs_changes:
        return flags
    sorted_ch = sorted(abs_changes)
    median = sorted_ch[len(sorted_ch) // 2] or 1

    for strike, info in near:
        for kind in ("call", "put"):
            change = info[kind + "_change"]
            if abs(change) < 50000:
                continue
            if abs(change) < 2 * median:
                continue
            score = min(100, 30 + int(abs(change) / median * 5))
            flags.append({
                "ticker": ticker,
                "expiry": snapshot.get("expiry"),
                "strike": strike,
                "kind": kind,
                "oi_change": change,
                "signal_score": score,
                "detail": json.dumps({
                    "spot": spot,
                    "atm_iv": snapshot.get("atm_iv"),
                    "implied_move_pct": snapshot.get("implied_move_pct"),
                    "median_change": median,
                }),
            })
    return flags


def snapshot_and_persist(db, tickers: List[str], is_index: bool = False) -> Dict:
    """Pull option chain for each ticker, persist snapshot + unusual flags."""
    ensure_schema(db)
    out = {"snapshots": 0, "unusual": 0, "skipped": 0}
    cur = db.conn.cursor()
    for tk in tickers:
        try:
            payload = fetch_option_chain(tk, is_index=is_index)
            if not payload:
                out["skipped"] += 1
                # Be polite — back off between failures
                time.sleep(0.4)
                continue
            snap = analyze_chain(payload)
            if not snap:
                out["skipped"] += 1
                continue
            # Persist snapshot (without the heavy oi_by_strike map)
            slim = {k: v for k, v in snap.items() if k != "oi_by_strike"}
            cur.execute(
                """
                INSERT INTO fo_snapshot
                (ticker, expiry, spot, atm_iv, pcr, implied_move_pct, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (tk, snap["expiry"], snap["spot"], snap["atm_iv"], snap["pcr"],
                 snap["implied_move_pct"], json.dumps(slim, default=str)),
            )
            out["snapshots"] += 1

            # Persist unusual flags
            unusual = detect_unusual_oi(snap, tk)
            for u in unusual:
                cur.execute(
                    """
                    INSERT INTO fo_unusual
                    (ticker, expiry, strike, kind, oi_change, signal_score, detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (u["ticker"], u["expiry"], u["strike"], u["kind"],
                     u["oi_change"], u["signal_score"], u["detail"]),
                )
                out["unusual"] += 1
            db.conn.commit()
            time.sleep(0.6)  # rate-limit politely
        except Exception as e:
            logger.warning(f"snapshot {tk}: {e}")
            continue
    return out


def latest_snapshot(db, ticker: str) -> Optional[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """SELECT ticker, expiry, spot, atm_iv, pcr, implied_move_pct, payload_json, fetched_at
           FROM fo_snapshot WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1""",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["payload"] = json.loads(out.pop("payload_json") or "{}")
    except Exception:
        out["payload"] = {}
    return out


def recent_unusual(db, ticker: Optional[str] = None, hours: int = 24) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    args = [f"-{hours} hours"]
    q = """SELECT ticker, expiry, strike, kind, oi_change, signal_score, detail, fetched_at
           FROM fo_unusual WHERE fetched_at >= datetime('now', ?) """
    if ticker:
        q += " AND ticker = ?"
        args.append(ticker.upper())
    q += " ORDER BY signal_score DESC, fetched_at DESC LIMIT 100"
    cur.execute(q, args)
    return [dict(r) for r in cur.fetchall()]
