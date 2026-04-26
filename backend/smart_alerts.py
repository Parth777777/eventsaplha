"""Smart alert types: volume spike, news velocity, forensic-band flip, bulk deal,
promoter pledge change, signal threshold cross.

Storage: smart_alerts table (created on demand). Triggers run on each scraper
cycle and during enrichment. Hits are pushed via realtime.broadcast_alert and
optionally persisted into the existing notifications pipeline.

Schema (created idempotently):
    smart_alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,            -- volume_spike | news_velocity | forensic_flip
                                       -- | signal_alpha | promoter_pledge | bulk_deal
        ticker TEXT,                   -- optional, NULL = applies to whole market
        params TEXT,                   -- JSON: thresholds, windows, etc.
        active INTEGER DEFAULT 1,
        cooldown_minutes INTEGER DEFAULT 60,
        last_fired_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    smart_alert_hits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id INTEGER,
        fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
        payload TEXT
    )
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


SUPPORTED_KINDS = (
    "volume_spike",      # params: {min_z: 2.0, lookback_days: 20}
    "news_velocity",     # params: {min_per_hour: 3, window_hours: 2}
    "forensic_flip",     # params: {to_band: "likely_manipulated"}
    "signal_alpha",      # params: {min_alpha: 70, sentiment: "bullish"}
    "promoter_pledge",   # params: {min_delta_pct: 5}
    "bulk_deal",         # params: {min_value_cr: 5}
)


# ---- SCHEMA HELPERS ---------------------------------------------------------

def ensure_schema(db) -> None:
    """Create smart-alert tables if missing. Safe to call repeatedly."""
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT,
                params TEXT,
                active INTEGER DEFAULT 1,
                cooldown_minutes INTEGER DEFAULT 60,
                last_fired_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_alert_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER,
                fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
                payload TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_user ON smart_alerts(user_id, active)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_ticker ON smart_alerts(ticker, kind)")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"smart_alerts.ensure_schema failed: {e}")


# ---- CRUD -------------------------------------------------------------------

def create_alert(db, user_id: str, kind: str, ticker: Optional[str],
                 params: Dict[str, Any], cooldown_minutes: int = 60) -> Optional[int]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"Unsupported alert kind: {kind}")
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """
        INSERT INTO smart_alerts (user_id, kind, ticker, params, cooldown_minutes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, kind, (ticker or None), json.dumps(params or {}), int(cooldown_minutes)),
    )
    db.conn.commit()
    return cur.lastrowid


def list_alerts(db, user_id: str, active_only: bool = True) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    q = "SELECT id, user_id, kind, ticker, params, active, cooldown_minutes, last_fired_at, created_at FROM smart_alerts WHERE user_id=?"
    args: Tuple[Any, ...] = (user_id,)
    if active_only:
        q += " AND active=1"
    cur.execute(q + " ORDER BY id DESC", args)
    out = []
    for row in cur.fetchall():
        try:
            params = json.loads(row["params"]) if row["params"] else {}
        except Exception:
            params = {}
        out.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "kind": row["kind"],
            "ticker": row["ticker"],
            "params": params,
            "active": bool(row["active"]),
            "cooldown_minutes": row["cooldown_minutes"],
            "last_fired_at": row["last_fired_at"],
            "created_at": row["created_at"],
        })
    return out


def delete_alert(db, user_id: str, alert_id: int) -> bool:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute("DELETE FROM smart_alerts WHERE id=? AND user_id=?", (alert_id, user_id))
    db.conn.commit()
    return cur.rowcount > 0


def update_alert(db, user_id: str, alert_id: int, **fields) -> bool:
    if not fields:
        return False
    ensure_schema(db)
    allowed = {"active", "cooldown_minutes", "params", "ticker"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "params" and not isinstance(v, str):
            v = json.dumps(v)
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return False
    args.extend([alert_id, user_id])
    cur = db.conn.cursor()
    cur.execute(f"UPDATE smart_alerts SET {', '.join(sets)} WHERE id=? AND user_id=?", args)
    db.conn.commit()
    return cur.rowcount > 0


# ---- COOLDOWN GATE ----------------------------------------------------------

def _cooldown_ok(alert: Dict) -> bool:
    last = alert.get("last_fired_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return True
    cd = int(alert.get("cooldown_minutes") or 60)
    return datetime.utcnow() - last_dt.replace(tzinfo=None) >= timedelta(minutes=cd)


def _record_hit(db, alert_id: int, payload: Dict) -> None:
    cur = db.conn.cursor()
    cur.execute(
        "INSERT INTO smart_alert_hits (alert_id, payload) VALUES (?, ?)",
        (alert_id, json.dumps(payload, default=str)),
    )
    cur.execute("UPDATE smart_alerts SET last_fired_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), alert_id))
    db.conn.commit()


# ---- TRIGGER EVALUATORS -----------------------------------------------------

def _eval_volume_spike(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx must include: volume_data = {ticker: {z_score, current_volume, avg_volume_20d}}"""
    vd = ctx.get("volume_data") or {}
    ticker = alert["ticker"]
    if not ticker or ticker not in vd:
        return None
    info = vd[ticker]
    min_z = float(alert["params"].get("min_z", 2.0))
    z = float(info.get("z_score") or 0.0)
    if z >= min_z:
        return {
            "kind": "volume_spike",
            "ticker": ticker,
            "z_score": z,
            "current_volume": info.get("current_volume"),
            "avg_volume_20d": info.get("avg_volume_20d"),
            "headline": f"{ticker}: volume spike {z:.1f}x normal",
        }
    return None


def _eval_news_velocity(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx: news_velocity = {ticker_or_'_market': per_hour}"""
    nv = ctx.get("news_velocity") or {}
    ticker = alert["ticker"] or "_market"
    rate = float(nv.get(ticker) or 0.0)
    threshold = float(alert["params"].get("min_per_hour", 3))
    if rate >= threshold:
        return {
            "kind": "news_velocity",
            "ticker": alert["ticker"],
            "articles_per_hour": rate,
            "threshold": threshold,
            "headline": f"{alert['ticker'] or 'Market'}: news velocity {rate:.1f}/hr (threshold {threshold})",
        }
    return None


def _eval_forensic_flip(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx: forensic_flips = [{ticker, from_band, to_band, event_id}]"""
    flips = ctx.get("forensic_flips") or []
    target_band = alert["params"].get("to_band", "likely_manipulated")
    for flip in flips:
        if alert["ticker"] and flip.get("ticker") != alert["ticker"]:
            continue
        if flip.get("to_band") == target_band:
            return {
                "kind": "forensic_flip",
                "ticker": flip.get("ticker"),
                "from_band": flip.get("from_band"),
                "to_band": flip.get("to_band"),
                "event_id": flip.get("event_id"),
                "headline": f"{flip.get('ticker')}: forensic band → {flip.get('to_band')}",
            }
    return None


def _eval_signal_alpha(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx: signals = [{ticker, alpha_score, sentiment, ...}]"""
    signals = ctx.get("signals") or []
    min_alpha = float(alert["params"].get("min_alpha", 70))
    want_sent = alert["params"].get("sentiment")  # optional
    for s in signals:
        if alert["ticker"] and s.get("ticker") != alert["ticker"]:
            continue
        try:
            alpha = float(s.get("alpha_score") or 0)
        except (TypeError, ValueError):
            continue
        if alpha < min_alpha:
            continue
        if want_sent and s.get("sentiment") != want_sent:
            continue
        return {
            "kind": "signal_alpha",
            "ticker": s.get("ticker"),
            "alpha_score": alpha,
            "sentiment": s.get("sentiment"),
            "event_id": s.get("event_id"),
            "headline": f"{s.get('ticker')}: alpha {alpha:.0f} ({s.get('sentiment')})",
        }
    return None


def _eval_promoter_pledge(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx: pledge_changes = [{ticker, delta_pct, current_pct}]"""
    changes = ctx.get("pledge_changes") or []
    threshold = float(alert["params"].get("min_delta_pct", 5))
    for ch in changes:
        if alert["ticker"] and ch.get("ticker") != alert["ticker"]:
            continue
        try:
            delta = float(ch.get("delta_pct") or 0)
        except (TypeError, ValueError):
            continue
        if abs(delta) >= threshold:
            return {
                "kind": "promoter_pledge",
                "ticker": ch.get("ticker"),
                "delta_pct": delta,
                "current_pct": ch.get("current_pct"),
                "headline": f"{ch.get('ticker')}: pledge {'↑' if delta > 0 else '↓'}{abs(delta):.1f}pp",
            }
    return None


def _eval_bulk_deal(alert: Dict, ctx: Dict) -> Optional[Dict]:
    """ctx: bulk_deals = [{ticker, side, value_cr, party}]"""
    deals = ctx.get("bulk_deals") or []
    threshold = float(alert["params"].get("min_value_cr", 5))
    for d in deals:
        if alert["ticker"] and d.get("ticker") != alert["ticker"]:
            continue
        try:
            val = float(d.get("value_cr") or 0)
        except (TypeError, ValueError):
            continue
        if val >= threshold:
            return {
                "kind": "bulk_deal",
                "ticker": d.get("ticker"),
                "side": d.get("side"),
                "value_cr": val,
                "party": d.get("party"),
                "headline": f"{d.get('ticker')}: bulk {d.get('side')} ₹{val:.0f}cr by {d.get('party')}",
            }
    return None


_EVALUATORS: Dict[str, Callable[[Dict, Dict], Optional[Dict]]] = {
    "volume_spike": _eval_volume_spike,
    "news_velocity": _eval_news_velocity,
    "forensic_flip": _eval_forensic_flip,
    "signal_alpha": _eval_signal_alpha,
    "promoter_pledge": _eval_promoter_pledge,
    "bulk_deal": _eval_bulk_deal,
}


# ---- MAIN EVAL ENTRY --------------------------------------------------------

def evaluate(db, ctx: Dict, broadcast_fn: Optional[Callable[[Dict], None]] = None) -> List[Dict]:
    """Evaluate every active alert against ctx. Fires hits + returns them.

    ctx is built once per scraper cycle (or per request) and shared across
    all alerts for efficiency. Pass broadcast_fn to push to SSE/notifications.
    """
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute("SELECT id, user_id, kind, ticker, params, active, cooldown_minutes, last_fired_at FROM smart_alerts WHERE active=1")
    rows = cur.fetchall()

    fired: List[Dict] = []
    for r in rows:
        try:
            params = json.loads(r["params"] or "{}")
        except Exception:
            params = {}
        alert = {
            "id": r["id"],
            "user_id": r["user_id"],
            "kind": r["kind"],
            "ticker": r["ticker"],
            "params": params,
            "cooldown_minutes": r["cooldown_minutes"],
            "last_fired_at": r["last_fired_at"],
        }
        if not _cooldown_ok(alert):
            continue
        evaluator = _EVALUATORS.get(alert["kind"])
        if not evaluator:
            continue
        try:
            hit = evaluator(alert, ctx)
        except Exception as e:
            logger.warning(f"smart_alert evaluator {alert['kind']} failed: {e}")
            continue
        if not hit:
            continue
        hit["alert_id"] = alert["id"]
        hit["user_id"] = alert["user_id"]
        hit["fired_at"] = datetime.utcnow().isoformat()
        _record_hit(db, alert["id"], hit)
        fired.append(hit)
        if broadcast_fn:
            try:
                broadcast_fn(hit)
            except Exception as e:
                logger.warning(f"smart_alert broadcast failed: {e}")
    if fired:
        logger.info(f"smart_alerts: fired {len(fired)} hits")
    return fired


# ---- CONTEXT BUILDERS (best-effort, fail soft) ------------------------------

def build_context_from_signals(signals: List[Dict]) -> Dict:
    """Extract reusable context fields from a freshly-generated signals batch."""
    return {
        "signals": signals,
        # Volume / pledges / bulk-deals can be added by orchestrator_ext if available.
    }


def build_news_velocity(articles: List[Dict], window_hours: int = 2) -> Dict[str, float]:
    """Articles per hour, per ticker (and overall)."""
    if not articles:
        return {}
    cutoff = time.time() - window_hours * 3600
    counts: Dict[str, int] = {"_market": 0}
    for a in articles:
        try:
            from source_tiering import _parse_iso_timestamp
            ts = _parse_iso_timestamp(a.get("published") or a.get("timestamp") or "")
        except Exception:
            ts = time.time()
        if ts < cutoff:
            continue
        counts["_market"] = counts.get("_market", 0) + 1
        for t in (a.get("companies") or []):
            counts[t] = counts.get(t, 0) + 1
    span = max(0.5, window_hours)
    return {k: v / span for k, v in counts.items()}
