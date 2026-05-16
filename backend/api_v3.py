"""API v3: forensics, F&O, bulk-deals, screeners, paper-trading, telemetry,
audit-trail, onboarding. Mounts onto the same Flask app as api_v2.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from cache_util import ttl_cache  # response caching for hot endpoints

logger = logging.getLogger(__name__)

bp = Blueprint("api_v3", __name__)

_get_db: Optional[Callable] = None


def _user_id() -> str:
    return getattr(g, "user_id", None) or "legacy"


# ============ FORENSICS / MANIPULATION =====================================

@bp.route("/api/forensics/manipulation", methods=["GET"])
def manipulation_flags():
    """All manipulation flags (circular pattern, insider exit, distribution)."""
    db = _get_db()
    try:
        from forensics.manipulation_detectors import ensure_schema as _es
        _es(db)
    except Exception:
        pass
    try:
        cur = db.conn.cursor()
        # Defensive: if the table doesn't exist, return empty rather than 500
        try:
            cur.execute("SELECT 1 FROM manipulation_flags LIMIT 1")
        except Exception:
            return jsonify({"success": True, "data": [], "note": "manipulation_flags not yet populated"})
        ticker = (request.args.get("ticker") or "").upper().strip()
        days = int(request.args.get("days", "30"))
        args = [f"-{days} days"]
        q = "SELECT ticker, detector, score, band, evidence, as_of FROM manipulation_flags WHERE as_of >= datetime('now', ?)"
        if ticker:
            q += " AND ticker = ?"
            args.append(ticker)
        q += " ORDER BY as_of DESC LIMIT 200"
        cur.execute(q, args)
        out = []
        for r in cur.fetchall():
            try:
                ev = json.loads(r["evidence"] or "{}")
            except Exception:
                ev = {}
            out.append({
                "ticker": r["ticker"], "detector": r["detector"], "score": r["score"],
                "band": r["band"], "evidence": ev, "as_of": r["as_of"],
            })
        return jsonify({"success": True, "data": out})
    except Exception as e:
        return jsonify({"success": True, "data": [], "warning": str(e)})


@bp.route("/api/forensics/auditor-changes", methods=["GET"])
def auditor_changes():
    db = _get_db()
    try:
        from forensics.manipulation_detectors import calendar_auditor_changes
        return jsonify({"success": True, "data": calendar_auditor_changes(db)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============ BULK DEALS / SEBI BAN-LIST ====================================

@bp.route("/api/bulk-deals", methods=["GET"])
def bulk_deals_recent():
    db = _get_db()
    try:
        from bulk_deals import recent_bulk_deals
        ticker = (request.args.get("ticker") or "").upper().strip() or None
        days = int(request.args.get("days", "7"))
        min_value = float(request.args.get("min_value_cr", "0"))
        return jsonify({"success": True, "data": recent_bulk_deals(db, ticker, days, min_value)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/sebi/ban-check", methods=["GET"])
def sebi_ban_check():
    db = _get_db()
    try:
        from bulk_deals import is_banned
        q = (request.args.get("q") or request.args.get("ticker") or "").strip()
        if not q:
            return jsonify({"success": False, "error": "missing 'q' param"}), 400
        return jsonify({"success": True, "data": is_banned(db, q)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============ F&O / OPTIONS =================================================

@bp.route("/api/fo/<ticker>", methods=["GET"])
def fo_snapshot(ticker: str):
    db = _get_db()
    try:
        from fo_signals import latest_snapshot
        snap = latest_snapshot(db, ticker)
        if not snap:
            return jsonify({"success": False, "error": "no snapshot yet"}), 404
        return jsonify({"success": True, "data": snap})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/unusual", methods=["GET"])
def fo_unusual_list():
    db = _get_db()
    try:
        from fo_signals import recent_unusual
        ticker = (request.args.get("ticker") or "").upper().strip() or None
        hours = int(request.args.get("hours", "24"))
        return jsonify({"success": True, "data": recent_unusual(db, ticker, hours)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/refresh", methods=["POST"])
def fo_refresh():
    """Manual snapshot trigger (for testing or on-demand from UI)."""
    db = _get_db()
    try:
        from fo_signals import snapshot_and_persist
        data = request.get_json(force=True) or {}
        tickers = data.get("tickers") or []
        if not tickers:
            return jsonify({"success": False, "error": "tickers[] required"}), 400
        return jsonify({"success": True, "data": snapshot_and_persist(db, tickers)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/index/<symbol>", methods=["GET"])
@ttl_cache(60)
def fo_index_kpis(symbol: str):
    """Index-level F&O KPIs for NIFTY/BANKNIFTY: spot, max pain, PCR, IV, implied move,
    rollover, and the latest FII/DII derivatives row.
    """
    db = _get_db()
    sym = (symbol or "").upper().strip()
    if sym not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        return jsonify({"success": False, "error": "unsupported index"}), 400
    try:
        from fo_signals import latest_snapshot, fetch_rollover_data, recent_fii_dii_derivatives
        snap = latest_snapshot(db, sym) or {}
        rollover = fetch_rollover_data(sym, is_index=True) or {}
        fii_dii = recent_fii_dii_derivatives(db, days=1) or []
        return jsonify({
            "success": True,
            "data": {
                "snapshot": snap,
                "rollover": rollover,
                "fii_dii_latest": (fii_dii[0] if fii_dii else None),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/scanner", methods=["GET"])
@ttl_cache(60)
def fo_scanner():
    """Ranked list of tickers by F&O alpha score, joining unusual-OI flag count.
    Filters: min_alpha, min_oi_change, sort, limit.
    """
    db = _get_db()
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
        min_alpha = float(request.args.get("min_alpha", "0"))
        sort = request.args.get("sort", "alpha")  # alpha | iv_rank | pcr
        sort_col = {
            "alpha": "fo_alpha_score",
            "iv_rank": "iv_rank",
            "pcr": "pcr",
        }.get(sort, "fo_alpha_score")
        cur = db.conn.cursor()
        # Latest snapshot per ticker
        cur.execute(
            f"""
            WITH latest AS (
              SELECT ticker, MAX(fetched_at) AS last_at
              FROM fo_snapshot GROUP BY ticker
            )
            SELECT s.ticker, s.expiry, s.spot, s.atm_iv, s.pcr, s.implied_move_pct,
                   s.max_pain, s.iv_rank, s.fo_alpha_score, s.fetched_at,
                   (SELECT COUNT(*) FROM fo_unusual u
                      WHERE u.ticker = s.ticker
                        AND u.fetched_at >= datetime('now', '-24 hours')) AS unusual_24h
            FROM fo_snapshot s
            JOIN latest l ON s.ticker = l.ticker AND s.fetched_at = l.last_at
            WHERE COALESCE(s.fo_alpha_score, 0) >= ?
            ORDER BY COALESCE(s.{sort_col}, -1) DESC
            LIMIT ?
            """,
            (min_alpha, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"success": True, "data": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/ticker/<symbol>", methods=["GET"])
@ttl_cache(120)
def fo_ticker_drill(symbol: str):
    """Per-ticker drill: latest snapshot (with full chain), unusual flags, IV history."""
    db = _get_db()
    sym = (symbol or "").upper().strip()
    if not sym:
        return jsonify({"success": False, "error": "ticker required"}), 400
    try:
        from fo_signals import latest_snapshot, recent_unusual
        snap = latest_snapshot(db, sym)
        if not snap:
            return jsonify({"success": False, "error": "no snapshot for ticker"}), 404
        unusual = recent_unusual(db, sym, hours=72)
        cur = db.conn.cursor()
        cur.execute(
            "SELECT date, atm_iv FROM fo_iv_history WHERE ticker = ? ORDER BY date DESC LIMIT 90",
            (sym,),
        )
        iv_history = [dict(r) for r in cur.fetchall()]
        return jsonify({
            "success": True,
            "data": {
                "snapshot": snap,
                "unusual": unusual,
                "iv_history": list(reversed(iv_history)),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/fii-dii", methods=["GET"])
@ttl_cache(300)
def fo_fii_dii():
    """Recent FII/DII derivative net positions (default 30 days)."""
    db = _get_db()
    try:
        from fo_signals import recent_fii_dii_derivatives
        days = int(request.args.get("days", "30"))
        return jsonify({"success": True, "data": recent_fii_dii_derivatives(db, days=days)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/fo/rollover/<symbol>", methods=["GET"])
@ttl_cache(300)
def fo_rollover(symbol: str):
    """Current vs next-expiry OI rollover %. Useful in expiry week."""
    try:
        from fo_signals import fetch_rollover_data
        is_index = (symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"))
        data = fetch_rollover_data(symbol, is_index=is_index)
        if not data:
            return jsonify({"success": False, "error": "rollover data unavailable"}), 404
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============ SCREENERS =====================================================

_SCREENERS = {
    "high_alpha_clean": {
        "name": "High alpha + clean forensics",
        "description": "Alpha ≥ 65, forensic_band = clean, signal age < 7 days",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment, s.event_type,
                   s.created_at, COALESCE(ms.band, 'clean') AS forensic_band
            FROM signals s
            LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
            WHERE s.alpha_score >= 65
              AND COALESCE(ms.band, 'clean') = 'clean'
              AND s.created_at >= datetime('now', '-7 days')
              AND s.status = 'active'
            ORDER BY s.alpha_score DESC LIMIT 50
        """,
    },
    "smallcap_pledge_up": {
        "name": "Insider / promoter activity",
        "description": "Insider-flavored signals (alpha ≥ 50) over the last 14 days",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment,
                   s.headline, s.created_at
            FROM signals s
            WHERE s.event_type = 'insider'
              AND s.alpha_score >= 50
              AND s.created_at >= datetime('now', '-14 days')
              AND s.status = 'active'
            ORDER BY s.alpha_score DESC LIMIT 50
        """,
    },
    "volume_spike_no_news": {
        "name": "Quiet alpha (under the radar)",
        "description": "High-alpha signals (≥ 55) with light news coverage in the last 7 days",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment, s.event_type,
                   (SELECT COUNT(*) FROM events e
                      WHERE e.created_at >= datetime('now', '-7 days')
                        AND ',' || IFNULL(e.companies, '') || ',' LIKE '%,' || s.ticker || ',%'
                   ) AS news_7d,
                   s.created_at
            FROM signals s
            WHERE s.alpha_score >= 55
              AND s.created_at >= datetime('now', '-7 days')
              AND s.status = 'active'
              AND (SELECT COUNT(*) FROM events e
                     WHERE e.created_at >= datetime('now', '-7 days')
                       AND ',' || IFNULL(e.companies, '') || ',' LIKE '%,' || s.ticker || ',%'
                  ) <= 2
            ORDER BY s.alpha_score DESC LIMIT 50
        """,
    },
    "earnings_within_5d_high_alpha": {
        "name": "Hot earnings flow",
        "description": "Earnings-tagged signals (alpha ≥ 60) in the last 7 days — pre/post print activity",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment, s.headline, s.created_at
            FROM signals s
            WHERE s.event_type = 'earnings'
              AND s.alpha_score >= 60
              AND s.created_at >= datetime('now', '-7 days')
              AND s.status = 'active'
            ORDER BY s.created_at DESC, s.alpha_score DESC LIMIT 50
        """,
    },
    "manipulation_band": {
        "name": "Forensic risk (elevated)",
        "description": "Events with manipulation score ≥ 7 in the last 30 days — audit before trading",
        "sql": """
            SELECT s.ticker, s.company, ms.score, ms.band, ms.reasons,
                   s.headline, ms.computed_at AS as_of
            FROM manipulation_scores ms
            JOIN signals s ON s.event_id = ms.event_id
            WHERE ms.score >= 7
              AND ms.computed_at >= datetime('now', '-30 days')
            GROUP BY s.ticker
            ORDER BY ms.score DESC, ms.computed_at DESC LIMIT 50
        """,
    },
    "bulk_deal_targets": {
        "name": "M&A / corporate activity",
        "description": "Merger / insider / dividend signals (alpha ≥ 40) in the last 14 days",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.event_type, s.sentiment,
                   s.headline, s.created_at
            FROM signals s
            WHERE s.event_type IN ('merger', 'insider', 'dividend')
              AND s.alpha_score >= 40
              AND s.created_at >= datetime('now', '-14 days')
              AND s.status = 'active'
            ORDER BY s.created_at DESC LIMIT 50
        """,
    },
    "fo_unusual_24h": {
        "name": "F&O / derivatives chatter",
        "description": "Events mentioning F&O, options, futures, OI, or derivatives in last 3 days",
        "sql": """
            SELECT companies AS ticker, title AS headline, source, sentiment,
                   impact_score, magnitude, created_at
            FROM events
            WHERE created_at >= datetime('now', '-3 days')
              AND (
                   LOWER(title) LIKE '%f&o%'        OR LOWER(title) LIKE '%options%'
                OR LOWER(title) LIKE '%futures%'    OR LOWER(title) LIKE '%derivatives%'
                OR LOWER(title) LIKE '%open interest%' OR LOWER(title) LIKE '%fno%'
                OR LOWER(title) LIKE '%put writer%' OR LOWER(title) LIKE '%call writer%'
              )
              AND COALESCE(impact_score, 0) >= 30
            ORDER BY impact_score DESC LIMIT 50
        """,
    },
    "filing_grade": {
        "name": "Tier-1 financial press",
        "description": "High-alpha signals (≥ 55) sourced from Mint / ET / Hindu / MoneyControl in last 7 days",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.event_type, s.sentiment,
                   s.source, s.headline, s.created_at
            FROM signals s
            WHERE s.created_at >= datetime('now', '-7 days')
              AND s.alpha_score >= 55
              AND s.status = 'active'
              AND (
                   s.source LIKE 'mint_%'      OR s.source LIKE 'et_%'
                OR s.source LIKE 'thehindu%'   OR s.source LIKE 'mc_%'
                OR s.source LIKE 'business_standard%' OR s.source LIKE 'cnbctv18%'
                OR s.source LIKE 'BSE%' OR s.source LIKE 'NSE%'
                OR s.source LIKE 'SEBI%' OR s.source LIKE 'RBI%'
              )
            ORDER BY s.alpha_score DESC LIMIT 50
        """,
    },
}


@bp.route("/api/screeners", methods=["GET"])
def screeners_list():
    return jsonify({
        "success": True,
        "data": [
            {"id": k, "name": v["name"], "description": v["description"]}
            for k, v in _SCREENERS.items()
        ],
    })


@bp.route("/api/screeners/<screener_id>", methods=["GET"])
def screener_run(screener_id: str):
    db = _get_db()
    spec = _SCREENERS.get(screener_id)
    if not spec:
        return jsonify({"success": False, "error": f"unknown screener: {screener_id}"}), 404
    try:
        cur = db.conn.cursor()
        cur.execute(spec["sql"])
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"success": True, "name": spec["name"], "count": len(rows), "data": rows})
    except Exception as e:
        # Tables may not exist yet (e.g. earnings_calendar, volume_anomalies, fo_unusual)
        return jsonify({"success": True, "name": spec["name"], "count": 0,
                        "data": [], "warning": f"data not yet available: {e}"})


# ============ PAPER TRADING =================================================

def _ensure_paper_schema(db):
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,           -- LONG / SHORT
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_at TEXT DEFAULT CURRENT_TIMESTAMP,
            exit_price REAL,
            exit_at TEXT,
            pnl_pct REAL,
            pnl_inr REAL,
            tag TEXT,
            event_id TEXT,                -- optional link to triggering signal
            status TEXT DEFAULT 'OPEN'    -- OPEN / CLOSED
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_user ON paper_positions(user_id, status)")
    db.conn.commit()


@bp.route("/api/paper/positions", methods=["GET"])
def paper_positions_list():
    db = _get_db()
    _ensure_paper_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        "SELECT * FROM paper_positions WHERE user_id = ? ORDER BY entry_at DESC LIMIT 200",
        (_user_id(),),
    )
    rows = [dict(r) for r in cur.fetchall()]
    # Mark-to-market open positions using latest stock price
    try:
        for r in rows:
            if r["status"] != "OPEN":
                continue
            cur.execute("SELECT current_price FROM stocks WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
                        (r["ticker"],))
            sp = cur.fetchone()
            if not sp:
                continue
            cur_px = float(sp["current_price"] or 0)
            qty = int(r["quantity"] or 0)
            entry = float(r["entry_price"] or 0)
            if cur_px and entry:
                if r["side"] == "LONG":
                    r["unrealized_pct"] = round((cur_px - entry) / entry * 100, 2)
                else:
                    r["unrealized_pct"] = round((entry - cur_px) / entry * 100, 2)
                r["current_price"] = cur_px
                r["unrealized_inr"] = round(r["unrealized_pct"] / 100 * entry * qty, 2)
    except Exception:
        pass
    return jsonify({"success": True, "data": rows})


@bp.route("/api/paper/positions", methods=["POST"])
def paper_positions_open():
    db = _get_db()
    _ensure_paper_schema(db)
    data = request.get_json(force=True) or {}
    ticker = (data.get("ticker") or "").upper().strip()
    side = (data.get("side") or "LONG").upper()
    qty = int(data.get("quantity") or 0)
    entry = float(data.get("entry_price") or 0)
    if not (ticker and qty > 0 and entry > 0 and side in ("LONG", "SHORT")):
        return jsonify({"success": False, "error": "ticker, side, quantity > 0, entry_price > 0 required"}), 400
    cur = db.conn.cursor()
    cur.execute(
        """
        INSERT INTO paper_positions (user_id, ticker, side, quantity, entry_price, tag, event_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (_user_id(), ticker, side, qty, entry, data.get("tag"), data.get("event_id")),
    )
    db.conn.commit()
    pid = cur.lastrowid
    try:
        from api import audit_log as _audit
        _audit('paper_open', f'paper:{pid}',
               {'ticker': ticker, 'side': side, 'qty': qty, 'entry': entry,
                'tag': data.get('tag'), 'event_id': data.get('event_id')})
    except Exception:
        pass
    return jsonify({"success": True, "id": pid})


@bp.route("/api/paper/positions/<int:pid>/close", methods=["POST"])
def paper_positions_close(pid: int):
    db = _get_db()
    _ensure_paper_schema(db)
    data = request.get_json(silent=True) or {}
    exit_price = float(data.get("exit_price") or 0)
    cur = db.conn.cursor()
    cur.execute("SELECT * FROM paper_positions WHERE id = ? AND user_id = ?", (pid, _user_id()))
    pos = cur.fetchone()
    if not pos:
        return jsonify({"success": False, "error": "not found"}), 404
    if not exit_price:
        cur.execute("SELECT current_price FROM stocks WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
                    (pos["ticker"],))
        sp = cur.fetchone()
        if sp:
            exit_price = float(sp["current_price"] or 0)
    if not exit_price:
        return jsonify({"success": False, "error": "exit_price not provided and no live price found"}), 400
    entry = float(pos["entry_price"] or 0)
    qty = int(pos["quantity"] or 0)
    if pos["side"] == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
    pnl_inr = round(pnl_pct / 100 * entry * qty, 2)
    cur.execute(
        """UPDATE paper_positions SET status='CLOSED', exit_price=?, exit_at=CURRENT_TIMESTAMP,
           pnl_pct=?, pnl_inr=? WHERE id=?""",
        (exit_price, round(pnl_pct, 2), pnl_inr, pid),
    )
    db.conn.commit()
    try:
        from api import audit_log as _audit
        _audit('paper_close', f'paper:{pid}',
               {'ticker': pos['ticker'], 'side': pos['side'],
                'exit': exit_price, 'pnl_pct': round(pnl_pct, 2), 'pnl_inr': pnl_inr})
    except Exception:
        pass
    return jsonify({"success": True, "pnl_pct": round(pnl_pct, 2), "pnl_inr": pnl_inr})


@bp.route("/api/paper/summary", methods=["GET"])
def paper_summary():
    db = _get_db()
    _ensure_paper_schema(db)
    cur = db.conn.cursor()
    cur.execute("""SELECT
        COUNT(*) FILTER (WHERE status='OPEN') AS open_count,
        COUNT(*) FILTER (WHERE status='CLOSED') AS closed_count,
        COALESCE(SUM(pnl_inr) FILTER (WHERE status='CLOSED'), 0) AS realized_pnl,
        COALESCE(AVG(pnl_pct) FILTER (WHERE status='CLOSED'), 0) AS avg_pnl_pct,
        COUNT(*) FILTER (WHERE status='CLOSED' AND pnl_pct > 0) AS wins,
        COUNT(*) FILTER (WHERE status='CLOSED' AND pnl_pct <= 0) AS losses
        FROM paper_positions WHERE user_id = ?""", (_user_id(),))
    try:
        row = cur.fetchone()
        if row is None:
            # SQLite older versions don't support FILTER → fallback
            raise Exception("retry")
        out = dict(row)
    except Exception:
        cur.execute("SELECT status, pnl_inr, pnl_pct FROM paper_positions WHERE user_id = ?", (_user_id(),))
        rows = cur.fetchall()
        open_c = sum(1 for r in rows if r["status"] == "OPEN")
        closed = [r for r in rows if r["status"] == "CLOSED"]
        wins = sum(1 for r in closed if (r["pnl_pct"] or 0) > 0)
        losses = sum(1 for r in closed if (r["pnl_pct"] or 0) <= 0)
        out = {
            "open_count": open_c,
            "closed_count": len(closed),
            "realized_pnl": round(sum(float(r["pnl_inr"] or 0) for r in closed), 2),
            "avg_pnl_pct": round(sum(float(r["pnl_pct"] or 0) for r in closed) / max(1, len(closed)), 2),
            "wins": wins, "losses": losses,
        }
    out["win_rate_pct"] = round(out["wins"] / max(1, out["wins"] + out["losses"]) * 100, 1) if (out["wins"] + out["losses"]) else None
    return jsonify({"success": True, "data": out})


# ============ SIMULATOR REPLAY ==============================================

@bp.route("/api/simulator/replay", methods=["POST"])
def simulator_replay():
    """Replay past signals as if traded systematically.

    Body: {"min_alpha": 65, "tickers": ["RELIANCE",...], "days": 90, "size_inr": 10000}
    Returns: per-trade pnl + summary.
    """
    db = _get_db()
    data = request.get_json(force=True) or {}
    min_alpha = float(data.get("min_alpha", 65))
    tickers = data.get("tickers") or []
    days = int(data.get("days", 90))
    size_inr = float(data.get("size_inr", 10000))

    cur = db.conn.cursor()
    args = [f"-{days} days", min_alpha]
    q = """
        SELECT s.ticker, s.alpha_score, s.sentiment, s.entry_price, s.created_at, s.event_id,
               p.predicted_return_pct, p.actual_return_pct, p.horizon
        FROM signals s
        LEFT JOIN predictions p ON p.event_id = s.event_id AND p.horizon = '3D'
        WHERE s.created_at >= datetime('now', ?) AND s.alpha_score >= ?
    """
    if tickers:
        placeholders = ",".join(["?"] * len(tickers))
        q += f" AND s.ticker IN ({placeholders})"
        args.extend([t.upper() for t in tickers])
    q += " ORDER BY s.created_at ASC"
    cur.execute(q, args)
    rows = [dict(r) for r in cur.fetchall()]

    trades = []
    realized = 0.0
    wins = losses = pending = 0
    for r in rows:
        actual = r.get("actual_return_pct")
        if actual is None:
            pending += 1
            trades.append({**r, "pnl_inr": None, "status": "PENDING"})
            continue
        pnl_pct = float(actual) * (1 if r.get("sentiment") == "bullish" else -1)
        pnl_inr = round(size_inr * pnl_pct / 100, 2)
        realized += pnl_inr
        if pnl_inr > 0: wins += 1
        else: losses += 1
        trades.append({**r, "pnl_pct": round(pnl_pct, 2), "pnl_inr": pnl_inr, "status": "RESOLVED"})

    return jsonify({
        "success": True,
        "summary": {
            "trades": len(rows),
            "wins": wins, "losses": losses, "pending": pending,
            "win_rate_pct": round(wins / max(1, wins + losses) * 100, 1) if (wins + losses) else None,
            "realized_pnl_inr": round(realized, 2),
            "avg_per_trade_inr": round(realized / max(1, wins + losses), 2),
            "params": {"min_alpha": min_alpha, "days": days, "size_inr": size_inr,
                       "tickers": tickers or "ALL"},
        },
        "trades": trades[-200:],   # cap response size
    })


# ============ TELEMETRY (click / mute / dismiss tracking) ===================

def _ensure_telemetry_schema(db):
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            kind TEXT NOT NULL,         -- click / mute / dismiss / view / convert
            target TEXT,                -- e.g. event_id, ticker, page
            metadata TEXT,              -- JSON
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tlm_kind ON telemetry(kind, created_at)")
    db.conn.commit()


@bp.route("/api/telemetry", methods=["POST"])
def telemetry_record():
    db = _get_db()
    _ensure_telemetry_schema(db)
    data = request.get_json(force=True) or {}
    kind = data.get("kind")
    if not kind:
        return jsonify({"success": False, "error": "kind required"}), 400
    cur = db.conn.cursor()
    cur.execute(
        "INSERT INTO telemetry (user_id, kind, target, metadata) VALUES (?, ?, ?, ?)",
        (_user_id(), kind, data.get("target"), json.dumps(data.get("metadata") or {}, default=str)),
    )
    db.conn.commit()
    return jsonify({"success": True})


@bp.route("/api/telemetry/summary", methods=["GET"])
def telemetry_summary():
    db = _get_db()
    _ensure_telemetry_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """
        SELECT kind, COUNT(*) AS n
        FROM telemetry
        WHERE created_at >= datetime('now', '-7 days')
        GROUP BY kind
        """
    )
    return jsonify({"success": True, "data": [dict(r) for r in cur.fetchall()]})


# ============ AUDIT TRAIL (signal lineage) =================================

@bp.route("/api/audit/<event_id>", methods=["GET"])
def audit_trail(event_id: str):
    """Returns the full lineage of a signal: source articles, forensic flags,
    predictions, and resolution status. The 'show your work' endpoint."""
    db = _get_db()
    cur = db.conn.cursor()
    out: Dict = {"event_id": event_id}
    try:
        cur.execute("SELECT * FROM events WHERE event_id = ? LIMIT 1", (event_id,))
        ev = cur.fetchone()
        out["event"] = dict(ev) if ev else None
    except Exception:
        out["event"] = None
    try:
        cur.execute("SELECT * FROM signals WHERE event_id = ? LIMIT 1", (event_id,))
        sig = cur.fetchone()
        out["signal"] = dict(sig) if sig else None
    except Exception:
        out["signal"] = None
    try:
        cur.execute("SELECT horizon, predicted_return_pct, actual_return_pct, target_price, confidence, created_at FROM predictions WHERE event_id = ?", (event_id,))
        out["predictions"] = [dict(r) for r in cur.fetchall()]
    except Exception:
        out["predictions"] = []
    try:
        cur.execute("SELECT detector, score, band, evidence, as_of FROM manipulation_flags WHERE ticker = ? AND as_of >= datetime('now', '-30 days')",
                    ((sig["ticker"] if sig else ""),))
        out["manipulation_flags"] = [dict(r) for r in cur.fetchall()]
    except Exception:
        out["manipulation_flags"] = []
    return jsonify({"success": True, "data": out})


# ============ ONBOARDING ====================================================

def _ensure_onboarding_schema(db):
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT PRIMARY KEY,
            picked_tickers TEXT,
            picked_sectors TEXT,
            risk_tolerance TEXT,
            alert_channels TEXT,
            onboarded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.conn.commit()


@bp.route("/api/onboarding/state", methods=["GET"])
def onboarding_state():
    db = _get_db()
    _ensure_onboarding_schema(db)
    cur = db.conn.cursor()
    cur.execute("SELECT * FROM user_preferences WHERE user_id = ?", (_user_id(),))
    row = cur.fetchone()
    return jsonify({
        "success": True,
        "data": dict(row) if row else None,
        "onboarded": row is not None,
    })


@bp.route("/api/onboarding/save", methods=["POST"])
def onboarding_save():
    db = _get_db()
    _ensure_onboarding_schema(db)
    data = request.get_json(force=True) or {}
    cur = db.conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO user_preferences
        (user_id, picked_tickers, picked_sectors, risk_tolerance, alert_channels)
        VALUES (?, ?, ?, ?, ?)
        """,
        (_user_id(),
         json.dumps(data.get("tickers") or []),
         json.dumps(data.get("sectors") or []),
         data.get("risk") or "moderate",
         json.dumps(data.get("channels") or [])),
    )
    db.conn.commit()
    # Auto-populate watchlist from picked tickers
    try:
        for tk in (data.get("tickers") or [])[:20]:
            try:
                db.add_to_watchlist(_user_id(), tk.upper())
            except Exception:
                pass
    except Exception:
        pass
    return jsonify({"success": True})


# ============ WATCHLIST TAGS ================================================

def _ensure_watchlist_tags_schema(db):
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            ticker TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, tag, ticker)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wlt_user ON watchlist_tags(user_id, tag)")
    db.conn.commit()


@bp.route("/api/watchlist/tags", methods=["GET"])
def watchlist_tags_list():
    db = _get_db()
    _ensure_watchlist_tags_schema(db)
    cur = db.conn.cursor()
    cur.execute("SELECT tag, ticker, created_at FROM watchlist_tags WHERE user_id = ? ORDER BY tag, ticker",
                (_user_id(),))
    out: Dict[str, List[str]] = {}
    for r in cur.fetchall():
        out.setdefault(r["tag"], []).append(r["ticker"])
    return jsonify({"success": True, "data": [{"tag": k, "tickers": v} for k, v in out.items()]})


@bp.route("/api/watchlist/tags", methods=["POST"])
def watchlist_tag_add():
    db = _get_db()
    _ensure_watchlist_tags_schema(db)
    d = request.get_json(force=True) or {}
    tag = (d.get("tag") or "").strip()
    ticker = (d.get("ticker") or "").upper().strip()
    if not tag or not ticker:
        return jsonify({"success": False, "error": "tag and ticker required"}), 400
    cur = db.conn.cursor()
    try:
        cur.execute("INSERT OR IGNORE INTO watchlist_tags (user_id, tag, ticker) VALUES (?, ?, ?)",
                    (_user_id(), tag, ticker))
        db.conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/watchlist/tags", methods=["DELETE"])
def watchlist_tag_remove():
    db = _get_db()
    _ensure_watchlist_tags_schema(db)
    tag = (request.args.get("tag") or "").strip()
    ticker = (request.args.get("ticker") or "").upper().strip()
    cur = db.conn.cursor()
    if tag and ticker:
        cur.execute("DELETE FROM watchlist_tags WHERE user_id=? AND tag=? AND ticker=?", (_user_id(), tag, ticker))
    elif tag:
        cur.execute("DELETE FROM watchlist_tags WHERE user_id=? AND tag=?", (_user_id(), tag))
    else:
        return jsonify({"success": False, "error": "tag required"}), 400
    db.conn.commit()
    return jsonify({"success": True})


# ============ SECTOR ROTATION ===============================================

@bp.route("/api/sectors/rotation", methods=["GET"])
@ttl_cache(seconds=120)
def sectors_rotation():
    """Returns 1W/1M/3M relative-strength snapshot per sector.

    Strategy: there is no normalized 'stocks' table, so we proxy sector
    strength by computing the average alpha score and bullish-share of
    signals fired in each window. Money-flow is approximated by signal count
    weighted by alpha (high-alpha signals → likely high attention/flow).
    """
    db = _get_db()
    cur = db.conn.cursor()
    out = {"1W": [], "1M": [], "3M": []}
    try:
        from config import STOCK_SECTORS
    except Exception:
        STOCK_SECTORS = {}
    for label, days in (("1W", 7), ("1M", 30), ("3M", 90)):
        try:
            cur.execute(
                """SELECT ticker, alpha_score, sentiment FROM signals
                   WHERE created_at >= datetime('now', ?)""",
                (f"-{days} days",),
            )
            rows = cur.fetchall()
        except Exception:
            rows = []
        agg: Dict[str, Dict] = {}
        for r in rows:
            sec = STOCK_SECTORS.get(r["ticker"], "OTHER")
            d = agg.setdefault(sec, {"sum_alpha": 0.0, "n": 0, "bullish": 0, "bearish": 0})
            d["sum_alpha"] += float(r["alpha_score"] or 0)
            d["n"] += 1
            if r["sentiment"] == "bullish": d["bullish"] += 1
            elif r["sentiment"] == "bearish": d["bearish"] += 1
        items = []
        for sec, d in agg.items():
            if not d["n"]:
                continue
            avg_alpha = d["sum_alpha"] / d["n"]
            net_dir = (d["bullish"] - d["bearish"]) / d["n"]    # -1..+1
            # Composite "rotation strength" — turn into a % move proxy
            avg_change_pct = round(net_dir * (avg_alpha / 5.0), 2)
            items.append({
                "sector": sec,
                "avg_change_pct": avg_change_pct,
                "money_flow_proxy": int(d["sum_alpha"]),
                "stock_count": d["n"],
                "avg_alpha": round(avg_alpha, 1),
                "bullish_share_pct": round(d["bullish"] / d["n"] * 100, 1),
            })
        items.sort(key=lambda x: x["avg_change_pct"], reverse=True)
        out[label] = items[:25]
    return jsonify({"success": True, "data": out})


# ============ MAP STATE-LEVEL EVENTS =======================================

# Coarse India state centroids — used to tag domestic events with a location
INDIA_STATES = {
    "maharashtra": (19.7515, 75.7139), "delhi": (28.7041, 77.1025),
    "karnataka": (15.3173, 75.7139), "tamil nadu": (11.1271, 78.6569),
    "tamilnadu": (11.1271, 78.6569), "gujarat": (22.2587, 71.1924),
    "uttar pradesh": (26.8467, 80.9462), "rajasthan": (27.0238, 74.2179),
    "west bengal": (22.9868, 87.8550), "kerala": (10.8505, 76.2711),
    "telangana": (18.1124, 79.0193), "punjab": (31.1471, 75.3412),
    "haryana": (29.0588, 76.0856), "bihar": (25.0961, 85.3131),
    "odisha": (20.9517, 85.0985), "jharkhand": (23.6102, 85.2799),
    "chhattisgarh": (21.2787, 81.8661), "assam": (26.2006, 92.9376),
    "andhra pradesh": (15.9129, 79.7400), "madhya pradesh": (22.9734, 78.6569),
    "uttarakhand": (30.0668, 79.0193),
    "mumbai": (19.0760, 72.8777), "bengaluru": (12.9716, 77.5946),
    "chennai": (13.0827, 80.2707), "hyderabad": (17.3850, 78.4867),
    "pune": (18.5204, 73.8567), "ahmedabad": (23.0225, 72.5714),
    "kolkata": (22.5726, 88.3639),
}


@bp.route("/api/geo/india", methods=["GET"])
@ttl_cache(seconds=60)
def geo_india_events():
    """India-zoomed map data: events tagged with an Indian state.
    Each event is also classified into a lens (policy / earnings / sector)
    so the frontend can color-code by intent. Returns lens metadata too."""
    # Lazy import — _load_geo_configs is defined later in the file
    try:
        evergreen, rules = _load_geo_configs()
    except Exception:
        evergreen, rules = [], {"lenses": {}}
    db = _get_db()
    cur = db.conn.cursor()
    hours = min(int(request.args.get("hours", 168)), 720)  # default 7 days

    try:
        cur.execute(
            """SELECT event_id, title, summary, source, link, companies, sentiment, magnitude, impact_score, event_type, created_at
               FROM events WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 800""",
            (f"-{hours} hours",),
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        rows = []

    out = []
    for r in rows:
        text = ((r.get("title") or "") + " " + (r.get("summary") or ""))
        loc = _geocode_india_state(text)
        if not loc:
            continue
        state, lat, lng = loc

        # Severity 1-10
        severity = float(r.get("impact_score") or 0)
        if severity == 0 and r.get("magnitude"):
            severity = float(r["magnitude"]) * 10
        severity = max(1, min(10, round(severity / 10)))

        # Lens — prefer event_type if it's policy/earnings, else classify
        ev_type = (r.get("event_type") or "").lower()
        if ev_type == "policy":
            lens = "central_banks" if any(k in text.lower() for k in ["rbi", "sebi", "monetary"]) else "trade"
        elif ev_type == "earnings":
            lens = "central_banks"  # default bucket
        else:
            lens = _classify_lens(text, rules) or "geopolitics"

        out.append({
            "event_id":   r["event_id"],
            "state":      state,
            "lat":        lat,
            "lng":        lng,
            "title":      (r.get("title") or "")[:200],
            "summary":    (r.get("summary") or "")[:300],
            "lens":       lens,
            "event_type": r.get("event_type"),
            "sentiment":  r.get("sentiment"),
            "magnitude":  r.get("magnitude"),
            "severity":   severity,
            "source":     r.get("source"),
            "link":       r.get("link"),
            "companies":  r.get("companies"),
            "as_of":      r.get("created_at"),
        })

    # Lens metadata for the frontend
    lens_meta = {
        lid: {"label": l.get("label"), "color": l.get("color"), "monogram": l.get("monogram")}
        for lid, l in (rules.get("lenses") or {}).items()
    }

    # Aggregate by state for the right-rail "state intelligence" panel
    by_state: Dict[str, Dict] = {}
    for e in out:
        s = e["state"]
        if s not in by_state:
            by_state[s] = {"state": s, "lat": e["lat"], "lng": e["lng"], "count": 0,
                           "policy": 0, "earnings": 0, "other": 0, "max_severity": 0}
        by_state[s]["count"] += 1
        et = (e.get("event_type") or "").lower()
        if et == "policy":
            by_state[s]["policy"] += 1
        elif et == "earnings":
            by_state[s]["earnings"] += 1
        else:
            by_state[s]["other"] += 1
        if e["severity"] > by_state[s]["max_severity"]:
            by_state[s]["max_severity"] = e["severity"]
    state_summary = sorted(by_state.values(), key=lambda x: -x["count"])

    return jsonify({
        "success": True,
        "data":    out,
        "count":   len(out),
        "states":  state_summary,
        "lenses":  lens_meta,
    })


# ============ EARNINGS (BSE/NSE direct calendar via DB cache) ===============

def _ensure_earnings_schema(db):
    cur = db.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            company TEXT,
            sector TEXT,
            earnings_date TEXT NOT NULL,
            beat_miss TEXT,           -- BEAT / MISS / INLINE / null
            actual_eps REAL,
            consensus_eps REAL,
            implied_move_pct REAL,
            source TEXT,              -- bse / nse / yfinance
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, earnings_date)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_earn_date ON earnings_calendar(earnings_date)")
    db.conn.commit()


@bp.route("/api/earnings/upcoming", methods=["GET"])
def earnings_upcoming():
    db = _get_db()
    _ensure_earnings_schema(db)
    cur = db.conn.cursor()
    days = int(request.args.get("days", "14"))
    cur.execute(
        """SELECT * FROM earnings_calendar
           WHERE earnings_date >= date('now') AND earnings_date <= date('now', ?)
           ORDER BY earnings_date ASC LIMIT 200""",
        (f"+{days} days",),
    )
    return jsonify({"success": True, "data": [dict(r) for r in cur.fetchall()]})


@bp.route("/api/earnings/beats-misses", methods=["GET"])
def earnings_beats_misses():
    db = _get_db()
    _ensure_earnings_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """SELECT * FROM earnings_calendar
           WHERE earnings_date >= date('now', '-90 days') AND beat_miss IS NOT NULL
           ORDER BY earnings_date DESC LIMIT 200"""
    )
    return jsonify({"success": True, "data": [dict(r) for r in cur.fetchall()]})


# ============ COMPARE — peer fundamentals + last-5 events ===================

@bp.route("/api/compare/<ticker>/peers", methods=["GET"])
def compare_peers(ticker: str):
    """Find peers in the same sector. Uses scraper.config.STOCK_SECTORS for the
    sector lookup and signals/events table for recent peer activity since this
    project doesn't carry a normalized 'stocks' table.
    """
    db = _get_db()
    cur = db.conn.cursor()
    ticker = ticker.upper()
    try:
        # Resolve the sector via the static map maintained in scraper/config.py
        try:
            from config import STOCK_SECTORS, SECTOR_STOCKS, STOCK_COMPANIES
        except Exception:
            STOCK_SECTORS = SECTOR_STOCKS = STOCK_COMPANIES = {}

        sector = STOCK_SECTORS.get(ticker)
        if not sector:
            return jsonify({"success": True, "sector": None, "data": []})
        peers = [t for t in (SECTOR_STOCKS.get(sector) or []) if t != ticker][:12]

        out = []
        for t in peers:
            row = {"ticker": t, "company": STOCK_COMPANIES.get(t, t), "sector": sector}
            # Best-effort: enrich with last known signal data
            try:
                cur.execute(
                    "SELECT alpha_score, sentiment, entry_price, created_at FROM signals WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
                    (t,),
                )
                sig = cur.fetchone()
                if sig:
                    row["alpha_score"] = sig["alpha_score"]
                    row["sentiment"] = sig["sentiment"]
                    row["last_price"] = sig["entry_price"]
                    row["last_signal_at"] = sig["created_at"]
            except Exception:
                pass
            out.append(row)
        return jsonify({"success": True, "sector": sector, "data": out})
    except Exception as e:
        return jsonify({"success": True, "data": [], "warning": str(e)})


@bp.route("/api/compare/<ticker>/recent-events", methods=["GET"])
def compare_recent_events(ticker: str):
    db = _get_db()
    cur = db.conn.cursor()
    cur.execute(
        """SELECT event_id, title, summary, sentiment, magnitude, event_type, source, link, created_at
           FROM events WHERE companies LIKE ?
           ORDER BY created_at DESC LIMIT 5""",
        (f"%{ticker.upper()}%",),
    )
    return jsonify({"success": True, "data": [dict(r) for r in cur.fetchall()]})


# ============ SOCIAL HUB (X verified / Reddit serious / Telegram serious) ===

@bp.route("/api/social/feed", methods=["GET"])
@ttl_cache(seconds=60)
def social_feed():
    """Unified social feed.

    Query params:
      platform=x|reddit|telegram     (default: all)
      ticker=RELIANCE                 (optional filter)
      severity=quick|serious          (optional filter)
      verified=1                      (X-verified-only quick news)
      hours=12                        (freshness window)
      limit=50
    """
    db = _get_db()
    try:
        from social.hub import feed, ensure_schema
        ensure_schema(db)
        return jsonify({"success": True, "data": feed(
            db,
            platform=request.args.get("platform"),
            ticker=(request.args.get("ticker") or "").upper().strip() or None,
            severity=request.args.get("severity"),
            verified_only=request.args.get("verified") in ("1", "true", "True"),
            hours=int(request.args.get("hours", "12")),
            limit=int(request.args.get("limit", "50")),
        )})
    except Exception as e:
        return jsonify({"success": True, "data": [], "warning": str(e)})


@bp.route("/api/social/sources", methods=["GET"])
def social_sources_list():
    db = _get_db()
    try:
        from social.hub import ensure_schema, seed_default_sources
        ensure_schema(db); seed_default_sources(db)
        cur = db.conn.cursor()
        cur.execute("""SELECT id, platform, handle, kind, active FROM social_sources_user
                       WHERE user_id IN (?, 'default') ORDER BY platform, handle""",
                    (_user_id(),))
        return jsonify({"success": True, "data": [dict(r) for r in cur.fetchall()]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/social/sources", methods=["POST"])
def social_sources_add():
    db = _get_db()
    try:
        from social.hub import ensure_schema
        ensure_schema(db)
        data = request.get_json(force=True) or {}
        platform = (data.get("platform") or "").strip().lower()
        handle = (data.get("handle") or "").strip().lstrip("@").lstrip("/").lstrip("r/")
        kind = data.get("kind") or ("verified_quick" if platform == "x" else "serious_only")
        if platform not in ("x", "reddit", "telegram") or not handle:
            return jsonify({"success": False, "error": "platform must be x|reddit|telegram, handle required"}), 400
        cur = db.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO social_sources_user (user_id, platform, handle, kind) VALUES (?, ?, ?, ?)",
            (_user_id(), platform, handle, kind),
        )
        db.conn.commit()
        return jsonify({"success": True, "id": cur.lastrowid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/social/sources/<int:sid>", methods=["DELETE"])
def social_sources_delete(sid: int):
    db = _get_db()
    cur = db.conn.cursor()
    cur.execute("DELETE FROM social_sources_user WHERE id = ? AND user_id = ?", (sid, _user_id()))
    db.conn.commit()
    return jsonify({"success": cur.rowcount > 0})


@bp.route("/api/social/refresh", methods=["POST"])
def social_refresh():
    """Manual collection trigger — runs all three platforms now."""
    db = _get_db()
    try:
        from social.hub import run_all
        from stock_universe import UNIVERSE_TICKERS
        results = run_all(db, UNIVERSE_TICKERS)
        return jsonify({"success": True, "data": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============ STOCK DETAIL PAGE — fundamentals tabs =========================
# Tabs: profile, financials (P&L/BS/CF), ratios, shareholding, corp-actions, news.
# Tickertape-style coverage, but the page itself surfaces event-track-record first.

_STOCK_CACHE: Dict[str, Dict] = {}
_STOCK_CACHE_TTL = 60 * 60 * 6  # 6h — fundamentals don't change intraday


def _cached(key: str, ttl: int, builder):
    rec = _STOCK_CACHE.get(key)
    now = time.time()
    if rec and (now - rec["t"]) < ttl:
        return rec["v"]
    v = builder()
    _STOCK_CACHE[key] = {"t": now, "v": v}
    return v


def _yf_ticker(ticker: str):
    import yfinance as yf
    return yf.Ticker(f"{ticker.upper()}.NS")


def _df_to_year_records(df, max_periods: int = 5) -> List[Dict]:
    """Flatten a yfinance financials DataFrame (cols=dates, rows=line items)
    into [{period: 'FY24', <line>: <crore>, ...}, ...] (₹ crore)."""
    if df is None or df.empty:
        return []
    cols = list(df.columns)[:max_periods]
    out = []
    for c in cols:
        try:
            label = c.strftime("%b %Y") if hasattr(c, "strftime") else str(c)
        except Exception:
            label = str(c)
        rec = {"period": label}
        for idx, val in df[c].items():
            try:
                if val is None:
                    continue
                fval = float(val)
                if fval != fval:  # NaN
                    continue
                rec[str(idx)] = round(fval / 1e7, 2)  # to ₹ crore
            except Exception:
                continue
        out.append(rec)
    return out


@bp.route("/api/stock/<ticker>/profile", methods=["GET"])
def stock_profile(ticker: str):
    """Company description, sector, industry, address, key officers, web."""
    ticker = ticker.upper().strip()

    def build():
        try:
            info = _yf_ticker(ticker).info or {}
            return {
                "ticker": ticker,
                "company": info.get("longName") or info.get("shortName") or ticker,
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "summary": info.get("longBusinessSummary"),
                "website": info.get("website"),
                "country": info.get("country"),
                "city": info.get("city"),
                "employees": info.get("fullTimeEmployees"),
                "market_cap": info.get("marketCap"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "isin": info.get("isin"),
                "exchange": info.get("exchange"),
                "currency": info.get("currency", "INR"),
                "officers": [
                    {"name": o.get("name"), "title": o.get("title"), "age": o.get("age")}
                    for o in (info.get("companyOfficers") or [])[:6]
                ],
            }
        except Exception as e:
            return {"ticker": ticker, "error": str(e)}

    data = _cached(f"profile:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


@bp.route("/api/stock/<ticker>/financials", methods=["GET"])
def stock_financials(ticker: str):
    """Income statement, balance sheet, cash flow — last 4-5 fiscal years.
    Values returned in ₹ crore."""
    ticker = ticker.upper().strip()

    def build():
        try:
            t = _yf_ticker(ticker)
            return {
                "income_statement": _df_to_year_records(t.financials, 5),
                "balance_sheet": _df_to_year_records(t.balance_sheet, 5),
                "cash_flow": _df_to_year_records(t.cashflow, 5),
                "income_statement_q": _df_to_year_records(t.quarterly_financials, 4),
                "currency_unit": "₹ crore",
            }
        except Exception as e:
            return {"error": str(e)}

    data = _cached(f"fin:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


@bp.route("/api/stock/<ticker>/ratios", methods=["GET"])
def stock_ratios(ticker: str):
    """Key ratios: PE, PB, ROE, ROCE, debt/equity, current ratio, margins, yield, EPS."""
    ticker = ticker.upper().strip()

    def build():
        try:
            info = _yf_ticker(ticker).info or {}

            def _pct(v):
                if v is None:
                    return None
                try:
                    return round(float(v) * 100, 2)
                except Exception:
                    return None

            return {
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "pb_ratio": info.get("priceToBook"),
                "ps_ratio": info.get("priceToSalesTrailing12Months"),
                "peg_ratio": info.get("pegRatio"),
                "ev_to_ebitda": info.get("enterpriseToEbitda"),
                "ev_to_revenue": info.get("enterpriseToRevenue"),
                "eps_ttm": info.get("trailingEps"),
                "eps_forward": info.get("forwardEps"),
                "book_value": info.get("bookValue"),
                "roe_pct": _pct(info.get("returnOnEquity")),
                "roa_pct": _pct(info.get("returnOnAssets")),
                "operating_margin_pct": _pct(info.get("operatingMargins")),
                "profit_margin_pct": _pct(info.get("profitMargins")),
                "gross_margin_pct": _pct(info.get("grossMargins")),
                "ebitda_margin_pct": _pct(info.get("ebitdaMargins")),
                "debt_to_equity": info.get("debtToEquity"),
                "current_ratio": info.get("currentRatio"),
                "quick_ratio": info.get("quickRatio"),
                "dividend_yield_pct": _pct(info.get("dividendYield")),
                "payout_ratio_pct": _pct(info.get("payoutRatio")),
                "dividend_rate": info.get("dividendRate"),
                "beta": info.get("beta"),
                "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
                "fifty_day_avg": info.get("fiftyDayAverage"),
                "two_hundred_day_avg": info.get("twoHundredDayAverage"),
                "revenue_growth_pct": _pct(info.get("revenueGrowth")),
                "earnings_growth_pct": _pct(info.get("earningsGrowth")),
                "revenue_ttm": info.get("totalRevenue"),
                "ebitda_ttm": info.get("ebitda"),
                "net_income_ttm": info.get("netIncomeToCommon"),
                "total_cash": info.get("totalCash"),
                "total_debt": info.get("totalDebt"),
                "free_cashflow": info.get("freeCashflow"),
                "operating_cashflow": info.get("operatingCashflow"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "float_shares": info.get("floatShares"),
                "held_pct_insiders": _pct(info.get("heldPercentInsiders")),
                "held_pct_institutions": _pct(info.get("heldPercentInstitutions")),
            }
        except Exception as e:
            return {"error": str(e)}

    data = _cached(f"ratios:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


# ── Fundamental score: aggregate of ratios + promoter pledge into a single ─
# ── 0-100 grade with reasoning. Used to gate recommendations so we don't  ─
# ── pump a structurally bad stock just because the news is good.          ─
@bp.route("/api/fundamentals/score/<ticker>", methods=["GET"])
def fundamentals_score(ticker: str):
    """Return a 0-100 fundamental grade with reasoning + red flags.

    Components (each contributes a positive or negative band):
      - Valuation       (PE, PB)         ±15
      - Profitability   (ROE, margins)   ±20
      - Growth          (revenue, eps)   ±15
      - Leverage        (D/E)            ±15
      - Cashflow        (FCF positive)   ±10
      - Promoter pledge (from DB)        ±15
      - Institutional holding             ±10

    Bands:
      80-100  strong
      60-79   decent
      40-59   weak
      0-39    avoid (recommendation gated)
    """
    ticker = ticker.upper().strip()

    def build():
        try:
            info = _yf_ticker(ticker).info or {}
        except Exception as e:
            return {"error": str(e), "score": None}

        score = 50  # neutral baseline
        positives: List[str] = []
        red_flags: List[str] = []
        components: Dict[str, Dict] = {}

        # ── Valuation ────────────────────────────────────────────────────
        pe = info.get("trailingPE")
        pb = info.get("priceToBook")
        val_score = 0
        if pe is not None:
            try:
                pe = float(pe)
                if 0 < pe <= 18:
                    val_score += 8; positives.append(f"PE {pe:.1f} — reasonably priced")
                elif pe <= 30:
                    val_score += 2
                elif pe <= 60:
                    val_score -= 4; red_flags.append(f"PE {pe:.1f} — rich")
                else:
                    val_score -= 8; red_flags.append(f"PE {pe:.1f} — very expensive")
            except Exception: pass
        if pb is not None:
            try:
                pb = float(pb)
                if 0 < pb <= 3:
                    val_score += 4
                elif pb <= 8:
                    val_score += 0
                else:
                    val_score -= 4; red_flags.append(f"PB {pb:.1f} — stretched")
            except Exception: pass
        components["valuation"] = {"score": val_score, "pe": pe, "pb": pb}
        score += val_score

        # ── Profitability (ROE + operating margin) ───────────────────────
        roe = info.get("returnOnEquity")
        op_m = info.get("operatingMargins")
        prof_score = 0
        if roe is not None:
            try:
                roe_pct = float(roe) * 100
                if roe_pct >= 20:
                    prof_score += 12; positives.append(f"ROE {roe_pct:.1f}% — high quality")
                elif roe_pct >= 12:
                    prof_score += 6; positives.append(f"ROE {roe_pct:.1f}% — solid")
                elif roe_pct >= 5:
                    prof_score += 0
                elif roe_pct >= 0:
                    prof_score -= 6; red_flags.append(f"ROE {roe_pct:.1f}% — weak")
                else:
                    prof_score -= 12; red_flags.append(f"ROE {roe_pct:.1f}% — destroying capital")
            except Exception: pass
        if op_m is not None:
            try:
                m = float(op_m) * 100
                if m >= 20: prof_score += 6; positives.append(f"Op margin {m:.1f}% — strong")
                elif m >= 10: prof_score += 3
                elif m >= 0: pass
                else: prof_score -= 8; red_flags.append(f"Op margin {m:.1f}% — operating losses")
            except Exception: pass
        components["profitability"] = {"score": prof_score, "roe_pct": (roe or 0) * 100 if roe else None,
                                        "operating_margin_pct": (op_m or 0) * 100 if op_m else None}
        score += prof_score

        # ── Growth ───────────────────────────────────────────────────────
        rev_g = info.get("revenueGrowth")
        eps_g = info.get("earningsGrowth")
        grow_score = 0
        if rev_g is not None:
            try:
                g = float(rev_g) * 100
                if g >= 20: grow_score += 8; positives.append(f"Revenue +{g:.1f}% YoY")
                elif g >= 8: grow_score += 4; positives.append(f"Revenue +{g:.1f}% YoY")
                elif g >= 0: grow_score += 0
                else: grow_score -= 6; red_flags.append(f"Revenue {g:.1f}% YoY — shrinking")
            except Exception: pass
        if eps_g is not None:
            try:
                g = float(eps_g) * 100
                if g >= 25: grow_score += 7; positives.append(f"EPS +{g:.1f}%")
                elif g >= 10: grow_score += 3
                elif g >= 0: grow_score += 0
                else: grow_score -= 5; red_flags.append(f"EPS {g:.1f}% — declining")
            except Exception: pass
        components["growth"] = {"score": grow_score,
                                 "revenue_growth_pct": (rev_g or 0) * 100 if rev_g else None,
                                 "earnings_growth_pct": (eps_g or 0) * 100 if eps_g else None}
        score += grow_score

        # ── Leverage (debt/equity) ───────────────────────────────────────
        de = info.get("debtToEquity")
        lev_score = 0
        if de is not None:
            try:
                de_v = float(de)
                # yfinance reports as ratio*100 sometimes — normalise
                if de_v > 10: de_v = de_v / 100.0
                if de_v <= 0.3:
                    lev_score += 8; positives.append(f"D/E {de_v:.2f} — almost debt-free")
                elif de_v <= 0.8:
                    lev_score += 3
                elif de_v <= 1.5:
                    lev_score -= 4; red_flags.append(f"D/E {de_v:.2f} — high leverage")
                else:
                    lev_score -= 10; red_flags.append(f"D/E {de_v:.2f} — over-leveraged")
            except Exception: pass
        components["leverage"] = {"score": lev_score, "debt_to_equity": de}
        score += lev_score

        # ── Cashflow ─────────────────────────────────────────────────────
        fcf = info.get("freeCashflow")
        ocf = info.get("operatingCashflow")
        cf_score = 0
        try:
            if fcf is not None and float(fcf) > 0:
                cf_score += 6; positives.append("Free cash flow positive")
            elif fcf is not None and float(fcf) < 0:
                cf_score -= 6; red_flags.append("Free cash flow negative")
        except Exception: pass
        try:
            if ocf is not None and float(ocf) > 0:
                cf_score += 2
            elif ocf is not None and float(ocf) < 0:
                cf_score -= 4; red_flags.append("Operating cash flow negative")
        except Exception: pass
        components["cashflow"] = {"score": cf_score, "free_cashflow": fcf, "operating_cashflow": ocf}
        score += cf_score

        # ── Promoter pledge (from DB if available) ──────────────────────
        pledge_score = 0
        pledge_pct = None
        try:
            db = _get_db() if _get_db else None
            if db is not None and getattr(db, "conn", None) is not None:
                cur = db.conn.cursor()
                p = "%s" if getattr(db, "is_postgres", False) else "?"
                cur.execute(
                    f"SELECT promoter_pledge_pct, promoter_pct FROM promoter_holdings "
                    f"WHERE ticker = {p} ORDER BY quarter_end DESC LIMIT 1",
                    (ticker,),
                )
                row = cur.fetchone()
                if row:
                    pledge_pct = float(row[0] or 0) if not isinstance(row, dict) else float(row.get("promoter_pledge_pct") or 0)
                    if pledge_pct <= 1:
                        pledge_score += 6; positives.append("No promoter pledge")
                    elif pledge_pct <= 10:
                        pledge_score += 0
                    elif pledge_pct <= 25:
                        pledge_score -= 6; red_flags.append(f"Promoter pledge {pledge_pct:.1f}%")
                    else:
                        pledge_score -= 12; red_flags.append(f"Promoter pledge {pledge_pct:.1f}% — critical")
        except Exception as e:
            logger.debug("pledge lookup failed: %s", e)
        components["promoter_pledge"] = {"score": pledge_score, "pledge_pct": pledge_pct}
        score += pledge_score

        # ── Institutional holding ────────────────────────────────────────
        inst = info.get("heldPercentInstitutions")
        inst_score = 0
        try:
            if inst is not None:
                inst_v = float(inst) * 100
                if inst_v >= 30:
                    inst_score += 5; positives.append(f"Institutions hold {inst_v:.0f}%")
                elif inst_v >= 10:
                    inst_score += 2
                elif inst_v < 3:
                    inst_score -= 3
        except Exception: pass
        components["institutional"] = {"score": inst_score,
                                        "held_pct_institutions": (inst or 0) * 100 if inst else None}
        score += inst_score

        # ── Final clamp + tier ───────────────────────────────────────────
        final = max(0, min(100, int(round(score))))
        if final >= 80:
            tier, label = "strong", "Strong fundamentals"
        elif final >= 60:
            tier, label = "decent", "Decent fundamentals"
        elif final >= 40:
            tier, label = "weak", "Weak fundamentals — be cautious"
        else:
            tier, label = "avoid", "Poor fundamentals — avoid"

        return {
            "ticker": ticker,
            "score": final,
            "tier": tier,
            "label": label,
            "positives": positives[:6],
            "red_flags": red_flags[:6],
            "components": components,
        }

    data = _cached(f"fund_score:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


# Bulk variant — accepts ?tickers=TCS,RELIANCE,INFY and returns scores keyed by ticker
@bp.route("/api/fundamentals/score-bulk", methods=["GET"])
def fundamentals_score_bulk():
    raw = (request.args.get("tickers") or "").strip()
    if not raw:
        return jsonify({"success": True, "data": {}})
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()][:25]
    out: Dict[str, Dict] = {}
    for tk in tickers:
        try:
            resp = fundamentals_score(tk)
            payload = resp.get_json() if hasattr(resp, "get_json") else None
            if payload and payload.get("data"):
                out[tk] = payload["data"]
        except Exception as e:
            logger.debug("bulk score failed for %s: %s", tk, e)
    return jsonify({"success": True, "data": out})


@bp.route("/api/stock/<ticker>/shareholding", methods=["GET"])
def stock_shareholding(ticker: str):
    """Shareholding pattern — major holders, institutional list, mutual fund holders."""
    ticker = ticker.upper().strip()

    def build():
        try:
            t = _yf_ticker(ticker)
            out = {"major": [], "institutional": [], "mutual_funds": []}

            try:
                mh = t.major_holders
                if mh is not None and not mh.empty:
                    for _, row in mh.iterrows():
                        vals = [str(v) for v in row.tolist()]
                        if len(vals) >= 2:
                            out["major"].append({"label": vals[1], "pct": vals[0]})
            except Exception:
                pass

            try:
                ih = t.institutional_holders
                if ih is not None and not ih.empty:
                    for _, row in ih.iterrows():
                        out["institutional"].append({
                            "holder": str(row.get("Holder", "")),
                            "shares": int(row.get("Shares", 0) or 0),
                            "date_reported": str(row.get("Date Reported", "")),
                            "pct_out": float(row.get("% Out", 0) or 0),
                            "value": int(row.get("Value", 0) or 0),
                        })
            except Exception:
                pass

            try:
                mf = t.mutualfund_holders
                if mf is not None and not mf.empty:
                    for _, row in mf.iterrows():
                        out["mutual_funds"].append({
                            "holder": str(row.get("Holder", "")),
                            "shares": int(row.get("Shares", 0) or 0),
                            "date_reported": str(row.get("Date Reported", "")),
                            "pct_out": float(row.get("% Out", 0) or 0),
                            "value": int(row.get("Value", 0) or 0),
                        })
            except Exception:
                pass

            return out
        except Exception as e:
            return {"error": str(e)}

    data = _cached(f"sh:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


@bp.route("/api/stock/<ticker>/corp-actions", methods=["GET"])
def stock_corp_actions(ticker: str):
    """Corporate actions: dividends history, splits."""
    ticker = ticker.upper().strip()

    def build():
        try:
            t = _yf_ticker(ticker)
            divs, splits = [], []
            try:
                d = t.dividends
                if d is not None and not d.empty:
                    for dt, val in list(d.items())[-30:]:
                        divs.append({
                            "ex_date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),
                            "amount": round(float(val), 2),
                        })
            except Exception:
                pass
            try:
                s = t.splits
                if s is not None and not s.empty:
                    for dt, val in list(s.items())[-20:]:
                        splits.append({
                            "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),
                            "ratio": round(float(val), 4),
                        })
            except Exception:
                pass
            divs.reverse()
            splits.reverse()
            return {"dividends": divs, "splits": splits}
        except Exception as e:
            return {"error": str(e)}

    data = _cached(f"corp:{ticker}", _STOCK_CACHE_TTL, build)
    return jsonify({"success": True, "data": data})


@bp.route("/api/stock/<ticker>/news", methods=["GET"])
@ttl_cache(seconds=180)
def stock_news(ticker: str):
    """Recent news headlines from yfinance, plus this app's own events for the ticker.

    yfinance changed the news payload around 0.2.40: fields moved from flat
    keys (`title`, `link`, `publisher`, `providerPublishTime`) into a nested
    `content` object with `title`, `clickThroughUrl.url`, `provider.displayName`,
    `pubDate` (ISO string). We support both shapes so a yfinance upgrade
    doesn't blank out the page.
    """
    ticker = ticker.upper().strip()
    out = {"news": [], "events": []}

    def _norm(n):
        # New nested shape
        c = n.get("content") if isinstance(n, dict) else None
        if isinstance(c, dict):
            link = (c.get("clickThroughUrl") or {}).get("url") or \
                   (c.get("canonicalUrl") or {}).get("url")
            provider = ((c.get("provider") or {}).get("displayName")
                        or (c.get("provider") or {}).get("url"))
            pub_iso = c.get("pubDate") or c.get("displayTime")
            # Convert ISO → unix seconds for the frontend's fmt.date helper
            published_ts = None
            if pub_iso:
                try:
                    from datetime import datetime as _dt
                    s = pub_iso.replace("Z", "+00:00")
                    published_ts = int(_dt.fromisoformat(s).timestamp())
                except Exception:
                    published_ts = None
            return {
                "title": c.get("title"),
                "publisher": provider,
                "link": link,
                "published": published_ts,
                "type": c.get("contentType") or c.get("type"),
            }
        # Old flat shape
        return {
            "title": n.get("title"),
            "publisher": n.get("publisher"),
            "link": n.get("link"),
            "published": n.get("providerPublishTime"),
            "type": n.get("type"),
        }

    try:
        t = _yf_ticker(ticker)
        for n in (t.news or [])[:25]:
            row = _norm(n)
            # Skip rows where we still couldn't extract a title — protects
            # the UI from rendering "null" placeholders.
            if row.get("title"):
                out["news"].append(row)
    except Exception as exc:
        logger.debug(f"stock_news yfinance failed for {ticker}: {exc}")

    try:
        db = _get_db()
        cur = db.conn.cursor()
        cur.execute(
            """SELECT event_id, title, summary, sentiment, magnitude, event_type,
                      source, link, created_at
               FROM events WHERE companies LIKE ?
               ORDER BY created_at DESC LIMIT 25""",
            (f"%{ticker}%",),
        )
        out["events"] = [dict(r) for r in cur.fetchall()]
    except Exception:
        pass

    return jsonify({"success": True, "data": out})


# ============ STOCK INTELLIGENCE & SUMMARIZATION ============
# Builds Pros/Cons/Drivers from the data we already collect. No new
# computation — only aggregation of: signals, events, forensic flags,
# bulk deals, F&O unusual flow, premover scores.
#
# News summaries use Groq (when GROQ_API_KEY is set) with an extractive
# fallback (first sentence + key facts) when budget is exhausted or the
# LLM is unavailable.


def _classify_pros_cons(signals_rows, events_rows, manipulation_rows,
                         bulk_rows, fo_rows, ticker):
    """Walk the raw rows once and emit balanced pros/cons buckets.

    Each bullet is grounded in a specific data point so the panel never
    shows hand-waving — every claim points at a real signal/event/flag.
    """
    pros, cons = [], []

    # 1) Active signals — bullish ones become drivers, bearish become risks
    for s in signals_rows:
        alpha = float(s.get("alpha_score") or 0)
        sent = (s.get("sentiment") or "").lower()
        et = (s.get("event_type") or "news").replace("_", " ")
        head = (s.get("headline") or "").strip()
        gist = head[:140] + ("…" if len(head) > 140 else "")
        weight = "strong" if alpha >= 75 else "moderate" if alpha >= 60 else "weak"
        item = {
            "label": gist or f"{et} signal · α {alpha:.0f}",
            "weight": weight,
            "alpha": round(alpha, 1),
            "kind": s.get("event_type"),
            "source": s.get("source") or "Tickwave",
            "ts": str(s.get("created_at") or ""),
            "link": s.get("link") or "",
        }
        if sent == "bullish":
            pros.append(item)
        elif sent == "bearish":
            cons.append(item)

    # 2) Forensic flags — always cons (manipulation scores)
    for m in manipulation_rows:
        band = (m.get("band") or "").lower()
        score = m.get("score")
        if band and band != "clean":
            cons.append({
                "label": f"Forensic flag: {band.replace('_', ' ')}",
                "weight": "strong",
                "alpha": None,
                "kind": "forensic",
                "source": "Manipulation scoring",
                "ts": str(m.get("as_of") or ""),
                "detail": (
                    f"Score {float(score):.1f}/100" if score is not None else None
                ),
            })

    # 3) Bulk deals — buy = pro, sell = con (only block-size-relevant)
    for b in bulk_rows:
        try:
            value_cr = float(b.get("deal_value_cr") or 0)
        except (TypeError, ValueError):
            value_cr = 0
        if value_cr < 5:
            continue
        party = (b.get("buyer_seller") or "").strip()
        is_buy = "buy" in party.lower()
        bucket = pros if is_buy else cons
        weight = "strong" if value_cr >= 50 else "moderate" if value_cr >= 15 else "weak"
        bucket.append({
            "label": f"{'BUY' if is_buy else 'SELL'} bulk deal · ₹{value_cr:.1f} Cr",
            "weight": weight,
            "alpha": None,
            "kind": "bulk_deal",
            "source": b.get("exchange") or "NSE/BSE",
            "ts": str(b.get("deal_date") or ""),
            "detail": party,
        })

    # 4) F&O unusual flow — direction-coded if available
    for f in fo_rows:
        sig = (f.get("signal_type") or "unusual_flow").replace("_", " ")
        detail = f.get("detail") or ""
        # Heuristic: tag as risk by default unless detail says "call buying"
        is_bullish = "call" in detail.lower() and "buy" in detail.lower()
        bucket = pros if is_bullish else cons
        bucket.append({
            "label": f"F&O unusual: {sig}",
            "weight": "moderate",
            "alpha": None,
            "kind": "fo_unusual",
            "source": "F&O monitor",
            "ts": str(f.get("created_at") or ""),
            "detail": detail,
        })

    # Sort each bucket by weight then alpha desc; cap at 6 bullets each
    weight_order = {"strong": 0, "moderate": 1, "weak": 2}
    def _key(it):
        return (weight_order.get(it.get("weight"), 9), -(it.get("alpha") or 0))
    pros.sort(key=_key)
    cons.sort(key=_key)
    return pros[:6], cons[:6]


def _what_to_watch(events_rows, ticker):
    """Surface upcoming/recent catalysts. Pulls from the events table for
    earnings/policy/order_win/merger/dividend that are within 14 days."""
    out = []
    seen_kinds = set()
    for e in events_rows:
        et = (e.get("event_type") or "").lower()
        if et in seen_kinds:
            continue
        if et in ("earnings", "policy", "merger", "order_win", "dividend", "ipo"):
            seen_kinds.add(et)
            out.append({
                "label": (e.get("title") or "").strip()[:140],
                "kind": et,
                "ts": str(e.get("published_at") or e.get("created_at") or ""),
                "link": e.get("link") or "",
            })
        if len(out) >= 4:
            break
    return out


def _stance_from_buckets(pros, cons):
    """Net thesis score 0..100 from bucket weights + alphas.
    Positive bias if pros heavily outweigh cons, negative if reverse."""
    def _bucket_score(items):
        total = 0.0
        for it in items:
            w = {"strong": 30, "moderate": 18, "weak": 8}.get(it.get("weight"), 8)
            a = it.get("alpha")
            total += w + (max(0, (a or 0) - 50) * 0.4)
        return total
    p = _bucket_score(pros)
    c = _bucket_score(cons)
    if p == 0 and c == 0:
        return 50, "neutral"
    raw = (p - c) / max(p + c, 1)  # -1..+1
    score = round(50 + raw * 50)
    score = max(0, min(100, score))
    if score >= 65:
        stance = "bullish"
    elif score <= 35:
        stance = "bearish"
    else:
        stance = "neutral"
    return score, stance


@bp.route("/api/stock/<ticker>/intelligence", methods=["GET"])
@ttl_cache(seconds=120)
def stock_intelligence(ticker: str):
    """Auto-generated Pros / Cons / What-to-watch for a stock.

    All bullets are grounded in real rows — signals, events, forensic
    flags, bulk deals, F&O unusual flow. No invented metrics. Returns:
        {ticker, thesis_score, stance, pros[], cons[], what_to_watch[],
         narrative, generated_at, signals_seen}
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return jsonify({"success": False, "error": "ticker required"}), 400

    db = _get_db()
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503

    days = max(1, min(180, int(request.args.get("days", 30))))

    signals_rows: List[Dict] = []
    events_rows: List[Dict] = []
    manip_rows: List[Dict] = []
    bulk_rows: List[Dict] = []
    fo_rows: List[Dict] = []

    try:
        cur = db.conn.cursor()
        # Signals
        try:
            cur.execute(
                """SELECT event_id, ticker, alpha_score, confidence, sentiment,
                          event_type, headline, source, link, created_at
                   FROM signals
                   WHERE ticker = ?
                     AND created_at >= datetime('now', ?)
                   ORDER BY alpha_score DESC, created_at DESC LIMIT 12""",
                (ticker, f"-{days} days"),
            )
            signals_rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"intel signals: {e}")

        # Events for ticker (for what-to-watch)
        try:
            cur.execute(
                """SELECT event_id, title, summary, event_type, sentiment,
                          source, link, published_at, created_at
                   FROM events
                   WHERE companies LIKE ?
                     AND created_at >= datetime('now', ?)
                   ORDER BY created_at DESC LIMIT 30""",
                (f"%{ticker}%", f"-{days} days"),
            )
            events_rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"intel events: {e}")

        # Forensic flags
        try:
            cur.execute(
                """SELECT ticker, band, score, as_of
                   FROM manipulation_scores
                   WHERE ticker = ?
                     AND as_of >= datetime('now', ?)
                   ORDER BY as_of DESC LIMIT 5""",
                (ticker, f"-{days} days"),
            )
            manip_rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"intel manipulation: {e}")

        # Bulk deals
        try:
            cur.execute(
                """SELECT deal_date, ticker, deal_value_cr, deal_price,
                          buyer_seller, exchange
                   FROM bulk_deals
                   WHERE ticker = ?
                     AND deal_date >= datetime('now', ?)
                   ORDER BY deal_date DESC LIMIT 10""",
                (ticker, f"-{days} days"),
            )
            bulk_rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"intel bulk: {e}")

        # F&O unusual
        try:
            cur.execute(
                """SELECT ticker, signal_type, magnitude, detail, created_at
                   FROM fo_unusual
                   WHERE ticker = ?
                     AND created_at >= datetime('now', ?)
                   ORDER BY created_at DESC LIMIT 5""",
                (ticker, f"-{days} days"),
            )
            fo_rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"intel fo: {e}")
    except Exception as e:
        logger.warning(f"stock_intelligence: db sweep failed: {e}")

    pros, cons = _classify_pros_cons(signals_rows, events_rows, manip_rows,
                                      bulk_rows, fo_rows, ticker)
    watch = _what_to_watch(events_rows, ticker)
    score, stance = _stance_from_buckets(pros, cons)

    # Narrative — short, factual, pulls strongest pro + strongest con
    narrative_parts = []
    if pros:
        narrative_parts.append(f"Bull case: {pros[0]['label']}")
    if cons:
        narrative_parts.append(f"Bear case: {cons[0]['label']}")
    if not narrative_parts:
        narrative_parts.append("No active signals on this ticker in the lookback window.")
    narrative = " · ".join(narrative_parts)

    return jsonify({
        "success": True,
        "data": {
            "ticker": ticker,
            "thesis_score": score,
            "stance": stance,
            "pros": pros,
            "cons": cons,
            "what_to_watch": watch,
            "narrative": narrative,
            "signals_seen": len(signals_rows),
            "events_seen": len(events_rows),
            "lookback_days": days,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
    })


# ---- News summarization (Groq → extractive fallback) -----------------------

def _extractive_summary(title: str, body: str, max_chars: int = 220) -> str:
    """Cheap extractive summary — first sentence(s) up to max_chars.
    No magic; just a clean cut so the UI never gets a wall of text."""
    text = (body or "").strip()
    if not text:
        return (title or "").strip()
    # First sentence-ish boundary
    cut_points = [text.find(". "), text.find("? "), text.find("! ")]
    cuts = [c for c in cut_points if 30 <= c <= max_chars]
    if cuts:
        end = min(cuts) + 1
        return text[:end].strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _groq_summarize(title: str, body: str) -> Optional[str]:
    """Try Groq for a 1-sentence TL;DR. Returns None on any failure
    (no key, governor refused, network, JSON parse). The caller falls back
    to extractive when this returns None."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.info("groq summarize skipped: GROQ_API_KEY not set")
        return None
    # Soft budget check via the existing governor — we share the JARGON budget
    # since summaries are short and that bucket is otherwise unused.
    governor = None
    try:
        import sys as _sys
        scraper_path = os.path.join(os.path.dirname(__file__), "..", "scraper")
        if scraper_path not in _sys.path:
            _sys.path.insert(0, scraper_path)
        from groq_governor import governor as _gov  # type: ignore
        governor = _gov
        if not governor.can_spend("jargon", est_tokens=300):
            logger.info("groq summarize skipped: governor refused (jargon budget exhausted or CB open)")
            return None
    except Exception as e:
        logger.info(f"groq summarize: governor unavailable, proceeding without budget check ({e})")
    prompt = (
        "Summarize this Indian-markets news in ONE clear sentence "
        "(max 35 words). Plain English. No hedging.\n\n"
        f"Headline: {title}\n\nBody: {(body or '')[:1200]}"
    )
    try:
        import urllib.request
        import urllib.error
        import json as _json
        # 8B model is plenty for 1-sentence TL;DRs and has 14× higher daily
        # request quota than the 70B (14,400 RPD vs 1,000) at a fraction of
        # the tokens/output. Override via GROQ_TLDR_MODEL if you want 70B.
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=_json.dumps({
                "model": os.getenv("GROQ_TLDR_MODEL", "llama-3.1-8b-instant"),
                "messages": [
                    {"role": "system", "content": "Tight TL;DRs for traders. One sentence."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 80,
            }).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            j = _json.loads(resp.read())
        choice = (j.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip().strip('"')
        try:
            if governor:
                usage = j.get("usage") or {}
                governor.record_spend("jargon", int(usage.get("total_tokens") or 200))
        except Exception:
            pass
        if not text:
            logger.warning("groq summarize: empty completion text")
            return None
        return text
    except urllib.error.HTTPError as e:
        body_excerpt = ""
        try:
            body_excerpt = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        logger.warning(f"groq summarize HTTPError {e.code}: {body_excerpt}")
        try:
            if governor:
                governor.record_429("jargon")
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning(f"groq summarize failed ({type(e).__name__}): {e}")
        try:
            if governor:
                governor.record_429("jargon")
        except Exception:
            pass
        return None


_SUMMARY_CACHE: Dict[str, Dict] = {}
_SUMMARY_TTL_SECS = 60 * 60 * 24  # 24h in-memory; disk cache below is permanent
_SUMMARY_DISK_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "summary_cache.json"
)
_SUMMARY_DISK_DIRTY = False
_SUMMARY_DISK: Dict[str, Dict] = {}


def _disk_cache_load():
    """Load the on-disk summary cache once on first use. Article TL;DRs never
    change so we keep them forever — survives restarts, cuts Groq spend by
    not re-summarizing the same article tomorrow."""
    global _SUMMARY_DISK
    if _SUMMARY_DISK:
        return
    try:
        with open(_SUMMARY_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
            _SUMMARY_DISK = json.load(f)
    except FileNotFoundError:
        _SUMMARY_DISK = {}
    except Exception as e:
        logger.warning(f"summary disk cache load failed: {e}")
        _SUMMARY_DISK = {}


def _disk_cache_save():
    """Best-effort persist (called after each new summary). Atomic write so a
    crash mid-write doesn't corrupt the file."""
    try:
        tmp = _SUMMARY_DISK_CACHE_PATH + ".tmp"
        os.makedirs(os.path.dirname(_SUMMARY_DISK_CACHE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SUMMARY_DISK, f)
        os.replace(tmp, _SUMMARY_DISK_CACHE_PATH)
    except Exception as e:
        logger.debug(f"summary disk cache save: {e}")


@bp.route("/api/news/summarize", methods=["GET", "POST"])
def news_summarize():
    """Summarize a news article into one sentence.

    GET  /api/news/summarize?event_id=<id>          → looks up events table
    GET  /api/news/summarize?title=...&body=...      → ad-hoc
    POST body {title, summary?, body?, event_id?}    → ad-hoc

    Response: {"summary": "...", "source": "groq" | "extractive", "cached": bool}
    """
    payload = request.get_json(silent=True) or {}
    args = request.args

    event_id = (args.get("event_id") or payload.get("event_id") or "").strip()
    title = (args.get("title") or payload.get("title") or "").strip()
    body = (args.get("body") or payload.get("body")
            or args.get("summary") or payload.get("summary") or "").strip()

    # If the caller passed an event_id, look up canonical text from events table
    if event_id and (not title or not body):
        try:
            db = _get_db()
            cur = db.conn.cursor()
            cur.execute(
                "SELECT title, summary FROM events WHERE event_id = ? LIMIT 1",
                (event_id,),
            )
            row = cur.fetchone()
            if row:
                title = title or (row["title"] or "")
                body = body or (row["summary"] or "")
        except Exception as e:
            logger.debug(f"news_summarize lookup failed: {e}")

    if not title and not body:
        return jsonify({"success": False, "error": "title or body required"}), 400

    cache_key = event_id or (title[:80] + "|" + body[:80])

    # Check in-memory cache first (fastest)
    cached = _SUMMARY_CACHE.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < _SUMMARY_TTL_SECS:
        return jsonify({"success": True, "data": {**cached["payload"], "cached": True}})

    # Then check on-disk cache — article TL;DRs never expire (the article
    # content doesn't change). This is the BIG saving: same article summarized
    # once across all restarts, all users, forever.
    _disk_cache_load()
    if cache_key in _SUMMARY_DISK:
        payload = _SUMMARY_DISK[cache_key]
        _SUMMARY_CACHE[cache_key] = {"_ts": time.time(), "payload": payload}
        return jsonify({"success": True, "data": {**payload, "cached": True, "from": "disk"}})

    summary = _groq_summarize(title, body)
    if summary:
        out = {"summary": summary, "source": "groq", "cached": False}
    else:
        out = {"summary": _extractive_summary(title, body), "source": "extractive", "cached": False}

    _SUMMARY_CACHE[cache_key] = {"_ts": time.time(), "payload": out}
    # Persist Groq results forever; skip extractive (cheap to recompute)
    if out.get("source") == "groq":
        _SUMMARY_DISK[cache_key] = out
        _disk_cache_save()
    return jsonify({"success": True, "data": out})


# ============ STOCK DOCUMENTS — exchange filings, concalls, ratings ========
_CONCALL_HINTS = ("concall", "earnings call", "investor call", "transcript",
                   "investor meet", "analyst meet")
_RATING_HINTS = ("rating", "crisil", "icra", "care ", "moody", "fitch",
                  "s&p global", "india ratings", "brickwork")
_REPORT_HINTS = ("annual report", "annual general meeting", "agm",
                  "shareholders' meeting", "investor presentation")


def _bucket_doc(title: str, source: str) -> str:
    blob = ((title or "") + " " + (source or "")).lower()
    if any(k in blob for k in _CONCALL_HINTS):
        return "concall"
    if any(k in blob for k in _RATING_HINTS):
        return "rating"
    if any(k in blob for k in _REPORT_HINTS):
        return "annual_report"
    return "announcement"


@bp.route("/api/stock/<ticker>/documents", methods=["GET"])
@ttl_cache(seconds=300)
def stock_documents(ticker: str):
    """Return all corporate documents for a ticker, bucketed by type.
    Pulls from the events table (filings ingested from BSE/NSE/SEBI) and
    augments with yfinance SEC filings when available."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return jsonify({"success": False, "error": "ticker required"}), 400
    db = _get_db()
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    try:
        days = max(7, min(1095, int(request.args.get("days", 365))))
    except ValueError:
        days = 365

    buckets = {"announcements": [], "concalls": [], "ratings": [], "annual_reports": []}
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT event_id, title, summary, source, link, event_type,
                      published_at, created_at
               FROM events WHERE companies LIKE ?
                 AND created_at >= datetime('now', ?)
               ORDER BY created_at DESC LIMIT 200""",
            (f"%{ticker}%", f"-{days} days"),
        )
        for r in cur.fetchall():
            row = dict(r)
            kind = _bucket_doc(row.get("title") or "", row.get("source") or "")
            key = {"announcement": "announcements", "concall": "concalls",
                   "rating": "ratings", "annual_report": "annual_reports"}.get(kind, "announcements")
            buckets[key].append({
                "date": str(row.get("published_at") or row.get("created_at") or ""),
                "title": row.get("title") or "",
                "summary": (row.get("summary") or "")[:300],
                "source": row.get("source") or "",
                "link": row.get("link") or "",
                "event_id": row.get("event_id"),
            })
    except Exception as e:
        logger.warning(f"stock_documents events query failed: {e}")

    try:
        t = _yf_ticker(ticker)
        sec = getattr(t, "sec_filings", None) or []
        for f in list(sec or [])[:10]:
            if not isinstance(f, dict):
                continue
            buckets["annual_reports"].append({
                "date": str(f.get("date") or ""),
                "title": f.get("title") or f.get("type") or "Filing",
                "summary": "",
                "source": "SEC / Yahoo",
                "link": f.get("edgarUrl") or f.get("url") or "",
                "event_id": None,
            })
    except Exception as e:
        logger.debug(f"stock_documents yfinance filings: {e}")

    counts = {}
    for k, v in buckets.items():
        v.sort(key=lambda it: str(it.get("date") or ""), reverse=True)
        buckets[k] = v[:20]
        counts[k] = len(buckets[k])
    return jsonify({"success": True, "data": {
        "ticker": ticker, "lookback_days": days, "counts": counts, **buckets,
    }})


@bp.route("/api/stock/<ticker>/peers-detail", methods=["GET"])
def stock_peers_detail(ticker: str):
    """Peer comparison with Tickertape-style metrics: P/E, P/B, market-cap, ROE, alpha."""
    ticker = ticker.upper().strip()
    try:
        try:
            from config import STOCK_SECTORS, SECTOR_STOCKS, STOCK_COMPANIES
        except Exception:
            STOCK_SECTORS = SECTOR_STOCKS = STOCK_COMPANIES = {}
        sector = STOCK_SECTORS.get(ticker)
        peers_list = (SECTOR_STOCKS.get(sector) or []) if sector else []
        peers_list = [p for p in peers_list if p != ticker][:10]

        def enrich(t):
            row = {"ticker": t, "company": STOCK_COMPANIES.get(t, t)}
            try:
                info = _yf_ticker(t).info or {}
                row.update({
                    "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                    "change_pct": info.get("regularMarketChangePercent"),
                    "market_cap": info.get("marketCap"),
                    "pe": info.get("trailingPE"),
                    "pb": info.get("priceToBook"),
                    "roe_pct": (info.get("returnOnEquity") or 0) * 100 if info.get("returnOnEquity") else None,
                    "div_yield_pct": (info.get("dividendYield") or 0) * 100 if info.get("dividendYield") else None,
                })
            except Exception:
                pass
            try:
                db = _get_db()
                cur = db.conn.cursor()
                cur.execute(
                    "SELECT alpha_score, sentiment FROM signals WHERE ticker=? AND status='active' ORDER BY alpha_score DESC LIMIT 1",
                    (t,),
                )
                sig = cur.fetchone()
                if sig:
                    row["alpha_score"] = sig["alpha_score"]
                    row["sentiment"] = sig["sentiment"]
            except Exception:
                pass
            return row

        # Always include the subject ticker first for side-by-side comparison
        rows = [enrich(ticker)] + [enrich(p) for p in peers_list]
        return jsonify({"success": True, "sector": sector, "data": rows})
    except Exception as e:
        return jsonify({"success": True, "data": [], "warning": str(e)})


# ============ CUSTOM SCREENER — formula query over signals/fundamentals =====
# Tickertape-style filter builder. Accepts a list of {field, op, value} clauses
# joined with AND. Whitelist of fields prevents SQL injection.

_SCREEN_FIELDS = {
    # numeric, on signals table
    "alpha_score":   {"col": "s.alpha_score",   "type": "num", "label": "Alpha Score"},
    "confidence":    {"col": "s.confidence",    "type": "num", "label": "Confidence"},
    "magnitude":     {"col": "s.magnitude",     "type": "num", "label": "Magnitude"},
    "entry_price":   {"col": "s.entry_price",   "type": "num", "label": "Last Price (₹)"},
    # categorical
    "sentiment":     {"col": "s.sentiment",     "type": "cat", "label": "Sentiment",
                      "options": ["bullish", "bearish", "neutral"]},
    "event_type":    {"col": "s.event_type",    "type": "cat", "label": "Event Type"},
    "ticker":        {"col": "s.ticker",        "type": "cat", "label": "Ticker"},
    "forensic_band": {"col": "COALESCE(ms.band,'clean')", "type": "cat",
                      "label": "Forensic Band",
                      "options": ["clean", "watch", "suspicious", "likely_manipulated"]},
    # time bucket — handled specially as days-since-created
    "age_days":      {"col": "JULIANDAY('now') - JULIANDAY(s.created_at)",
                      "type": "num", "label": "Age (days since signal)"},
}

_SCREEN_OPS_NUM = {">", ">=", "<", "<=", "=", "!="}
_SCREEN_OPS_CAT = {"=", "!=", "IN"}


@bp.route("/api/screener/fields", methods=["GET"])
def screener_fields():
    """List of whitelisted filter fields the custom screener accepts."""
    return jsonify({
        "success": True,
        "data": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "col"}}
                 for k, v in _SCREEN_FIELDS.items()],
    })


@bp.route("/api/screener/run", methods=["POST"])
def screener_run_custom():
    """Run a user-built screener.
    Body: { "filters": [{field, op, value}], "sort": "alpha_score", "limit": 100 }
    """
    body = request.get_json(force=True, silent=True) or {}
    filters = body.get("filters") or []
    sort = body.get("sort") or "alpha_score"
    sort_dir = "DESC" if (body.get("sort_dir") or "desc").lower() == "desc" else "ASC"
    limit = max(1, min(int(body.get("limit") or 50), 500))

    where = ["s.status = 'active'"]
    params: List = []

    for f in filters:
        fid = f.get("field")
        op = (f.get("op") or "").upper()
        val = f.get("value")
        spec = _SCREEN_FIELDS.get(fid)
        if not spec or val in (None, ""):
            continue
        col = spec["col"]
        if spec["type"] == "num":
            if op not in _SCREEN_OPS_NUM:
                continue
            try:
                fv = float(val)
            except Exception:
                continue
            where.append(f"{col} {op} ?")
            params.append(fv)
        else:  # cat
            if op == "IN" and isinstance(val, list):
                if not val:
                    continue
                placeholders = ",".join(["?"] * len(val))
                where.append(f"{col} IN ({placeholders})")
                params.extend([str(x) for x in val])
            elif op in {"=", "!="}:
                where.append(f"{col} {op} ?")
                params.append(str(val))

    sort_col = (_SCREEN_FIELDS.get(sort) or {}).get("col") or "s.alpha_score"
    sql = f"""
        SELECT s.ticker, s.company, s.alpha_score, s.confidence, s.sentiment,
               s.event_type, s.entry_price, s.magnitude,
               COALESCE(ms.band,'clean') AS forensic_band,
               s.created_at,
               ROUND(JULIANDAY('now') - JULIANDAY(s.created_at), 1) AS age_days
        FROM signals s
        LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
        WHERE {' AND '.join(where)}
        ORDER BY {sort_col} {sort_dir}
        LIMIT {limit}
    """
    try:
        db = _get_db()
        cur = db.conn.cursor()
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"success": True, "count": len(rows), "data": rows,
                        "filters_applied": filters})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "sql": sql}), 400


# ============ FREEMIUM TIER SYSTEM ==========================================
# Single source of truth for what each tier can do.  Read by both frontend
# (pricing page, capability checks) and backend (@require_tier decorator).

TIER_LIMITS = {
    "free": {
        "screener_max_rows":   50,
        "screener_export_csv": False,
        "watchlist_size":      50,
        "alerts_active":       2,
        "stock_detail_full":   False,   # gates Financials/Ratios/Shareholding tabs
        "policy_fast_window":  24,      # hours of policy events visible
        "commodity_macro":     False,
        "global_spillover":    False,
        "fo_unusual":          False,
        "pre_mover_full":      False,   # Free → top-5 preview only
        "pre_mover_preview":   5,
    },
    "pro": {
        "screener_max_rows":   500,
        "screener_export_csv": True,
        "watchlist_size":      1000,
        "alerts_active":       50,
        "stock_detail_full":   True,
        "policy_fast_window":  720,     # 30d
        "commodity_macro":     True,
        "global_spillover":    True,
        "fo_unusual":          True,
        "pre_mover_full":      True,
        "pre_mover_preview":   100,
    },
}


def _ensure_tier_column(db) -> None:
    """Lazy-add `tier` column to users table.  Idempotent."""
    try:
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in cur.fetchall()}
        if "tier" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'free'")
            db.conn.commit()
            logger.info("users.tier column added (defaulting to 'free')")
    except Exception as e:
        logger.warning(f"_ensure_tier_column: {e}")


def _user_tier() -> str:
    """Resolve current user's tier.  Returns 'free' when no auth (legacy)."""
    uid = getattr(g, "user_id", None)
    if not uid:
        return "free"
    try:
        db = _get_db()
        _ensure_tier_column(db)
        cur = db.conn.cursor()
        cur.execute("SELECT tier FROM users WHERE id = ? OR email = ?", (uid, uid))
        row = cur.fetchone()
        if row and row[0] in TIER_LIMITS:
            return row[0]
    except Exception:
        pass
    return "free"


def require_tier(level: str):
    """Decorator: gate an endpoint behind a tier level.  Free is always allowed."""
    from functools import wraps

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            current = _user_tier()
            if level == "free" or current == "pro":
                return fn(*args, **kwargs)
            return jsonify({
                "success": False,
                "error": "tier_required",
                "required_tier": level,
                "current_tier": current,
                "upgrade_url": "/pricing.html",
            }), 402  # Payment Required
        return wrapper
    return deco


@bp.route("/api/tier", methods=["GET"])
def tier_info():
    """Return current user's tier + the limits matrix.  UI uses this for gating."""
    return jsonify({
        "success": True,
        "data": {
            "current_tier": _user_tier(),
            "limits":       TIER_LIMITS,
        },
    })


@bp.route("/api/tier/upgrade", methods=["POST"])
def tier_upgrade_stub():
    """Upgrade endpoint stub.  Real payment integration goes here.
    For now returns a 501 with a clear message — keeps the UI honest."""
    return jsonify({
        "success": False,
        "error":   "payment_provider_not_configured",
        "note":    "wire Razorpay/Stripe here; this endpoint flips users.tier to 'pro' on success",
    }), 501


# ============ OBSERVABILITY METRICS =========================================
# Lightweight in-memory counters.  Cheap, no extra dependency.
# Exposed at /api/metrics in both JSON and Prometheus text format.

_METRICS_LOCK = __import__("threading").Lock()
_METRICS: Dict[str, float] = {}
_METRIC_LABELS: Dict[str, Dict[str, str]] = {}


def _metric_key(name: str, labels: Optional[Dict[str, str]] = None) -> str:
    if not labels:
        return name
    parts = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def metric_inc(name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
    key = _metric_key(name, labels)
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0.0) + value
        if labels:
            _METRIC_LABELS[key] = {"name": name, **labels}
        else:
            _METRIC_LABELS[key] = {"name": name}


def metric_observe(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    """Histogram-style observation: stores last value + running sum + count."""
    key = _metric_key(name, labels)
    with _METRICS_LOCK:
        _METRICS[key + "_sum"]   = _METRICS.get(key + "_sum", 0.0) + value
        _METRICS[key + "_count"] = _METRICS.get(key + "_count", 0.0) + 1
        _METRICS[key + "_last"]  = value


@bp.route("/api/metrics", methods=["GET"])
def metrics_endpoint():
    """JSON metrics dump.  Prometheus scrape format available at ?format=prom."""
    fmt = (request.args.get("format") or "json").lower()
    with _METRICS_LOCK:
        snap = dict(_METRICS)
    if fmt == "prom":
        lines = []
        seen_help = set()
        for k, v in snap.items():
            base = k.split("{")[0]
            if base not in seen_help:
                lines.append(f"# TYPE {base} counter")
                seen_help.add(base)
            lines.append(f"{k} {v}")
        return ("\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4"})
    return jsonify({"success": True, "data": snap})


@bp.before_app_request
def _metric_request_counter():
    """Count every API request.  Cheap; runs in the request thread."""
    try:
        path = request.path or "/"
        if path.startswith("/api/"):
            metric_inc("tickwave_api_requests_total",
                       {"path": path.split("?")[0][:120]})
    except Exception:
        pass


# ============ POLICY FAST-TRACK ============================================
# Surfaces filing-grade policy events from RBI / SEBI / Govt / Min-of-Finance
# / GST sources with the tightest latency the scraper supports.  Tickwave's
# differentiator vs Tickertape/Screener: those platforms don't surface policy
# events as a first-class feed.

_POLICY_SOURCE_PATTERNS = (
    "RBI", "SEBI", "MoF", "Min%Fin", "Min%Finance",
    "GST", "CBDT", "PIB", "NSE", "BSE",
    "RBI Notification", "RBI Press", "SEBI Press", "SEBI Circular",
    "Reserve Bank", "Securities Exchange Board",
)


def _policy_where_clause(table_alias: str = "s") -> str:
    """OR-joined LIKE clauses for the policy source set."""
    return " OR ".join(f"{table_alias}.source LIKE '{p}%'" for p in _POLICY_SOURCE_PATTERNS)


@bp.route("/api/policy/fast", methods=["GET"])
@ttl_cache(seconds=60)
def policy_fast():
    """Recent policy events.  Free tier: 24h window.  Pro tier: 30d window.
    Returns ordered by created_at DESC with alpha + sentiment for ranking."""
    db = _get_db()
    tier = _user_tier()
    hours_cap = TIER_LIMITS[tier]["policy_fast_window"]
    hours = min(int(request.args.get("hours", "24")), hours_cap)
    limit = min(int(request.args.get("limit", "100")), 500)

    metric_inc("tickwave_policy_fast_calls_total", {"tier": tier})
    where = _policy_where_clause("s")
    sql = f"""
        SELECT s.event_id, s.ticker, s.company, s.alpha_score, s.confidence,
               s.sentiment, s.event_type, s.headline, s.source, s.link,
               s.created_at, COALESCE(s.forensic_band, 'clean') AS forensic_band
        FROM signals s
        WHERE ({where})
          AND s.created_at >= datetime('now', ?)
        ORDER BY s.created_at DESC
        LIMIT ?
    """
    try:
        cur = db.conn.cursor()
        cur.execute(sql, (f"-{hours} hours", limit))
        rows = [dict(r) for r in cur.fetchall()]

        # Bucket-by-source for the KPI strip
        by_source: Dict[str, int] = {}
        for r in rows:
            src = (r.get("source") or "").split(":")[0].strip()
            for tag in ("RBI", "SEBI", "GST", "CBDT", "PIB", "NSE", "BSE", "MoF"):
                if tag.lower() in src.lower():
                    by_source[tag] = by_source.get(tag, 0) + 1
                    break

        return jsonify({
            "success":   True,
            "tier":      tier,
            "window_h":  hours,
            "by_source": by_source,
            "count":     len(rows),
            "data":      rows,
        })
    except Exception as e:
        return jsonify({"success": True, "data": [], "warning": str(e)})


# ============ COMMODITY × MACRO FACTORS ====================================
# Cross-tabulates commodity moves against detected macro themes.  Answers:
# "when does crude rally — what macro theme drove it?".  Pro-only.

_COMMODITY_KEYWORDS = {
    "crude":    ["crude", "brent", "wti", "oil price"],
    "gold":     ["gold price", "gold rallies", "gold tumbles", "comex gold"],
    "silver":   ["silver price", "silver rallies"],
    "copper":   ["copper", "lme copper"],
    "natgas":   ["natural gas", "henry hub", "lng"],
    "wheat":    ["wheat", "grain"],
    "sugar":    ["sugar"],
    "rubber":   ["rubber"],
    "aluminum": ["aluminium", "aluminum", "lme alumin"],
}


@bp.route("/api/commodities/macro", methods=["GET"])
@require_tier("pro")
def commodities_macro():
    """For each commodity, count how many active signals reference it grouped
    by macro theme (war, rate, oil, supply).  Output is a heatmap-ready matrix."""
    db = _get_db()
    days = min(int(request.args.get("days", "30")), 365)
    metric_inc("tickwave_commodity_macro_calls_total")

    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT headline, summary, event_type, sentiment, alpha_score,
                      ticker, company, source, created_at
               FROM signals
               WHERE created_at >= datetime('now', ?)
               LIMIT 5000""",
            (f"-{days} days",),
        )
        sigs = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Macro themes detected by simple keyword scan (matches scraper's vocabulary)
    macro_themes = {
        "war":    ["war", "conflict", "sanctions", "embargo", "ukraine", "russia", "israel", "gaza", "middle east"],
        "rate":   ["rate cut", "rate hike", "fomc", "fed decision", "rbi rate", "repo rate", "monetary policy"],
        "supply": ["supply chain", "shortage", "production cut", "opec", "output cut", "logistic"],
        "demand": ["demand surge", "demand drop", "consumption", "festive", "ev demand"],
        "policy": ["budget", "import duty", "export ban", "subsidy", "tax cut"],
    }

    # Matrix: commodity × theme → {count, avg_alpha, sentiment_skew, samples}
    matrix: Dict[str, Dict[str, Dict]] = {c: {t: {"count": 0, "alpha_sum": 0.0, "bull": 0, "bear": 0, "samples": []}
                                              for t in macro_themes}
                                          for c in _COMMODITY_KEYWORDS}

    totals = {c: 0 for c in _COMMODITY_KEYWORDS}
    for s in sigs:
        text = ((s.get("headline") or "") + " " + (s.get("summary") or "")).lower()
        if not text.strip():
            continue
        # Identify which commodity (if any)
        hit_comm = None
        for comm, kws in _COMMODITY_KEYWORDS.items():
            if any(kw in text for kw in kws):
                hit_comm = comm
                break
        if not hit_comm:
            continue
        totals[hit_comm] += 1
        # Identify which macro theme
        for theme, kws in macro_themes.items():
            if any(kw in text for kw in kws):
                cell = matrix[hit_comm][theme]
                cell["count"] += 1
                cell["alpha_sum"] += float(s.get("alpha_score") or 0)
                if s.get("sentiment") == "bullish":
                    cell["bull"] += 1
                elif s.get("sentiment") == "bearish":
                    cell["bear"] += 1
                if len(cell["samples"]) < 3:
                    cell["samples"].append({
                        "headline":  (s.get("headline") or "")[:140],
                        "ticker":    s.get("ticker"),
                        "alpha":     s.get("alpha_score"),
                        "sentiment": s.get("sentiment"),
                        "source":    s.get("source"),
                        "link":      None,
                    })

    # Reduce to a flat list of cells with avg alpha and a tilt score
    out_cells = []
    for comm, themes in matrix.items():
        for theme, cell in themes.items():
            n = cell["count"]
            if n == 0:
                continue
            avg_alpha = cell["alpha_sum"] / n
            tilt = (cell["bull"] - cell["bear"]) / n  # -1..+1
            out_cells.append({
                "commodity":     comm,
                "macro_theme":   theme,
                "count":         n,
                "avg_alpha":     round(avg_alpha, 1),
                "tilt":          round(tilt, 2),
                "bull":          cell["bull"],
                "bear":          cell["bear"],
                "samples":       cell["samples"],
            })

    out_cells.sort(key=lambda x: x["count"], reverse=True)
    return jsonify({
        "success":     True,
        "window_days": days,
        "totals":      totals,
        "themes":      list(macro_themes),
        "commodities": list(_COMMODITY_KEYWORDS),
        "data":        out_cells,
    })


# ============ GLOBAL SPILLOVER / MARKET IMPACT ==============================
# Cross-tab Indian sectors vs global indices.  For each (sector, index) pair,
# computes a co-movement-style score from concurrent signal density.  Pro-only.

_GLOBAL_INDEX_KEYWORDS = {
    "S&P 500":    ["s&p 500", "sp500", "us stocks", "wall street", "dow jones"],
    "NASDAQ":     ["nasdaq", "tech stocks rally", "tech sell-off"],
    "Nikkei":     ["nikkei", "japan stocks"],
    "Hang Seng":  ["hang seng", "hong kong stocks", "hsi"],
    "FTSE":       ["ftse", "uk stocks"],
    "DAX":        ["dax", "germany stocks"],
    "Shanghai":   ["shanghai composite", "china stocks", "sse"],
    "Crude":      ["brent crude", "wti crude", "oil price"],
    "Dollar":     ["dxy", "dollar index", "usd strength", "rupee fall"],
    "Bond yield": ["10-year yield", "us treasury yield", "bond yield"],
}

_INDIAN_SECTORS = (
    "BANK", "IT", "AUTO", "PHARMA", "ENERGY", "FMCG", "METAL", "REALTY",
    "INFRA", "POWER", "TELECOM", "DEFENCE", "CHEMICALS", "RETAIL",
)


@bp.route("/api/global/spillover", methods=["GET"])
@require_tier("pro")
def global_spillover():
    """Sector-by-global-driver matrix from concurrent signal flow.
    For each (Indian sector, global driver) pair, count how often the global
    keyword appears in articles that also touch sector-tagged tickers."""
    db = _get_db()
    days = min(int(request.args.get("days", "14")), 90)
    metric_inc("tickwave_global_spillover_calls_total")

    try:
        from config import STOCK_SECTORS  # ticker → sector
    except Exception:
        STOCK_SECTORS = {}

    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT headline, summary, ticker, sentiment, alpha_score, created_at
               FROM signals
               WHERE created_at >= datetime('now', ?)
               LIMIT 5000""",
            (f"-{days} days",),
        )
        sigs = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    cells: Dict[str, Dict[str, Dict]] = {
        sec: {idx: {"count": 0, "bull": 0, "bear": 0, "alpha_sum": 0.0, "samples": []}
              for idx in _GLOBAL_INDEX_KEYWORDS}
        for sec in _INDIAN_SECTORS
    }

    sector_totals = {s: 0 for s in _INDIAN_SECTORS}
    for s in sigs:
        text = ((s.get("headline") or "") + " " + (s.get("summary") or "")).lower()
        sector = STOCK_SECTORS.get((s.get("ticker") or "").upper())
        if not sector or sector not in cells:
            continue
        sector_totals[sector] += 1
        for idx_name, kws in _GLOBAL_INDEX_KEYWORDS.items():
            if not any(kw in text for kw in kws):
                continue
            c = cells[sector][idx_name]
            c["count"] += 1
            c["alpha_sum"] += float(s.get("alpha_score") or 0)
            if s.get("sentiment") == "bullish":
                c["bull"] += 1
            elif s.get("sentiment") == "bearish":
                c["bear"] += 1
            if len(c["samples"]) < 2:
                c["samples"].append({
                    "headline": (s.get("headline") or "")[:140],
                    "ticker":   s.get("ticker"),
                    "alpha":    s.get("alpha_score"),
                })

    out = []
    for sec, idxs in cells.items():
        for idx_name, c in idxs.items():
            if c["count"] == 0:
                continue
            n = c["count"]
            out.append({
                "sector":    sec,
                "driver":    idx_name,
                "count":     n,
                "avg_alpha": round(c["alpha_sum"] / n, 1),
                "tilt":      round((c["bull"] - c["bear"]) / n, 2),
                "bull":      c["bull"],
                "bear":      c["bear"],
                "samples":   c["samples"],
            })
    out.sort(key=lambda x: x["count"], reverse=True)

    return jsonify({
        "success":        True,
        "window_days":    days,
        "sector_totals":  sector_totals,
        "drivers":        list(_GLOBAL_INDEX_KEYWORDS),
        "sectors":        list(_INDIAN_SECTORS),
        "data":           out,
    })


# ============ PRE-MOVER ENDPOINT ============================================
# Ranks the universe by leading indicators that fire BEFORE a price move.
# Free tier sees a 5-row preview + an upsell flag; Pro sees the full ranking.

@bp.route("/api/premover", methods=["GET"])
def premover_endpoint():
    """Pre-mover candidates ranked by composite leading-indicator score.

    Query params:
      horizon=1D|5D|20D    (default 5D)
      limit=N              (1..100, default 30)
      min_score=F          (0..100, default 30)

    Free-tier behaviour: result truncated to TIER_LIMITS['free']['pre_mover_preview'] (5)
    rows; response carries `tier_gated=True` and `upgrade_url=/pricing.html` so
    the UI can show the upsell banner without a separate call.
    """
    db = _get_db()
    tier = _user_tier()
    horizon = (request.args.get("horizon") or "5D").upper()
    if horizon not in ("1D", "5D", "20D"):
        horizon = "5D"
    limit = max(1, min(int(request.args.get("limit", "30")), 100))
    # Default kept low so the page never goes empty when only the news-catalyst
    # factor is populated (e.g. before bulk-deal / F&O / promoter feeds catch
    # up). Operators can still tighten via ?min_score=...
    try:
        min_score = float(request.args.get("min_score", "8"))
    except Exception:
        min_score = 8.0

    metric_inc("tickwave_premover_calls_total", {"horizon": horizon, "tier": tier})

    # Pull from the scraper's pure scoring module
    try:
        # scraper/ is on sys.path via api.py boot; if not, try a relative import.
        try:
            from premover import compute_premover_scores
        except Exception:
            import sys as _sys, os as _os
            _scr = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "scraper")
            if _scr not in _sys.path:
                _sys.path.insert(0, _scr)
            from premover import compute_premover_scores
    except Exception as e:
        return jsonify({"success": False, "error": f"premover module not loadable: {e}"}), 500

    t0 = time.time()
    try:
        result = compute_premover_scores(
            db,
            horizon=horizon,
            limit=limit,
            min_score=min_score,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"compute failed: {e}"}), 500

    metric_observe("tickwave_premover_latency_seconds", time.time() - t0,
                   {"horizon": horizon})
    metric_observe("tickwave_premover_candidates",
                   float(len(result.get("data") or [])),
                   {"horizon": horizon})

    # Tier-gate: free users see a top-N preview
    full_data = result.get("data") or []
    tier_gated = False
    upgrade_url = None
    if not TIER_LIMITS[tier].get("pre_mover_full", False):
        preview_n = int(TIER_LIMITS[tier].get("pre_mover_preview", 5))
        if len(full_data) > preview_n:
            full_data = full_data[:preview_n]
            tier_gated = True
            upgrade_url = "/pricing.html"
        result["data"] = full_data

    result.update({
        "success":      True,
        "tier":         tier,
        "tier_gated":   tier_gated,
        "upgrade_url":  upgrade_url,
        "count":        len(result.get("data") or []),
    })
    return jsonify(result)


# ============ GEO INTELLIGENCE MAP ===========================================

# Cached at module load — these are config files, not hot data
_GEO_EVERGREEN: Optional[List[Dict]] = None
_TRANSMISSION_RULES: Optional[Dict] = None

# Country → (display_name, lat, lng). Display name is preserved on output.
# Aliases are matched as whole words (regex \b...\b), so short forms like "US"
# only match when standalone, not inside words like "must" or "guess".
_COUNTRIES = [
    # (display, lat, lng, [aliases])
    ("United States",   38.8951,  -77.0369, [r"united states", r"\bUSA?\b", r"\bWashington\b", r"\bWall Street\b"]),
    ("China",           35.8617,  104.1954, [r"\bChina\b", r"Beijing", r"Shanghai", r"Shenzhen", r"\bPBoC\b"]),
    ("Japan",           35.6762,  139.6503, [r"\bJapan\b", r"\bTokyo\b", r"\bBoJ\b", r"\bNikkei\b"]),
    ("Russia",          55.7558,  37.6173,  [r"\bRussia\b", r"\bMoscow\b", r"\bKremlin\b", r"\bPutin\b"]),
    ("Ukraine",         50.4501,  30.5234,  [r"\bUkraine\b", r"\bKyiv\b"]),
    ("Saudi Arabia",    24.7136,  46.6753,  [r"Saudi Arabia", r"\bRiyadh\b", r"\bAramco\b"]),
    ("Iran",            35.6892,  51.3890,  [r"\bIran\b", r"\bTehran\b"]),
    ("Iraq",            33.3152,  44.3661,  [r"\bIraq\b"]),
    ("Israel",          32.0853,  34.7818,  [r"\bIsrael\b", r"\bGaza\b", r"\bTel Aviv\b"]),
    ("UAE",             25.2048,  55.2708,  [r"\bUAE\b", r"\bDubai\b", r"\bAbu Dhabi\b"]),
    ("Qatar",           25.2854,  51.5310,  [r"\bQatar\b", r"\bDoha\b"]),
    ("Germany",         52.5200,  13.4050,  [r"\bGermany\b", r"\bBerlin\b", r"\bFrankfurt\b"]),
    ("France",          48.8566,  2.3522,   [r"\bFrance\b", r"\bParis\b"]),
    ("United Kingdom",  51.5074,  -0.1278,  [r"United Kingdom", r"\bUK\b", r"\bLondon\b", r"\bBritain\b", r"\bBank of England\b"]),
    ("Italy",           41.9028,  12.4964,  [r"\bItaly\b", r"\bRome\b", r"\bMilan\b"]),
    ("Netherlands",     52.3676,  4.9041,   [r"\bNetherlands\b", r"\bAmsterdam\b", r"\bASML\b"]),
    ("Switzerland",     47.5596,  7.5886,   [r"\bSwitzerland\b", r"\bGeneva\b", r"\bZurich\b"]),
    ("Australia",      -25.2744,  133.7751, [r"\bAustralia\b", r"\bSydney\b", r"\bCanberra\b", r"\bPilbara\b"]),
    ("Indonesia",       -2.5489,  118.0149, [r"\bIndonesia\b", r"\bJakarta\b"]),
    ("Singapore",       1.3521,   103.8198, [r"\bSingapore\b"]),
    ("South Korea",     37.5665,  126.9780, [r"South Korea", r"\bSeoul\b", r"\bSamsung\b"]),
    ("Taiwan",          25.0330,  121.5654, [r"\bTaiwan\b", r"\bTaipei\b", r"\bTSMC\b"]),
    ("Vietnam",         14.0583,  108.2772, [r"\bVietnam\b", r"\bHanoi\b"]),
    ("Philippines",     12.8797,  121.7740, [r"\bPhilippines\b", r"\bManila\b"]),
    ("Brazil",         -14.2350, -51.9253,  [r"\bBrazil\b", r"\bSao Paulo\b", r"\bBrasilia\b"]),
    ("Argentina",      -38.4161, -63.6167,  [r"\bArgentina\b", r"\bBuenos Aires\b"]),
    ("Mexico",          23.6345, -102.5528, [r"\bMexico\b"]),
    ("Canada",          56.1304, -106.3468, [r"\bCanada\b", r"\bOttawa\b", r"\bToronto\b"]),
    ("South Africa",   -30.5595,  22.9375,  [r"South Africa", r"\bJohannesburg\b"]),
    ("Nigeria",         9.0820,   8.6753,   [r"\bNigeria\b", r"\bLagos\b"]),
    ("Egypt",           26.8206,  30.8025,  [r"\bEgypt\b", r"\bCairo\b", r"Suez Canal"]),
    ("Turkey",          38.9637,  35.2433,  [r"\bTurkey\b", r"\bAnkara\b", r"\bIstanbul\b"]),
    ("Venezuela",       6.4238,  -66.5897,  [r"\bVenezuela\b"]),
    ("Chile",          -35.6751, -71.5430,  [r"\bChile\b", r"\bSantiago\b"]),
    ("Pakistan",        30.3753,  69.3451,  [r"\bPakistan\b", r"\bIslamabad\b", r"\bKarachi\b"]),
    ("Bangladesh",      23.6850,  90.3563,  [r"\bBangladesh\b", r"\bDhaka\b"]),
    ("Sri Lanka",       7.8731,   80.7718,  [r"Sri Lanka", r"\bColombo\b"]),
    ("Thailand",        15.8700,  100.9925, [r"\bThailand\b", r"\bBangkok\b"]),
    ("Malaysia",        4.2105,   101.9758, [r"\bMalaysia\b", r"Kuala Lumpur"]),
    # India is special-cased — we want events that are *about India*, not US news
    # mentioning Indian companies. So treat India as last-resort + require strong signals.
    ("India",           20.5937,  78.9629,  [r"\bRBI\b", r"\bSEBI\b", r"\bMumbai\b", r"\bNew Delhi\b", r"\bDalal Street\b", r"\bNifty\b"]),
]

# Indian states for the India-zoomed map
_INDIA_STATES = {
    "maharashtra":      (19.7515, 75.7139),
    "mumbai":           (19.0760, 72.8777),
    "delhi":            (28.7041, 77.1025),
    "karnataka":        (15.3173, 75.7139),
    "bengaluru":        (12.9716, 77.5946),
    "bangalore":        (12.9716, 77.5946),
    "tamil nadu":       (11.1271, 78.6569),
    "chennai":          (13.0827, 80.2707),
    "telangana":        (18.1124, 79.0193),
    "hyderabad":        (17.3850, 78.4867),
    "andhra pradesh":   (15.9129, 79.7400),
    "kerala":           (10.8505, 76.2711),
    "gujarat":          (22.2587, 71.1924),
    "ahmedabad":        (23.0225, 72.5714),
    "rajasthan":        (27.0238, 74.2179),
    "jaipur":           (26.9124, 75.7873),
    "punjab":           (31.1471, 75.3412),
    "haryana":          (29.0588, 76.0856),
    "gurugram":         (28.4595, 77.0266),
    "uttar pradesh":    (26.8467, 80.9462),
    "lucknow":          (26.8467, 80.9462),
    "noida":            (28.5355, 77.3910),
    "madhya pradesh":   (22.9734, 78.6569),
    "bhopal":           (23.2599, 77.4126),
    "west bengal":      (22.9868, 87.8550),
    "kolkata":          (22.5726, 88.3639),
    "bihar":            (25.0961, 85.3131),
    "patna":            (25.5941, 85.1376),
    "odisha":           (20.9517, 85.0985),
    "bhubaneswar":      (20.2961, 85.8245),
    "assam":            (26.2006, 92.9376),
    "guwahati":         (26.1445, 91.7362),
    "jharkhand":        (23.6102, 85.2799),
    "chhattisgarh":     (21.2787, 81.8661),
    "uttarakhand":      (30.0668, 79.0193),
    "himachal pradesh": (31.1048, 77.1734),
    "goa":              (15.2993, 74.1240),
    "jammu and kashmir":(33.7782, 76.5762),
    "kashmir":          (33.7782, 76.5762),
    "ladakh":           (34.1526, 77.5770),
    "manipur":          (24.6637, 93.9063),
    "tripura":          (23.9408, 91.9882),
    "meghalaya":        (25.4670, 91.3662),
    "nagaland":         (26.1584, 94.5624),
    "mizoram":          (23.1645, 92.9376),
    "arunachal pradesh":(28.2180, 94.7278),
    "sikkim":           (27.5330, 88.5122),
}


def _load_geo_configs():
    """Load JSON config files once (module-cached)."""
    global _GEO_EVERGREEN, _TRANSMISSION_RULES
    if _GEO_EVERGREEN is None:
        try:
            scraper_dir = os.path.join(os.path.dirname(__file__), "..", "scraper")
            with open(os.path.join(scraper_dir, "geo_evergreen.json"), "r", encoding="utf-8") as f:
                _GEO_EVERGREEN = json.load(f)
        except Exception as e:
            logger.warning(f"geo_evergreen.json load failed: {e}")
            _GEO_EVERGREEN = []
    if _TRANSMISSION_RULES is None:
        try:
            scraper_dir = os.path.join(os.path.dirname(__file__), "..", "scraper")
            with open(os.path.join(scraper_dir, "transmission_rules.json"), "r", encoding="utf-8") as f:
                _TRANSMISSION_RULES = json.load(f)
        except Exception as e:
            logger.warning(f"transmission_rules.json load failed: {e}")
            _TRANSMISSION_RULES = {"lenses": {}}
    return _GEO_EVERGREEN, _TRANSMISSION_RULES


import re

# Compile country alias regexes once (case-insensitive, word-boundary aware)
_COUNTRY_PATTERNS = [
    (display, lat, lng, [re.compile(a, re.IGNORECASE) for a in aliases])
    for display, lat, lng, aliases in _COUNTRIES
]


def _classify_lens(text: str, rules: Dict) -> Optional[str]:
    """Match an event against keyword lists with whole-word matching.
    Each lens needs at least 1 hit and beats the previous best by hit count.
    Short keywords (<4 chars) are required to be standalone words."""
    if not text:
        return None
    t = text.lower()
    best_lens = None
    best_hits = 0
    for lens_id, lens_cfg in (rules.get("lenses") or {}).items():
        hits = 0
        for kw in lens_cfg.get("keywords", []):
            if not kw:
                continue
            kw_low = kw.lower()
            # Word-boundary match for short tokens; substring OK for multi-word phrases
            if len(kw_low.split()) > 1:
                if kw_low in t:
                    hits += 1
            else:
                # \b...\b on lowercase text
                if re.search(r"\b" + re.escape(kw_low) + r"\b", t):
                    hits += 1
        if hits > best_hits:
            best_hits = hits
            best_lens = lens_id
    return best_lens


def _geocode_country(text: str) -> Optional[tuple]:
    """Find first matching country (by aliases, word-boundary). Preserves
    original display casing. Returns (display_name, lat, lng) or None."""
    if not text:
        return None
    for display, lat, lng, patterns in _COUNTRY_PATTERNS:
        for p in patterns:
            if p.search(text):
                return (display, lat, lng)
    return None


def _geocode_india_state(text: str) -> Optional[tuple]:
    """Find first matching Indian state/city, return (state, lat, lng)."""
    if not text:
        return None
    t = text.lower()
    # Sort longer keys first to prefer "uttar pradesh" over "pradesh"
    for key in sorted(_INDIA_STATES.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", t):
            lat, lng = _INDIA_STATES[key]
            return (key.title(), lat, lng)
    return None


@bp.route("/api/geo/events", methods=["GET"])
def geo_events():
    """Merged geo events: curated evergreen pins + live DB events tagged with country.

    Query params:
    - lens: filter to a single lens (energy/metals/agri/geopolitics/central_banks/trade/tech)
    - hours: lookback for live events (default 72)
    """
    evergreen, rules = _load_geo_configs()
    db = _get_db()
    lens_filter = request.args.get("lens")
    hours = min(int(request.args.get("hours", 168)), 720)

    out = []

    # 1) Live geo-tagged events from DB
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT event_id, title, summary, source, link, sentiment, magnitude, impact_score, event_type, companies, published_at, created_at
               FROM events
               WHERE created_at >= datetime('now', ?)
               ORDER BY created_at DESC LIMIT 400""",
            (f"-{hours} hours",))
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"geo_events DB read failed: {e}")
        rows = []

    for r in rows:
        text = ((r.get("title") or "") + " " + (r.get("summary") or ""))
        geo = _geocode_country(text)
        if not geo:
            continue
        country, lat, lng = geo
        lens = _classify_lens(text, rules) or "geopolitics"
        if lens_filter and lens != lens_filter:
            continue

        # Severity: prefer impact_score (0-100), fallback to magnitude (0-10)
        severity = float(r.get("impact_score") or 0)
        if severity == 0 and r.get("magnitude"):
            severity = float(r["magnitude"]) * 10
        severity = max(1, min(10, round(severity / 10)))

        # Compute age
        created = r.get("created_at") or r.get("published_at")
        age_h = None
        try:
            from datetime import datetime as _dt
            if isinstance(created, str):
                # SQLite returns "YYYY-MM-DD HH:MM:SS"
                ct = _dt.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
                age_h = (_dt.utcnow() - ct).total_seconds() / 3600.0
        except Exception:
            age_h = None

        out.append({
            "event_id":   r["event_id"],
            "kind":       "live",
            "country":    country,
            "lat": lat, "lng": lng,
            "lens":       lens,
            "severity":   severity,
            "title":      (r.get("title") or "")[:200],
            "summary":    (r.get("summary") or "")[:400],
            "source":     r.get("source"),
            "link":       r.get("link"),
            "sentiment":  r.get("sentiment"),
            "companies":  r.get("companies"),
            "age_hours":  round(age_h, 2) if age_h is not None else None,
            "is_new":     bool(age_h is not None and age_h < 2),
            "as_of":      str(created) if created else None,
        })

    # 2) Evergreen curated pins (always shown unless lens-filtered)
    for ev in (evergreen or []):
        if lens_filter and ev.get("lens") != lens_filter:
            continue
        out.append({
            "event_id":   ev["id"],
            "kind":       "evergreen",
            "country":    ev.get("country"),
            "lat":        ev.get("lat"),
            "lng":        ev.get("lng"),
            "lens":       ev.get("lens"),
            "severity":   ev.get("severity", 7),
            "title":      ev.get("title"),
            "summary":    ev.get("summary"),
            "source":     "Tickwave Evergreen",
            "link":       None,
            "is_new":     False,
            "age_hours":  None,
        })

    # Sort: new live first (by age asc), then by severity desc
    def _sort_key(e):
        age = e.get("age_hours")
        is_new = 0 if e.get("is_new") else 1
        return (is_new, age if age is not None else 99999, -float(e.get("severity") or 0))
    out.sort(key=_sort_key)

    # Lens metadata for the frontend (colors, labels, monograms)
    lens_meta = {
        lid: {"label": l.get("label"), "color": l.get("color"), "monogram": l.get("monogram")}
        for lid, l in (rules.get("lenses") or {}).items()
    }

    return jsonify({
        "success": True,
        "data": out,
        "count": len(out),
        "lenses": lens_meta,
    })


@bp.route("/api/geo/transmission/<event_id>", methods=["GET"])
def geo_transmission(event_id: str):
    """Given an event_id (live or evergreen), return the transmission chain:
    affected Indian tickers ranked by historical impact, with the latest
    signal data (alpha, sentiment, last entry price) where available."""
    evergreen, rules = _load_geo_configs()
    db = _get_db()
    cur = db.conn.cursor()

    # Resolve the event: evergreen pins start with "evg-"; live events live in DB
    event_obj = None
    if event_id.startswith("evg-"):
        for ev in (evergreen or []):
            if ev.get("id") == event_id:
                event_obj = dict(ev)
                event_obj["kind"] = "evergreen"
                break
    else:
        try:
            cur.execute(
                """SELECT event_id, title, summary, source, link, sentiment, magnitude, impact_score, companies, created_at
                   FROM events WHERE event_id = ? LIMIT 1""", (event_id,))
            r = cur.fetchone()
            if r:
                event_obj = dict(r)
                event_obj["kind"] = "live"
        except Exception as e:
            logger.warning(f"transmission DB read: {e}")

    if not event_obj:
        return jsonify({"success": False, "error": "event not found", "data": []}), 404

    # Determine lens
    text = (event_obj.get("title") or "") + " " + (event_obj.get("summary") or "")
    lens = event_obj.get("lens") or _classify_lens(text, rules) or "geopolitics"
    lens_cfg = (rules.get("lenses") or {}).get(lens) or {}

    # Build affected ticker list with live signal data
    tickers_cfg = lens_cfg.get("tickers", [])
    affected = []
    for tcfg in tickers_cfg:
        tk = tcfg["ticker"]
        signal = None
        try:
            cur.execute(
                """SELECT ticker, company, alpha_score, confidence, entry_price, sentiment, magnitude, impact_score, headline, created_at
                   FROM signals WHERE ticker = ? ORDER BY created_at DESC LIMIT 1""", (tk,))
            srow = cur.fetchone()
            if srow:
                signal = dict(srow)
        except Exception:
            signal = None

        affected.append({
            "ticker":      tk,
            "company":     (signal or {}).get("company"),
            "rank":        tcfg.get("rank"),
            "reason":      tcfg.get("reason"),
            "beta":        tcfg.get("beta"),
            "alpha_score": (signal or {}).get("alpha_score"),
            "confidence":  (signal or {}).get("confidence"),
            "last_price":  (signal or {}).get("entry_price"),
            "sentiment":   (signal or {}).get("sentiment"),
            "signal_age":  (signal or {}).get("created_at"),
            "headline":    (signal or {}).get("headline"),
        })

    return jsonify({
        "success": True,
        "data": {
            "event": {
                "event_id":  event_obj.get("event_id") or event_obj.get("id"),
                "title":     event_obj.get("title"),
                "summary":   event_obj.get("summary"),
                "source":    event_obj.get("source"),
                "link":      event_obj.get("link"),
                "country":   event_obj.get("country"),
                "lens":      lens,
                "lens_label": lens_cfg.get("label"),
                "lens_color": lens_cfg.get("color"),
                "kind":      event_obj.get("kind"),
                "as_of":     str(event_obj.get("created_at") or ""),
            },
            "affected": affected,
            "count":    len(affected),
        },
    })


# ============ REACTION STATUS ===============================================
# "Has this ticker already moved on today's news, or is there room left?"
# Pulls live %change from yfinance (90s cache), classifies vs event sentiment.

_REACTION_CACHE: Dict[str, tuple] = {}   # ticker -> (last, prev_close, fetched_at)
_REACTION_TTL_S = 90


def _fetch_today_change(tickers: List[str]) -> Dict[str, Dict]:
    """Batch-fetch last + prev close from yfinance, computing today's %change.
    In-memory cached for 90s per ticker so back-to-back calls don't hammer yf.
    """
    if not tickers:
        return {}
    now = time.time()
    out: Dict[str, Dict] = {}
    needs_fetch: List[str] = []
    for t in tickers:
        cached = _REACTION_CACHE.get(t)
        if cached and (now - cached[2]) < _REACTION_TTL_S:
            last, prev, _ = cached
            out[t] = {"last": last, "prev_close": prev}
        else:
            needs_fetch.append(t)

    if needs_fetch:
        try:
            import yfinance as yf
            yf_syms = [f"{tk}.NS" for tk in needs_fetch]
            data = yf.download(yf_syms, period="2d", interval="1d",
                               group_by="ticker", progress=False, threads=True,
                               auto_adjust=False)
            for tk in needs_fetch:
                yf_sym = f"{tk}.NS"
                try:
                    if len(yf_syms) == 1:
                        closes = data["Close"].dropna()
                    else:
                        closes = data[yf_sym]["Close"].dropna()
                    if len(closes) < 1:
                        out[tk] = {"last": None, "prev_close": None}
                        continue
                    last = float(closes.iloc[-1])
                    prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                    _REACTION_CACHE[tk] = (last, prev, now)
                    out[tk] = {"last": last, "prev_close": prev}
                except Exception:
                    out[tk] = {"last": None, "prev_close": None}
        except Exception as e:
            logger.warning(f"reactions yf fetch: {e}")
            for tk in needs_fetch:
                out.setdefault(tk, {"last": None, "prev_close": None})
    return out


def _classify_reaction(chg_pct: Optional[float], sentiment: Optional[str], magnitude: float) -> Dict:
    """Map (today_change, expected_sentiment, event_severity) → status badge."""
    if chg_pct is None:
        return {"status": "no_data", "label": "—", "color": "#5a6373",
                "tooltip": "Live price unavailable.", "direction_match": None,
                "headroom_pct": None}

    # Magnitude is 0-10; scale thresholds. Default mid (5) → 1.0x.
    mag = max(1.0, min(10.0, float(magnitude or 5)))
    scale = max(0.6, mag / 5.0)
    T_PRICED  = 2.0 * scale
    T_PARTIAL = 0.5 * scale
    T_FLAT    = 0.3

    sent = (sentiment or "").lower()
    expected_sign = +1 if sent == "bullish" else -1 if sent == "bearish" else 0

    # Neutral / unknown sentiment — only flag big abs moves
    if expected_sign == 0:
        if abs(chg_pct) >= T_PRICED:
            return {"status": "volatile", "label": f"Volatile {chg_pct:+.1f}%",
                    "color": "#a78bfa", "direction_match": None,
                    "headroom_pct": None,
                    "tooltip": f"Stock moved {chg_pct:+.2f}% on a neutral-sentiment event."}
        return {"status": "neutral", "label": f"{chg_pct:+.1f}%", "color": "#8a94a8",
                "direction_match": None, "headroom_pct": None,
                "tooltip": f"Today {chg_pct:+.2f}%, neutral sentiment."}

    # Did the stock move in the expected direction?
    aligned_pct = chg_pct * expected_sign      # positive if matching expectation
    direction_match = aligned_pct > 0

    if aligned_pct >= T_PRICED:
        return {"status": "priced_in", "label": "Priced in",
                "color": "#8a94a8", "direction_match": True,
                "headroom_pct": 0.0,
                "tooltip": f"Already {chg_pct:+.2f}% today — expected {sent} move largely captured."}
    if aligned_pct >= T_PARTIAL:
        headroom = max(0.0, T_PRICED - aligned_pct)
        return {"status": "partial", "label": "Partial move",
                "color": "#e6b84a", "direction_match": True,
                "headroom_pct": round(headroom, 2),
                "tooltip": f"Moved {chg_pct:+.2f}% so far; ~{headroom:.1f}% headroom left in expected direction."}
    if aligned_pct >= -T_FLAT:
        return {"status": "open", "label": "Room to spike",
                "color": "#2dd4aa", "direction_match": True,
                "headroom_pct": round(T_PRICED, 2),
                "tooltip": f"Barely moved ({chg_pct:+.2f}%) — alpha potential intact."}
    return {"status": "counter", "label": "Diverging",
            "color": "#f29090", "direction_match": False,
            "headroom_pct": None,
            "tooltip": f"Stock moved {chg_pct:+.2f}% — opposite to expected {sent} direction."}


@bp.route("/api/reactions", methods=["GET"])
def reactions():
    """Per-ticker reaction status. Tells you whether the stock has already
    spiked on today's event or if there's still room to run.

    Query params:
    - tickers: comma-separated (max 25)
    - sentiment: bullish | bearish | neutral (used to set expected direction)
    - magnitude: 0-10 (severity; scales the move-size thresholds)
    """
    raw = (request.args.get("tickers") or "").strip()
    if not raw:
        return jsonify({"success": False, "error": "tickers required", "data": {}}), 400
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()][:25]
    sentiment = request.args.get("sentiment")
    try:
        magnitude = float(request.args.get("magnitude") or 5)
    except Exception:
        magnitude = 5.0

    quotes = _fetch_today_change(tickers)
    out: Dict[str, Dict] = {}
    for tk in tickers:
        q = quotes.get(tk) or {}
        last, prev = q.get("last"), q.get("prev_close")
        chg_pct = ((last - prev) / prev * 100.0) if (last and prev) else None
        cls = _classify_reaction(chg_pct, sentiment, magnitude)
        out[tk] = {
            "ticker":     tk,
            "last":       last,
            "prev_close": prev,
            "change_pct": round(chg_pct, 2) if chg_pct is not None else None,
            **cls,
        }
    return jsonify({"success": True, "data": out, "count": len(out)})


# ============ DAILY PICKS (Groq-reasoned) ===================================
# Top alpha signals for "today" — diverse by sector, cached per UTC day so the
# picks are stable through the trading session. Each pick gets a 1-line Groq
# reasoning (cached too) explaining WHY it made the cut.

_PICKS_CACHE: Dict[str, Dict] = {}   # day -> {data, generated_at}
_REASONING_CACHE: Dict[str, str] = {}  # day:ticker -> reasoning text


def _groq_pick_reason(pick: Dict) -> Optional[str]:
    """Generate a 1-sentence reason this stock made today's picks. Returns
    None if Groq unavailable / governor refuses; caller falls back."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        import sys as _sys
        scraper_path = os.path.join(os.path.dirname(__file__), "..", "scraper")
        if scraper_path not in _sys.path:
            _sys.path.insert(0, scraper_path)
        from groq_governor import governor as _gov  # type: ignore
        if not _gov.can_spend("reasoning", est_tokens=200):
            return None
    except Exception:
        pass
    prompt = (
        f"Stock: {pick.get('ticker')} ({pick.get('company','')})\n"
        f"Event: {pick.get('event_type','news')} · sentiment {pick.get('sentiment','neutral')}\n"
        f"Headline: {(pick.get('headline') or '')[:200]}\n"
        f"Alpha score: {pick.get('alpha_score',0):.0f}/100\n\n"
        "In ONE sentence (max 25 words), explain WHY this stock is on today's "
        "high-conviction list. Be concrete, cite the catalyst, no hedging."
    )
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=_json.dumps({
                "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                "messages": [
                    {"role": "system", "content": "You write tight, concrete trade reasoning for Indian equities. One sentence."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3, "max_tokens": 60,
            }).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if msg:
            try:
                tk = (data.get("usage") or {}).get("total_tokens", 0)
                _gov.record_spend("reasoning", actual_tokens=int(tk))
            except Exception:
                pass
            return msg.strip().strip('"')
    except Exception as e:
        logger.info(f"groq pick reason failed: {e}")
    return None


def _fallback_pick_reason(pick: Dict) -> str:
    """Hand-crafted reason when Groq is unavailable."""
    et = (pick.get("event_type") or "news").replace("_", " ")
    sent = pick.get("sentiment") or "neutral"
    alpha = pick.get("alpha_score") or 0
    sent_word = {"bullish": "tailwind", "bearish": "headwind", "neutral": "watch"}.get(sent, "watch")
    return f"Alpha {alpha:.0f}/100 · {et} {sent_word} surfaced today — confirm before position sizing."


# ── Subject-match helper for daily picks ─────────────────────────────────────
# Drops "article says X, suggest Y" false positives — same logic as the
# client-side CuratedSignals.headlineMatchesTicker check.
_PICKS_SHORT_NAMES = {
    "TCS":        ["TCS", "Tata Consultancy"],
    "RELIANCE":   ["RIL", "Reliance Industries", "Reliance Ind"],
    "INFY":       ["INFY", "Infosys"],
    "HDFCBANK":   ["HDFC Bank"],
    "ICICIBANK":  ["ICICI Bank", "ICICI"],
    "HINDUNILVR": ["HUL", "Hindustan Unilever", "Hind Unilever"],
    "SBIN":       ["SBI", "State Bank of India"],
    "BHARTIARTL": ["Bharti Airtel", "Airtel"],
    "KOTAKBANK":  ["Kotak Mahindra", "Kotak Bank"],
    "LT":         ["Larsen", "L&T", "L&amp;T"],
    "ITC":        ["ITC Ltd", "ITC "],
    "AXISBANK":   ["Axis Bank"],
    "BAJFINANCE": ["Bajaj Finance"],
    "BAJAJ-AUTO": ["Bajaj Auto"],
    "BAJAJFINSV": ["Bajaj Finserv"],
    "MARUTI":     ["Maruti Suzuki", "Maruti"],
    "NESTLEIND":  ["Nestle India", "Nestle"],
    "ASIANPAINT": ["Asian Paints"],
    "WIPRO":      ["Wipro"],
    "TECHM":      ["Tech Mahindra", "TechM"],
    "HCLTECH":    ["HCL Technologies", "HCL Tech"],
    "SUNPHARMA":  ["Sun Pharma"],
    "TATACONSUM": ["Tata Consumer"],
    "TATASTEEL":  ["Tata Steel"],
    "TATAMOTORS": ["Tata Motors"],
    "COALINDIA":  ["Coal India"],
    "POWERGRID":  ["Power Grid"],
    "ADANIENT":   ["Adani Enterprises", "Adani Ent"],
    "ADANIPORTS": ["Adani Ports"],
    "TITAN":      ["Titan Company", "Titan "],
    "HEROMOTOCO": ["Hero MotoCorp", "Hero Moto"],
    "DRREDDY":    ["Dr Reddy", "Dr. Reddy"],
    "TATAPOWER":  ["Tata Power"],
    "EICHERMOT":  ["Eicher Motors"],
    "M&M":        ["Mahindra & Mahindra", "M&M", "Mahindra "],
    "ULTRACEMCO": ["UltraTech Cement", "UltraTech"],
    "JSWSTEEL":   ["JSW Steel"],
    "HINDALCO":   ["Hindalco"],
    "BSE":        ["BSE Ltd", "BSE Limited", "Bombay Stock Exchange", "BSE shares", "BSE stock"],
    "CLEAN":      ["Clean Science", "Clean Sci"],
}

def _picks_headline_matches(headline: str, ticker: str, summary: str = "") -> bool:
    """Check whether the article's headline OR summary names the ticker."""
    if not ticker:
        return False
    import re as _re
    tk = ticker.upper().strip()
    text = ((headline or "") + " " + (summary or "")).lower()
    if not text.strip():
        return False
    names = list(_PICKS_SHORT_NAMES.get(tk, [])) + [tk]
    for n in names:
        if _re.search(r"\b" + _re.escape(n.lower()) + r"\b", text):
            return True
    return False

# Broker / research-firm tickers — when the article uses them in analyst
# posture, it's commentary on OTHER stocks, not news about the broker.
_BROKER_TICKERS = {
    'NUVAMA', 'MOTILALOFS', 'ANGELONE', 'IIFL', 'ANANDRATHI', 'ICICIPRULI',
    'BSE', 'CDSL', 'CAMS', 'MCX',
}
_ANALYST_VERBS = (
    r"\s+(?:warns?|says?|sees?|rates?|recommends?|advises?|targets?|cuts?|"
    r"raises?|maintains?|initiates?|forecasts?|projects?|expects?|prefers?|"
    r"picks?|pick|view|outlook|note|on|upgrades?|downgrades?|reiterates?)\b"
)

def _picks_is_broker_commentary(headline: str, ticker: str, summary: str = "") -> bool:
    if not ticker:
        return False
    import re as _re
    tk = ticker.upper().strip()
    if tk not in _BROKER_TICKERS:
        return False
    text = ((headline or "") + " " + (summary or "")).lower()
    if not text.strip():
        return False
    names = [n.lower() for n in _PICKS_SHORT_NAMES.get(tk, [tk])]
    if not names:
        names = [tk.lower()]
    for n in names:
        ne = _re.escape(n)
        if _re.search(r"\b" + ne + r"\s*:", text):                 return True
        if _re.search(r"\b" + ne + _ANALYST_VERBS,  text):         return True
        if _re.search(r"\b" + ne + r"'s", text):                   return True
        if _re.search(r"\b" + ne + r"\s+(?:report|note|research|analyst|brokerage)\b", text):
            return True
    return False


@bp.route("/api/picks/daily", methods=["GET"])
def daily_picks():
    """5-7 high-conviction picks for today's session, with 1-line reasoning each.
    Stable per UTC day (cached); reasoning is Groq-generated when budget allows.
    """
    db = _get_db()
    cur = db.conn.cursor()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Day-cached picks: regenerate if cache stale or empty
    cached = _PICKS_CACHE.get(today)
    if cached and (time.time() - cached.get("generated_at", 0)) < 3600:  # 1h soft TTL
        return jsonify({"success": True, **cached, "as_of": today})

    # Pull top alpha signals from last 24h, dedupe by ticker.
    # Signal event_id has shape "TICKER_TYPE_HASH"; events.event_id is just
    # "HASH". Substring-match on the trailing hash so the JOIN actually hits.
    try:
        cur.execute("""
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment, s.event_type,
                   s.headline, s.entry_price, s.confidence, s.created_at,
                   e.summary AS article_summary
            FROM signals s
            LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
            LEFT JOIN events e
              ON e.event_id = substr(s.event_id, length(s.event_id) - 15)
            WHERE s.ticker IS NOT NULL AND s.ticker != ''
              AND s.alpha_score >= 50
              AND s.created_at >= datetime('now', '-24 hours')
              AND s.status = 'active'
              AND COALESCE(ms.score, 0) < 7
            ORDER BY s.alpha_score DESC LIMIT 60
        """)
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"daily_picks query: {e}")
        rows = []

    # Subject-match filter: the headline OR summary must actually name this
    # ticker. Drops misattribution false-positives.
    seen_tickers = set()
    picks = []
    for r in rows:
        if r["ticker"] in seen_tickers:
            continue
        summary = r.get("article_summary") or ""
        if not _picks_headline_matches(r.get("headline") or "", r["ticker"], summary):
            continue
        if _picks_is_broker_commentary(r.get("headline") or "", r["ticker"], summary):
            continue   # Nuvama writing about midcaps ≠ alpha for Nuvama
        seen_tickers.add(r["ticker"])
        picks.append(r)
        if len(picks) >= 6:
            break

    # Attach Groq-generated reasoning (cached per day:ticker so we only spend
    # tokens once per pick per day)
    for p in picks:
        cache_key = f"{today}:{p['ticker']}"
        reason = _REASONING_CACHE.get(cache_key)
        if not reason:
            reason = _groq_pick_reason(p) or _fallback_pick_reason(p)
            _REASONING_CACHE[cache_key] = reason
        p["reasoning"] = reason
        # Drop heavy fields the frontend doesn't need
        p.pop("entry_price", None)
        p.pop("confidence", None)

    payload = {"data": picks, "count": len(picks), "generated_at": time.time()}
    _PICKS_CACHE[today] = payload
    return jsonify({"success": True, **payload, "as_of": today})


# ============ GROQ USAGE STATUS =============================================

@bp.route("/api/groq/status", methods=["GET"])
def groq_status():
    """Live Groq token-budget status for the dashboard chip."""
    try:
        import sys as _sys
        scraper_path = os.path.join(os.path.dirname(__file__), "..", "scraper")
        if scraper_path not in _sys.path:
            _sys.path.insert(0, scraper_path)
        from groq_governor import governor as _gov  # type: ignore
        snap = _gov.snapshot() if hasattr(_gov, "snapshot") else None
        if snap is None:
            # Fallback: pull state file directly
            state_path = os.path.join(os.path.dirname(__file__), "..", "data", "groq_state.json")
            with open(state_path, "r") as f:
                snap = json.load(f)
        spent = sum((snap.get("spent") or {}).values())
        tpd = int(os.getenv("GROQ_TPD", "100000"))
        return jsonify({
            "success":     True,
            "tpd":         tpd,
            "spent_today": spent,
            "remaining":   max(0, tpd - spent),
            "pct_used":    round(spent / tpd * 100, 1) if tpd else 0,
            "by_bucket":   snap.get("spent") or {},
            "cb_open":     bool(snap.get("cb_until", 0) > time.time()),
            "day":         snap.get("day"),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============ IPOs / CORPORATE ACTIONS ======================================

@bp.route("/api/ipos", methods=["GET"])
def get_ipos():
    """Fetch upcoming IPO listings."""
    db = _get_db()
    limit = min(int(request.args.get("limit", 6)), 50)

    try:
        cur = db.conn.cursor()
        query = """
            SELECT id, title, companies, summary, impact_score, published_at
            FROM events
            WHERE event_type = 'ipo'
            ORDER BY published_at DESC
            LIMIT ?
        """
        cur.execute(query, [limit])
        rows = cur.fetchall()

        ipos = []
        for r in rows:
            ipos.append({
                "id": r[0],
                "company": r[1] or r[2],  # Use title or companies field
                "sector": "Finance",  # Default sector, could be extracted from summary
                "listing_date": str(r[5]) if r[5] else None,
                "price_band_low": None,  # Would need to parse from summary if available
                "price_band_high": None,
                "issue_size": "TBD",
                "impact_score": float(r[4]) if r[4] else 50
            })

        return jsonify({
            "success": True,
            "data": ipos,
            "count": len(ipos)
        })
    except Exception as e:
        logger.warning(f"IPOs fetch error: {e}")
        return jsonify({"success": False, "error": str(e), "data": []}), 500


@bp.route("/api/corporate-actions", methods=["GET"])
def get_corporate_actions():
    """Fetch corporate actions (buybacks, dividends, splits, etc.)."""
    db = _get_db()
    limit = min(int(request.args.get("limit", 10)), 50)

    try:
        cur = db.conn.cursor()
        query = """
            SELECT id, title, companies, event_type, summary, impact_score, published_at
            FROM events
            WHERE event_type IN ('buyback', 'dividend', 'split', 'bonus', 'rights')
            ORDER BY published_at DESC
            LIMIT ?
        """
        cur.execute(query, [limit])
        rows = cur.fetchall()

        actions = []
        for r in rows:
            actions.append({
                "id": r[0],
                "title": r[1],
                "companies": r[2],
                "event_type": r[3],
                "summary": r[4],
                "impact_score": float(r[5]) if r[5] else 50,
                "published_at": str(r[6]) if r[6] else None
            })

        return jsonify({
            "success": True,
            "data": actions,
            "count": len(actions)
        })
    except Exception as e:
        logger.warning(f"Corporate actions fetch error: {e}")
        return jsonify({"success": False, "error": str(e), "data": []}), 500


# ============ REGISTRATION ==================================================

def register(app, get_db: Callable):
    global _get_db
    _get_db = get_db
    if "api_v3" not in app.blueprints:
        app.register_blueprint(bp)
