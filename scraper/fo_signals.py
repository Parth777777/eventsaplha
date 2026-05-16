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
                max_pain REAL,
                iv_rank REAL,
                fo_alpha_score REAL,
                payload_json TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fos_tkr ON fo_snapshot(ticker, fetched_at)")
        # Backfill columns on pre-existing tables (SQLite tolerates duplicate ADD via try/except)
        for col, decl in (("max_pain", "REAL"), ("iv_rank", "REAL"), ("fo_alpha_score", "REAL")):
            try:
                cur.execute(f"ALTER TABLE fo_snapshot ADD COLUMN {col} {decl}")
            except Exception:
                pass
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fo_iv_history (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                atm_iv REAL,
                PRIMARY KEY (ticker, date)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fo_fii_dii_derivatives (
                date TEXT PRIMARY KEY,
                fii_index_fut_net INTEGER,
                fii_index_opt_net INTEGER,
                fii_stock_fut_net INTEGER,
                fii_stock_opt_net INTEGER,
                dii_index_fut_net INTEGER,
                dii_index_opt_net INTEGER,
                dii_stock_fut_net INTEGER,
                dii_stock_opt_net INTEGER,
                client_index_fut_net INTEGER,
                pro_index_fut_net INTEGER,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
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

            # Enrich: max pain (cheap), IV rank (needs history), alpha score (optional)
            mp = compute_max_pain(snap)
            if mp is not None:
                snap["max_pain"] = mp
            record_iv_history(db, tk, snap.get("atm_iv") or 0)
            iv_rank = compute_iv_rank(db, tk, snap.get("atm_iv") or 0)
            if iv_rank is not None:
                snap["iv_rank"] = iv_rank
            alpha = None
            try:
                from fo_alpha_scorer import score_snapshot  # local import: optional dep
                alpha = score_snapshot(db, tk, snap)
            except Exception:
                pass
            if alpha is not None:
                snap["fo_alpha_score"] = alpha

            # Persist snapshot (without the heavy oi_by_strike map)
            slim = {k: v for k, v in snap.items() if k != "oi_by_strike"}
            cur.execute(
                """
                INSERT INTO fo_snapshot
                (ticker, expiry, spot, atm_iv, pcr, implied_move_pct,
                 max_pain, iv_rank, fo_alpha_score, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tk, snap["expiry"], snap["spot"], snap["atm_iv"], snap["pcr"],
                 snap["implied_move_pct"], snap.get("max_pain"), snap.get("iv_rank"),
                 snap.get("fo_alpha_score"), json.dumps(slim, default=str)),
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
            # Bridge into the global signals table for any watchlisted ticker.
            # This lets the existing alerts UI + notification pipeline pick it up
            # without F&O needing its own delivery channel.
            try:
                emitted = _emit_watchlist_signals(db, tk, unusual, snap.get("spot") or 0)
                out["watchlist_alerts"] = out.get("watchlist_alerts", 0) + emitted
            except Exception as _e:
                logger.debug(f"emit_watchlist_signals {tk}: {_e}")
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
        """SELECT ticker, expiry, spot, atm_iv, pcr, implied_move_pct,
                  max_pain, iv_rank, fo_alpha_score, payload_json, fetched_at
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


def compute_max_pain(snapshot: Dict) -> Optional[float]:
    """Strike where total option-writer payout is minimized.

    For each candidate strike S, total pain =
        Σ_K call_oi[K] * max(S - K, 0)  +  Σ_K put_oi[K] * max(K - S, 0)
    Pick S that minimizes the sum. That's where most writers want price to settle.
    """
    by_strike = snapshot.get("oi_by_strike") or {}
    if not by_strike:
        return None
    strikes = sorted(by_strike.keys())
    if len(strikes) < 3:
        return None
    best_strike = strikes[0]
    best_pain = float("inf")
    for s in strikes:
        pain = 0.0
        for k, info in by_strike.items():
            if s > k:
                pain += info["call_oi"] * (s - k)
            if k > s:
                pain += info["put_oi"] * (k - s)
        if pain < best_pain:
            best_pain = pain
            best_strike = s
    return float(best_strike)


def record_iv_history(db, ticker: str, atm_iv: float, when: Optional[datetime] = None) -> None:
    """Append today's ATM IV to fo_iv_history (one row per ticker per date)."""
    if not atm_iv:
        return
    when = when or datetime.now()
    date_s = when.strftime("%Y-%m-%d")
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO fo_iv_history (ticker, date, atm_iv) VALUES (?, ?, ?)",
            (ticker.upper(), date_s, float(atm_iv)),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug(f"record_iv_history {ticker}: {e}")


def compute_iv_rank(db, ticker: str, current_iv: float, lookback_days: int = 252) -> Optional[float]:
    """IV percentile: where current_iv sits in trailing IV range. 0–100.

    Returns None if fewer than 20 history points (not enough signal).
    """
    if not current_iv:
        return None
    try:
        cur = db.conn.cursor()
        cur.execute(
            "SELECT atm_iv FROM fo_iv_history WHERE ticker = ? AND date >= date('now', ?) ",
            (ticker.upper(), f"-{lookback_days} days"),
        )
        ivs = [r[0] for r in cur.fetchall() if r[0] is not None]
        if len(ivs) < 20:
            return None
        below = sum(1 for v in ivs if v < current_iv)
        return round(100.0 * below / len(ivs), 1)
    except Exception as e:
        logger.debug(f"compute_iv_rank {ticker}: {e}")
        return None


# NSE FAO (F&O Participant Open Interest) daily report. Public CSV.
NSE_FAO_PARTICIPANT_URL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date}.csv"


def fetch_fii_dii_derivatives(when: Optional[datetime] = None) -> Optional[Dict]:
    """Fetch participant-wise OI snapshot from NSE FAO daily report.

    Aggregates Long-Short for FII / DII / Client / Pro across index futs+opts and
    stock futs+opts. Returns net positions as ints. Best-effort — NSE often
    publishes T-1; we walk back up to 5 days until we find the report.
    """
    target = when or datetime.now()
    s = _get_session()
    for back in range(0, 6):
        d = target - timedelta(days=back)
        url = NSE_FAO_PARTICIPANT_URL.format(date=d.strftime("%d%m%Y"))
        try:
            r = s.get(url, timeout=10)
            if r.status_code != 200 or not r.text or "Client Type" not in r.text:
                continue
        except Exception:
            continue
        try:
            return _parse_fao_csv(r.text, d)
        except Exception as e:
            logger.debug(f"parse FAO {d}: {e}")
            continue
    return None


def _parse_fao_csv(text: str, when: datetime) -> Dict:
    """Parse the NSE FAO participant CSV. Format (header on row containing 'Client Type'):

    Client Type, Future Index Long, Future Index Short, Future Stock Long, Future Stock Short,
    Option Index Call Long, Option Index Put Long, Option Index Call Short, Option Index Put Short,
    Option Stock Call Long, Option Stock Put Long, Option Stock Call Short, Option Stock Put Short,
    Total Long Contracts, Total Short Contracts
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    header_idx = next(i for i, ln in enumerate(lines) if "Client Type" in ln)
    rows = {}
    for ln in lines[header_idx + 1:]:
        cells = [c.strip() for c in ln.split(",")]
        if len(cells) < 13:
            continue
        ctype = cells[0].upper()
        try:
            cells_i = [int((cells[j] or "0").replace('"', '').replace(',', '') or 0) for j in range(1, 13)]
        except Exception:
            continue
        rows[ctype] = cells_i  # 12 ints starting at index 0

    def _net(ctype, long_idx, short_idx):
        r = rows.get(ctype)
        if not r:
            return 0
        return r[long_idx] - r[short_idx]

    # Indices (after stripping the Client Type column):
    # 0 fut_idx_long, 1 fut_idx_short, 2 fut_stk_long, 3 fut_stk_short,
    # 4 opt_idx_call_long, 5 opt_idx_put_long, 6 opt_idx_call_short, 7 opt_idx_put_short,
    # 8 opt_stk_call_long, 9 opt_stk_put_long, 10 opt_stk_call_short, 11 opt_stk_put_short
    # Net "option index" position = (call_long + put_short) - (call_short + put_long) — i.e., delta-direction
    def _opt_idx_net(c):
        r = rows.get(c) or [0] * 12
        return (r[4] + r[7]) - (r[6] + r[5])

    def _opt_stk_net(c):
        r = rows.get(c) or [0] * 12
        return (r[8] + r[11]) - (r[10] + r[9])

    return {
        "date": when.strftime("%Y-%m-%d"),
        "fii_index_fut_net": _net("FII", 0, 1),
        "fii_index_opt_net": _opt_idx_net("FII"),
        "fii_stock_fut_net": _net("FII", 2, 3),
        "fii_stock_opt_net": _opt_stk_net("FII"),
        "dii_index_fut_net": _net("DII", 0, 1),
        "dii_index_opt_net": _opt_idx_net("DII"),
        "dii_stock_fut_net": _net("DII", 2, 3),
        "dii_stock_opt_net": _opt_stk_net("DII"),
        "client_index_fut_net": _net("CLIENTS", 0, 1) or _net("CLIENT", 0, 1),
        "pro_index_fut_net": _net("PRO", 0, 1),
    }


def persist_fii_dii_derivatives(db, row: Dict) -> bool:
    if not row or not row.get("date"):
        return False
    try:
        cur = db.conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO fo_fii_dii_derivatives
            (date, fii_index_fut_net, fii_index_opt_net, fii_stock_fut_net, fii_stock_opt_net,
             dii_index_fut_net, dii_index_opt_net, dii_stock_fut_net, dii_stock_opt_net,
             client_index_fut_net, pro_index_fut_net)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["date"], row["fii_index_fut_net"], row["fii_index_opt_net"],
             row["fii_stock_fut_net"], row["fii_stock_opt_net"],
             row["dii_index_fut_net"], row["dii_index_opt_net"],
             row["dii_stock_fut_net"], row["dii_stock_opt_net"],
             row.get("client_index_fut_net"), row.get("pro_index_fut_net")),
        )
        db.conn.commit()
        return True
    except Exception as e:
        logger.warning(f"persist_fii_dii_derivatives: {e}")
        return False


def recent_fii_dii_derivatives(db, days: int = 30) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """SELECT * FROM fo_fii_dii_derivatives
           WHERE date >= date('now', ?) ORDER BY date DESC""",
        (f"-{days} days",),
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_rollover_data(symbol: str, is_index: bool = False) -> Optional[Dict]:
    """Compute % of OI rolled to next expiry vs current expiry. Best-effort.

    Returns {current_expiry, next_expiry, current_oi, next_oi, rollover_pct}.
    Note: NSE option-chain endpoint returns *all* expiries in one payload, so we
    can compute this from a single fetch.
    """
    payload = fetch_option_chain(symbol, is_index=is_index)
    if not payload:
        return None
    try:
        records = payload.get("records") or {}
        data = records.get("data") or []
        spot = float(records.get("underlyingValue") or 0)
        if not data or not spot:
            return None
        expiries = list({d.get("expiryDate") for d in data if d.get("expiryDate")})

        def _parse_dt(s):
            for fmt in ("%d-%b-%Y", "%d %b %Y"):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
            return datetime.max

        expiries.sort(key=_parse_dt)
        if len(expiries) < 2:
            return None
        cur_e, next_e = expiries[0], expiries[1]

        def _total_oi(exp):
            total = 0
            for d in data:
                if d.get("expiryDate") != exp:
                    continue
                ce = d.get("CE") or {}
                pe = d.get("PE") or {}
                total += int(ce.get("openInterest") or 0) + int(pe.get("openInterest") or 0)
            return total

        cur_oi = _total_oi(cur_e)
        next_oi = _total_oi(next_e)
        denom = cur_oi + next_oi
        rollover_pct = round(100.0 * next_oi / denom, 2) if denom else 0
        return {
            "ticker": symbol.upper(),
            "current_expiry": cur_e,
            "next_expiry": next_e,
            "current_oi": cur_oi,
            "next_oi": next_oi,
            "rollover_pct": rollover_pct,
        }
    except Exception as e:
        logger.debug(f"fetch_rollover_data {symbol}: {e}")
        return None


def _emit_watchlist_signals(db, ticker: str, flags: List[Dict], spot: float,
                            score_threshold: float = 60.0) -> int:
    """For each high-score unusual flag on a watchlisted ticker, upsert into the
    `signals` table (event_type='fo_unusual') so the global alerts UI picks it up.
    Returns count of signals emitted.
    """
    if not flags:
        return 0
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT 1 FROM watchlist WHERE ticker = ? LIMIT 1", (ticker.upper(),))
        if not cur.fetchone():
            return 0
    except Exception:
        return 0
    emitted = 0
    for f in flags:
        score = float(f.get("signal_score") or 0)
        if score < score_threshold:
            continue
        try:
            event_id = (
                f"fo_unusual:{ticker.upper()}:{f.get('expiry','')}:{f.get('kind','')}"
                f":{int(f.get('strike') or 0)}:{int(f.get('oi_change') or 0)}"
            )
            sentiment = "bullish" if f.get("kind") == "call" else (
                        "bearish" if f.get("kind") == "put" else "neutral")
            ok = db.upsert_signal(
                event_id=event_id,
                event_type="fo_unusual",
                ticker=ticker.upper(),
                alpha_score=score,
                confidence=0.6,
                regime="derivatives",
                entry_price=float(spot or 0),
                sentiment=sentiment,
                magnitude=int(f.get("oi_change") or 0),
                impact_score=int(score),
                source="fo_signals",
                headline=(f"Unusual {f.get('kind','')} OI at {int(f.get('strike') or 0)} "
                          f"({int(f.get('oi_change') or 0):+,})"),
            )
            if ok:
                emitted += 1
        except Exception as e:
            logger.debug(f"emit signal {ticker}: {e}")
            continue
    return emitted


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
