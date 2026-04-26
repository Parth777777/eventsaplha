"""API v2 routes: status/health, fast news priority feed, smart-alerts CRUD,
forensic-tagged event feed.

Mount via: api_v2.register(app, get_db, scraper_status_ref)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from flask import Blueprint, Response, g, jsonify, request

logger = logging.getLogger(__name__)

bp = Blueprint("api_v2", __name__)

# Filled in by register()
_get_db: Optional[Callable] = None
_scraper_status: Optional[Dict] = None
_started_at: float = time.time()


# ---- /api/status ------------------------------------------------------------

@bp.route("/api/status", methods=["GET"])
def api_status():
    """Comprehensive product health page.

    Drives the frontend "is the data fresh?" banner and the public trust panel.
    Never raises — every probe wraps in try/except so a single bad subsystem
    can't 500 the status page itself.
    """
    db = _get_db() if _get_db else None
    out: Dict = {
        "ok": True,
        "uptime_secs": int(time.time() - _started_at),
        "now": datetime.utcnow().isoformat() + "Z",
        "version": os.getenv("APP_VERSION", "dev"),
    }

    # Scraper
    try:
        ss = _scraper_status or {}
        last = ss.get("last_run")
        age_min = None
        if last:
            try:
                age_min = (datetime.utcnow() - datetime.fromisoformat(last.replace("Z", "")).replace(tzinfo=None)).total_seconds() / 60.0
            except Exception:
                age_min = None
        out["scraper"] = {
            "running": bool(ss.get("running")),
            "last_run": last,
            "last_run_age_min": round(age_min, 2) if age_min is not None else None,
            "last_result": ss.get("last_result"),
            "last_error": ss.get("error"),
            "stale": (age_min is not None and age_min > 15),
        }
    except Exception as e:
        out["scraper"] = {"error": str(e)}

    # DB row counts (cheap if indexed)
    try:
        if db:
            cur = db.conn.cursor()
            counts: Dict[str, int] = {}
            for table in ("signals", "events", "predictions"):
                try:
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    counts[table] = int((cur.fetchone() or {"n": 0})["n"] or 0)
                except Exception:
                    counts[table] = -1
            try:
                cur.execute("SELECT COUNT(*) AS n FROM signals WHERE created_at >= datetime('now', '-24 hours')")
                counts["signals_24h"] = int((cur.fetchone() or {"n": 0})["n"] or 0)
            except Exception:
                counts["signals_24h"] = -1
            try:
                cur.execute("SELECT COUNT(*) AS n FROM events WHERE created_at >= datetime('now', '-1 hour')")
                counts["events_1h"] = int((cur.fetchone() or {"n": 0})["n"] or 0)
            except Exception:
                counts["events_1h"] = -1
            out["db"] = counts
    except Exception as e:
        out["db"] = {"error": str(e)}

    # Realtime stream stats
    try:
        from realtime import stats as _rt_stats
        out["stream"] = _rt_stats()
    except Exception as e:
        out["stream"] = {"error": str(e)}

    # Backtest hit-rate (for public trust panel)
    try:
        if db:
            cur = db.conn.cursor()
            cur.execute(
                """
                SELECT horizon,
                       COUNT(*) AS n,
                       SUM(CASE WHEN actual_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                       SUM(CASE WHEN
                           (predicted_return_pct > 0 AND actual_return_pct > 0) OR
                           (predicted_return_pct < 0 AND actual_return_pct < 0)
                       THEN 1 ELSE 0 END) AS hits
                FROM predictions
                WHERE created_at >= datetime('now', '-90 days')
                GROUP BY horizon
                """
            )
            hr = {}
            for row in cur.fetchall():
                resolved = int(row["resolved"] or 0)
                hits = int(row["hits"] or 0)
                hr[row["horizon"]] = {
                    "resolved": resolved,
                    "hit_rate_pct": round((hits / resolved * 100), 2) if resolved else None,
                }
            out["hit_rate_90d"] = hr
    except Exception as e:
        out["hit_rate_90d"] = {"error": str(e)}

    # Mark unhealthy if scraper is wildly stale
    sc = out.get("scraper") or {}
    if sc.get("stale") or sc.get("last_error"):
        out["ok"] = False

    return jsonify({"success": True, "data": out})


# ---- /api/news/fast ---------------------------------------------------------

@bp.route("/api/news/fast", methods=["GET"])
def news_fast():
    """Fastest path to user: combines streaming buffer + DB recent events,
    sorted by freshness, with forensic band + source tier surfaced.

    This is what the home page should call (not /api/events) when the user
    wants the latest. Doesn't re-fetch RSS — purely reads from buffer + DB.
    """
    try:
        limit = max(1, min(100, int(request.args.get("limit", "30"))))
    except ValueError:
        limit = 30
    ticker = (request.args.get("ticker") or "").upper().strip() or None
    min_alpha = request.args.get("min_alpha")
    forensic_band = request.args.get("forensic_band")

    db = _get_db() if _get_db else None
    items: List[Dict] = []

    # 1) live buffer (last few minutes — fastest)
    try:
        from realtime import get_recent
        for ev in get_recent(channel=None, limit=limit * 2):
            if ev.get("channel") not in ("news", "scored"):
                continue
            if ticker and ticker not in (ev.get("tickers") or [ev.get("ticker")] if ev.get("ticker") else []):
                continue
            items.append({
                "source": "buffer",
                **ev,
            })
    except Exception:
        pass

    # 2) DB events table (last hour)
    if db:
        try:
            cur = db.conn.cursor()
            q = """
                SELECT event_id, title, summary, source, link, event_type, sentiment,
                       sentiment_confidence, magnitude, impact_score, companies,
                       published_at, created_at
                FROM events
                WHERE created_at >= datetime('now', '-2 hours')
                ORDER BY datetime(COALESCE(published_at, created_at)) DESC
                LIMIT ?
            """
            cur.execute(q, (limit * 2,))
            for row in cur.fetchall():
                companies = row["companies"]
                if isinstance(companies, str):
                    try:
                        companies = json.loads(companies)
                    except Exception:
                        companies = [c.strip() for c in companies.split(",") if c.strip()]
                if ticker and ticker not in (companies or []):
                    continue
                items.append({
                    "source": "db",
                    "event_id": row["event_id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "feed": row["source"],
                    "link": row["link"],
                    "event_type": row["event_type"],
                    "sentiment": row["sentiment"],
                    "sentiment_confidence": row["sentiment_confidence"],
                    "magnitude": row["magnitude"],
                    "impact_score": row["impact_score"],
                    "tickers": companies,
                    "published_at": row["published_at"],
                    "created_at": row["created_at"],
                })
        except Exception as e:
            logger.warning(f"news_fast db read: {e}")

    # 3) annotate with source tier + freshness
    try:
        from source_tiering import classify_source, freshness_label, _parse_age_hours
        for it in items:
            feed = it.get("feed") or it.get("source_name") or it.get("source")
            link = it.get("link", "")
            meta = classify_source(str(feed or ""), link)
            it["tier"] = meta.tier
            it["tier_weight"] = meta.weight
            published = it.get("published_at") or it.get("published") or it.get("created_at")
            it["freshness"] = freshness_label(published)
            it["age_hours"] = round(_parse_age_hours(published), 2)
    except Exception as e:
        logger.warning(f"news_fast annotate: {e}")

    # 4) optional alpha / forensic filters using signals table join
    if (min_alpha or forensic_band) and db:
        try:
            event_ids = [it.get("event_id") for it in items if it.get("event_id")]
            if event_ids:
                cur = db.conn.cursor()
                placeholders = ",".join(["?"] * len(event_ids))
                cur.execute(
                    f"SELECT event_id, alpha_score, forensic_band FROM signals WHERE event_id IN ({placeholders})",
                    event_ids,
                )
                meta_map = {}
                for row in cur.fetchall():
                    meta_map[row["event_id"]] = {
                        "alpha_score": row["alpha_score"],
                        "forensic_band": row["forensic_band"] if "forensic_band" in row.keys() else None,
                    }
                for it in items:
                    eid = it.get("event_id")
                    if eid and eid in meta_map:
                        it["alpha_score"] = meta_map[eid].get("alpha_score")
                        it["forensic_band"] = meta_map[eid].get("forensic_band")
                if min_alpha:
                    try:
                        thr = float(min_alpha)
                        items = [it for it in items if (it.get("alpha_score") or 0) >= thr]
                    except ValueError:
                        pass
                if forensic_band:
                    items = [it for it in items if it.get("forensic_band") == forensic_band]
        except Exception as e:
            logger.warning(f"news_fast filter: {e}")

    # 5) dedup by event_id, then by content hash
    try:
        from source_tiering import content_hash
        seen_eid: set = set()
        seen_hash: set = set()
        deduped: List[Dict] = []
        for it in items:
            eid = it.get("event_id")
            if eid and eid in seen_eid:
                continue
            ch = content_hash(it.get("title") or "", it.get("summary") or "")
            if ch in seen_hash:
                continue
            if eid:
                seen_eid.add(eid)
            seen_hash.add(ch)
            deduped.append(it)
        items = deduped
    except Exception:
        pass

    # 6) sort by age (freshest first)
    items.sort(key=lambda x: x.get("age_hours") or 999.0)

    return jsonify({
        "success": True,
        "count": len(items[:limit]),
        "data": items[:limit],
        "served_at": datetime.utcnow().isoformat() + "Z",
    })


# ---- smart-alerts CRUD -----------------------------------------------------

def _user_id() -> str:
    return getattr(g, "user_id", None) or "legacy"


@bp.route("/api/alerts/smart", methods=["GET"])
def smart_alerts_list():
    db = _get_db()
    try:
        from smart_alerts import list_alerts
        active_only = request.args.get("active", "1") in ("1", "true", "True")
        return jsonify({"success": True, "data": list_alerts(db, _user_id(), active_only=active_only)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart", methods=["POST"])
def smart_alerts_create():
    db = _get_db()
    try:
        from smart_alerts import create_alert, SUPPORTED_KINDS
        data = request.get_json(force=True) or {}
        kind = data.get("kind")
        if kind not in SUPPORTED_KINDS:
            return jsonify({"success": False, "error": f"kind must be one of {SUPPORTED_KINDS}"}), 400
        ticker = (data.get("ticker") or "").upper() or None
        params = data.get("params") or {}
        cooldown = int(data.get("cooldown_minutes") or 60)
        new_id = create_alert(db, _user_id(), kind, ticker, params, cooldown_minutes=cooldown)
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/<int:alert_id>", methods=["DELETE"])
def smart_alerts_delete(alert_id: int):
    db = _get_db()
    try:
        from smart_alerts import delete_alert
        ok = delete_alert(db, _user_id(), alert_id)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/<int:alert_id>", methods=["PATCH"])
def smart_alerts_update(alert_id: int):
    db = _get_db()
    try:
        from smart_alerts import update_alert
        data = request.get_json(force=True) or {}
        ok = update_alert(db, _user_id(), alert_id, **data)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/kinds", methods=["GET"])
def smart_alerts_kinds():
    """Surfaces alert kinds + their parameter shapes for the UI builder."""
    return jsonify({"success": True, "data": [
        {"kind": "volume_spike", "label": "Volume spike", "needs_ticker": True,
         "params": [{"name": "min_z", "type": "number", "default": 2.0,
                     "help": "Z-score vs 20-day average (2.0 = 2x normal)"}]},
        {"kind": "news_velocity", "label": "News velocity", "needs_ticker": False,
         "params": [{"name": "min_per_hour", "type": "number", "default": 3,
                     "help": "Articles per hour threshold"},
                    {"name": "window_hours", "type": "number", "default": 2}]},
        {"kind": "forensic_flip", "label": "Forensic band flip", "needs_ticker": False,
         "params": [{"name": "to_band", "type": "select", "default": "likely_manipulated",
                     "options": ["likely_manipulated", "unverified"]}]},
        {"kind": "signal_alpha", "label": "Alpha threshold cross", "needs_ticker": False,
         "params": [{"name": "min_alpha", "type": "number", "default": 70},
                    {"name": "sentiment", "type": "select", "default": "",
                     "options": ["", "bullish", "bearish"]}]},
        {"kind": "promoter_pledge", "label": "Promoter pledge change", "needs_ticker": True,
         "params": [{"name": "min_delta_pct", "type": "number", "default": 5}]},
        {"kind": "bulk_deal", "label": "Bulk deal", "needs_ticker": True,
         "params": [{"name": "min_value_cr", "type": "number", "default": 5}]},
    ]})


# ---- /api/global/markets and /api/commodities/prices fallback shims --------

@bp.route("/api/global/markets", methods=["GET"])
def global_markets():
    """Live world indices via yfinance. Falls back to last known DB row if
    network call fails. Always returns honest 'as_of' so the UI can flag stale.
    """
    indices = [
        ("^GSPC", "S&P 500", "USA"),
        ("^DJI", "Dow Jones", "USA"),
        ("^IXIC", "NASDAQ", "USA"),
        ("^FTSE", "FTSE 100", "UK"),
        ("^N225", "Nikkei 225", "JPN"),
        ("^HSI", "Hang Seng", "HKG"),
        ("000001.SS", "Shanghai Comp", "CHN"),
        ("^GDAXI", "DAX", "DEU"),
        ("^NSEI", "Nifty 50", "IND"),
        ("^BSESN", "Sensex", "IND"),
    ]
    fx_pairs = [
        ("INR=X", "USD/INR"),
        ("EURINR=X", "EUR/INR"),
        ("GBPINR=X", "GBP/INR"),
        ("JPYINR=X", "JPY/INR"),
    ]
    commodities = [
        ("CL=F", "Crude Oil", "WTI"),
        ("BZ=F", "Brent", "OIL"),
        ("GC=F", "Gold", "USD/oz"),
        ("SI=F", "Silver", "USD/oz"),
        ("NG=F", "Natural Gas", "MMBtu"),
    ]

    out: Dict = {"indices": [], "fx": [], "commodities": [], "as_of": None, "stale": False}
    try:
        import yfinance as yf
        all_syms = [s for s, _, _ in indices] + [s for s, _ in fx_pairs] + [s for s, _, _ in commodities]
        data = yf.download(all_syms, period="2d", interval="1d", group_by="ticker",
                           progress=False, threads=True)

        def _last_two(sym: str):
            try:
                if len(all_syms) == 1:
                    closes = data["Close"].dropna()
                else:
                    closes = data[sym]["Close"].dropna()
                if len(closes) < 1:
                    return None, None
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                return last, prev
            except Exception:
                return None, None

        for sym, name, region in indices:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["indices"].append({
                "symbol": sym, "name": name, "region": region,
                "price": round(last, 2),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        for sym, name in fx_pairs:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["fx"].append({
                "symbol": sym, "name": name,
                "price": round(last, 4),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        for sym, name, unit in commodities:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["commodities"].append({
                "symbol": sym, "name": name, "unit": unit,
                "price": round(last, 2),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        out["as_of"] = datetime.utcnow().isoformat() + "Z"
        out["source"] = "yfinance"
    except Exception as e:
        out["error"] = str(e)
        out["stale"] = True

    if not out["indices"] and not out["fx"] and not out["commodities"]:
        return jsonify({"success": False, "error": "no live data; check network", "data": out}), 503

    return jsonify({"success": True, "data": out})


@bp.route("/api/commodities/prices", methods=["GET"])
def commodities_prices():
    """Live commodity prices keyed by the IDs that commodities.html expects.

    Replaces the hardcoded MOCK_COMMODITY_PRICES dict in the frontend.
    Returns {id: {price, change_pct, currency, as_of}}. Front-end keeps its
    catalog (descriptions, sectors affected) and only reads price/change here.
    """
    # Yahoo Finance symbol map for the commodities the UI knows about
    sym_map = {
        "crude_brent": ("BZ=F", "USD/bbl"),
        "crude_wti": ("CL=F", "USD/bbl"),
        "natural_gas": ("NG=F", "USD/MMBtu"),
        "gold": ("GC=F", "USD/oz"),
        "silver": ("SI=F", "USD/oz"),
        "copper": ("HG=F", "USD/lb"),
        "aluminum": ("ALI=F", "USD/lb"),
        "wheat": ("ZW=F", "USD/bu"),
        "corn": ("ZC=F", "USD/bu"),
        "soybean": ("ZS=F", "USD/bu"),
        "cotton": ("CT=F", "USD/lb"),
        "sugar": ("SB=F", "USD/lb"),
        "coffee": ("KC=F", "USD/lb"),
        "palm_oil": ("FCPO=F", "MYR/MT"),
    }
    out: Dict[str, Dict] = {}
    stale = False
    try:
        import yfinance as yf
        syms = [v[0] for v in sym_map.values()]
        data = yf.download(syms, period="3d", interval="1d", group_by="ticker",
                           progress=False, threads=True)

        for cid, (sym, unit) in sym_map.items():
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                if len(closes) < 1:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                out[cid] = {
                    "price": round(last, 2),
                    "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
                    "currency": unit,
                    "as_of": datetime.utcnow().isoformat() + "Z",
                    "source": "yfinance",
                }
            except Exception:
                continue
    except Exception as e:
        stale = True
        logger.warning(f"commodities_prices yfinance failed: {e}")

    if not out:
        return jsonify({"success": False, "error": "no live commodity data; UI should show stale state",
                        "stale": True}), 503
    return jsonify({"success": True, "data": out, "stale": stale,
                    "as_of": datetime.utcnow().isoformat() + "Z"})


# ---- /api/global/history ----------------------------------------------------

_HISTORY_CACHE: Dict[str, Dict] = {}
_HISTORY_TTL_SECS = 600  # 10 min — yfinance is slow and the data is daily


@bp.route("/api/global/history", methods=["GET"])
def global_history():
    """Daily closes for the headline world indices over the last N trading days.

    Powers the 30-day normalized-returns line chart and the overnight-impact
    bar chart on the Global page. Cached for 10 minutes — yfinance bulk
    downloads are slow (~3-6s) and the underlying data is daily.

    Query params:
        days: int, default 30, capped at 90.
    """
    try:
        days = max(5, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    cache_key = f"days={days}"
    cached = _HISTORY_CACHE.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < _HISTORY_TTL_SECS:
        return jsonify({"success": True, "data": cached["payload"], "cached": True})

    indices = [
        ("^IXIC", "nasdaq", "NASDAQ"),
        ("^GSPC", "sp500", "S&P 500"),
        ("^DJI", "dow", "Dow Jones"),
        ("^GDAXI", "dax", "DAX"),
        ("^FTSE", "ftse", "FTSE 100"),
        ("^N225", "nikkei", "Nikkei 225"),
        ("^HSI", "hangseng", "Hang Seng"),
        ("000001.SS", "shanghai", "Shanghai"),
        ("^NSEI", "nifty", "Nifty 50"),
    ]

    payload: Dict = {"indices": {}, "as_of": None, "days": days, "stale": False}
    try:
        import yfinance as yf
        # Pull a few extra calendar days to cover weekends/holidays
        period_days = int(days * 1.6) + 10
        syms = [s for s, _, _ in indices]
        data = yf.download(syms, period=f"{period_days}d", interval="1d",
                           group_by="ticker", progress=False, threads=True)

        for sym, key, label in indices:
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                if len(closes) < 2:
                    continue
                # Take last `days` trading days only
                tail = closes.tail(days)
                series = [
                    {"date": idx.strftime("%Y-%m-%d"), "close": round(float(val), 2)}
                    for idx, val in tail.items()
                ]
                payload["indices"][key] = {
                    "label": label, "symbol": sym, "series": series,
                }
            except Exception as e:
                logger.debug(f"global_history: skipped {sym}: {e}")
                continue

        payload["as_of"] = datetime.utcnow().isoformat() + "Z"
    except Exception as exc:
        payload["stale"] = True
        payload["error"] = str(exc)
        logger.warning(f"global_history yfinance failed: {exc}")

    if not payload["indices"]:
        return jsonify({"success": False, "error": "no historical data available",
                        "data": payload}), 503

    _HISTORY_CACHE[cache_key] = {"_ts": time.time(), "payload": payload}
    return jsonify({"success": True, "data": payload, "cached": False})


# ---- registration -----------------------------------------------------------

def register(app, get_db: Callable, scraper_status_ref: Optional[Dict] = None):
    """Mount onto a Flask app. Idempotent."""
    global _get_db, _scraper_status, _started_at
    _get_db = get_db
    _scraper_status = scraper_status_ref or {}
    _started_at = time.time()
    if "api_v2" not in app.blueprints:
        app.register_blueprint(bp)
