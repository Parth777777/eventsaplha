"""API v3: forensics, F&O, bulk-deals, screeners, paper-trading, telemetry,
audit-trail, onboarding. Mounts onto the same Flask app as api_v2.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

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
        "name": "Smallcaps with promoter pledge ↑",
        "description": "Promoter pledge increased ≥3pp in last 90d (any cap)",
        "sql": """
            SELECT ph.ticker, ph.ticker AS company,
                   MAX(ph.pledge_pct) - MIN(ph.pledge_pct) AS pledge_delta,
                   MAX(ph.pledge_pct) AS current_pledge,
                   MAX(ph.as_of_date) AS last_update
            FROM promoter_holdings ph
            WHERE ph.as_of_date >= date('now', '-90 days')
            GROUP BY ph.ticker
            HAVING pledge_delta >= 3
            ORDER BY pledge_delta DESC LIMIT 50
        """,
    },
    "volume_spike_no_news": {
        "name": "Volume spike + no news",
        "description": "Tickers with recent volume z-score > 2 and no Tier-1 news",
        "sql": """
            SELECT ticker, MAX(z_score) AS z, MAX(created_at) AS last_seen
            FROM volume_anomalies
            WHERE created_at >= datetime('now', '-3 days') AND z_score >= 2.0
            GROUP BY ticker ORDER BY z DESC LIMIT 50
        """,
    },
    "earnings_within_5d_high_alpha": {
        "name": "Pre-earnings high-alpha",
        "description": "Earnings within 5 days AND active alpha ≥ 60",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.sentiment,
                   e.earnings_date
            FROM signals s
            JOIN earnings_calendar e ON e.ticker = s.ticker
            WHERE s.alpha_score >= 60
              AND e.earnings_date BETWEEN date('now') AND date('now', '+5 days')
            ORDER BY e.earnings_date ASC, s.alpha_score DESC LIMIT 50
        """,
    },
    "manipulation_band": {
        "name": "Likely manipulated (avoid)",
        "description": "Forensic band = likely_manipulated OR active manipulation flag",
        "sql": """
            SELECT DISTINCT ticker, MAX(as_of) AS last_seen, GROUP_CONCAT(detector) AS detectors
            FROM manipulation_flags
            WHERE band IN ('likely_manipulated', 'suspicious')
              AND as_of >= datetime('now', '-30 days')
            GROUP BY ticker ORDER BY last_seen DESC LIMIT 50
        """,
    },
    "bulk_deal_targets": {
        "name": "Recent bulk-deal targets",
        "description": "Stocks that saw a bulk deal ≥ ₹5cr in last 7 days",
        "sql": """
            SELECT ticker, company, SUM(value_cr) AS total_cr, COUNT(*) AS deal_count
            FROM bulk_deals
            WHERE deal_date >= date('now', '-7 days') AND value_cr >= 5
            GROUP BY ticker ORDER BY total_cr DESC LIMIT 50
        """,
    },
    "fo_unusual_24h": {
        "name": "Unusual options activity (24h)",
        "description": "Tickers with 2σ+ OI build-up near spot in last 24h",
        "sql": """
            SELECT ticker, MAX(signal_score) AS score,
                   COUNT(*) AS strikes_flagged, MAX(fetched_at) AS last_seen
            FROM fo_unusual
            WHERE fetched_at >= datetime('now', '-24 hours')
            GROUP BY ticker ORDER BY score DESC LIMIT 50
        """,
    },
    "filing_grade": {
        "name": "Filing-grade events (Tier-1)",
        "description": "Events confirmed by direct exchange/regulator filings",
        "sql": """
            SELECT s.ticker, s.company, s.alpha_score, s.event_type, s.created_at, s.headline
            FROM signals s
            WHERE s.created_at >= datetime('now', '-7 days')
              AND (s.source LIKE 'BSE%' OR s.source LIKE 'NSE%' OR s.source LIKE 'SEBI%' OR s.source LIKE 'RBI%')
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
def geo_india_events():
    """Tag recent events with Indian state coordinates by matching state names
    in title/summary. Front-end map.html plots these as pins."""
    db = _get_db()
    cur = db.conn.cursor()
    try:
        cur.execute(
            """SELECT event_id, title, summary, source, link, companies, sentiment, magnitude, created_at
               FROM events WHERE created_at >= datetime('now', '-3 days') ORDER BY created_at DESC LIMIT 500"""
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        rows = []

    out = []
    for r in rows:
        text = (((r.get("title") or "") + " " + (r.get("summary") or ""))).lower()
        for state, (lat, lng) in INDIA_STATES.items():
            if state in text:
                out.append({
                    "event_id": r["event_id"],
                    "state": state.title(),
                    "lat": lat, "lng": lng,
                    "title": r["title"],
                    "sentiment": r.get("sentiment"),
                    "magnitude": r.get("magnitude"),
                    "source": r.get("source"),
                    "link": r.get("link"),
                    "as_of": r.get("created_at"),
                })
                break
    return jsonify({"success": True, "data": out})


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
    try:
        min_score = float(request.args.get("min_score", "30"))
    except Exception:
        min_score = 30.0

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


# ============ REGISTRATION ==================================================

def register(app, get_db: Callable):
    global _get_db
    _get_db = get_db
    if "api_v3" not in app.blueprints:
        app.register_blueprint(bp)
