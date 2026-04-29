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
    # `?exclude_social=1` filters out reddit/telegram social posts so the
    # home page Live Wire can show curated news (RSS + Google News + filings)
    # without being drowned in social volume.
    exclude_social = (request.args.get("exclude_social") or "").lower() in ("1", "true", "yes")
    # `?kinds=earnings,policy,order_win` whitelists event types.
    kinds_filter = (request.args.get("kinds") or "").strip()
    kinds_set = {k.strip().lower() for k in kinds_filter.split(",") if k.strip()} if kinds_filter else None

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

    # 2) DB events table — widen window when filtering social out, since
    # curated news has lower volume and a 2h window often returns nothing.
    if db:
        try:
            cur = db.conn.cursor()
            window = "-24 hours" if (exclude_social or kinds_set) else "-2 hours"
            # NOTE: Order by `created_at` (always SQLite-native ISO) rather than
            # `published_at`, which is sometimes RFC822 ("Wed, 02 Jul GMT") for
            # Google News events. SQLite's datetime() returns NULL on RFC822,
            # which previously pushed all curated news to the bottom while
            # ISO-formatted social posts bubbled to the top.
            q = f"""
                SELECT event_id, title, summary, source, link, event_type, sentiment,
                       sentiment_confidence, magnitude, impact_score, companies,
                       published_at, created_at
                FROM events
                WHERE created_at >= datetime('now', '{window}')
                ORDER BY created_at DESC
                LIMIT ?
            """
            cur.execute(q, (limit * 4 if (exclude_social or kinds_set) else limit * 2,))
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
                    f"""SELECT s.event_id, s.alpha_score,
                               COALESCE(ms.band, 'clean') AS forensic_band
                        FROM signals s
                        LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
                        WHERE s.event_id IN ({placeholders})""",
                    event_ids,
                )
                meta_map = {}
                for row in cur.fetchall():
                    meta_map[row["event_id"]] = {
                        "alpha_score": row["alpha_score"],
                        "forensic_band": row["forensic_band"],
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

    # 6) source/kind filters
    def _is_social(it: Dict) -> bool:
        if (it.get("event_type") or "").lower() == "social":
            return True
        feed = str(it.get("feed") or it.get("source") or "").lower()
        return feed.startswith("reddit") or feed.startswith("telegram") or "/r/" in feed
    if exclude_social:
        items = [it for it in items if not _is_social(it)]
    if kinds_set:
        items = [it for it in items if (it.get("event_type") or "").lower() in kinds_set]

    # 7) sort: prefer recent + high-tier. We multiply age in hours by a tier
    # penalty so a fresh ET/Mint headline beats a fresher reddit post.
    def _sort_key(x):
        age = x.get("age_hours") or 999.0
        tier = x.get("tier") or 4
        # Tier 1 (filings) = 1.0×, Tier 2 (press) = 1.4×, Tier 3 = 2.5×, Tier 4 = 5×
        penalty = {1: 1.0, 2: 1.4, 3: 2.5, 4: 5.0}.get(tier, 5.0)
        return age * penalty
    items.sort(key=_sort_key)

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
        try:
            from api import audit_log as _audit
            _audit('alert_create', f'alert:{new_id}',
                   {'kind': kind, 'ticker': ticker, 'params': params, 'cooldown': cooldown})
        except Exception:
            pass
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/preview", methods=["POST"])
def smart_alerts_preview():
    """Replay a candidate alert rule against historical signals/events and
    return what would have fired in the last N days.

    Lets users dry-run a rule before saving it (and tunes parameter defaults).
    Body: { "kind": ..., "ticker": optional, "params": {...}, "days": 30 }
    Returns: { matches: [{ts, ticker, payload}, ...], count, days }
    """
    db = _get_db()
    try:
        from smart_alerts import SUPPORTED_KINDS  # noqa: F401  (validate kind)
        body = request.get_json(force=True) or {}
        kind = body.get("kind")
        if kind not in SUPPORTED_KINDS:
            return jsonify({"success": False, "error": f"kind must be one of {SUPPORTED_KINDS}"}), 400
        ticker = (body.get("ticker") or "").upper() or None
        params = body.get("params") or {}
        try:
            days = max(1, min(90, int(body.get("days", 30))))
        except (TypeError, ValueError):
            days = 30

        cur = db.conn.cursor()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        matches: List[Dict] = []

        # Each kind has a different historical replay strategy. Keep this
        # narrow to avoid expensive table scans — preview is a UX nicety,
        # not a backtester.
        if kind == "signal_alpha":
            min_alpha = float(params.get("min_alpha", 70))
            sentiment = (params.get("sentiment") or "").strip().lower() or None
            sql = (f"SELECT created_at, ticker, alpha_score, sentiment, headline "
                   f"FROM signals WHERE alpha_score >= {p} "
                   f"AND created_at >= datetime('now', '-{days} days')")
            args = [min_alpha]
            if ticker:
                sql += f" AND ticker = {p}"; args.append(ticker)
            if sentiment:
                sql += f" AND sentiment = {p}"; args.append(sentiment)
            sql += " ORDER BY created_at DESC LIMIT 100"
            cur.execute(sql, tuple(args))
            for r in cur.fetchall():
                row = dict(r) if hasattr(r, "keys") else {
                    "created_at": r[0], "ticker": r[1], "alpha_score": r[2],
                    "sentiment": r[3], "headline": r[4],
                }
                matches.append({"ts": str(row.get("created_at")),
                                "ticker": row.get("ticker"),
                                "payload": {"alpha": row.get("alpha_score"),
                                            "sentiment": row.get("sentiment"),
                                            "headline": row.get("headline")}})
        elif kind == "bulk_deal":
            min_cr = float(params.get("min_value_cr", 5))
            try:
                sql = (f"SELECT deal_date, ticker, deal_value_cr, deal_price, buyer_seller "
                       f"FROM bulk_deals WHERE deal_value_cr >= {p} "
                       f"AND deal_date >= datetime('now', '-{days} days')")
                args = [min_cr]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY deal_date DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "deal_date": r[0], "ticker": r[1], "deal_value_cr": r[2],
                        "deal_price": r[3], "buyer_seller": r[4],
                    }
                    matches.append({"ts": str(row.get("deal_date")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"value_cr": row.get("deal_value_cr"),
                                                "price": row.get("deal_price"),
                                                "party": row.get("buyer_seller")}})
            except Exception:
                pass  # bulk_deals table may not exist in dev DBs
        elif kind == "promoter_pledge":
            min_delta = float(params.get("min_delta_pct", 5))
            try:
                sql = (f"SELECT event_date, ticker, pct_before, pct_after, person_name "
                       f"FROM promoter_events WHERE event_type = 'pledge' "
                       f"AND ABS(COALESCE(pct_after,0) - COALESCE(pct_before,0)) >= {p} "
                       f"AND event_date >= datetime('now', '-{days} days')")
                args = [min_delta]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY event_date DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "event_date": r[0], "ticker": r[1], "pct_before": r[2],
                        "pct_after": r[3], "person_name": r[4],
                    }
                    matches.append({"ts": str(row.get("event_date")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"before": row.get("pct_before"),
                                                "after": row.get("pct_after"),
                                                "person": row.get("person_name")}})
            except Exception:
                pass
        elif kind == "forensic_flip":
            to_band = params.get("to_band", "likely_manipulated")
            try:
                sql = (f"SELECT as_of, ticker, band, score FROM forensic_flags "
                       f"WHERE band = {p} AND as_of >= datetime('now', '-{days} days')")
                args = [to_band]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY as_of DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "as_of": r[0], "ticker": r[1], "band": r[2], "score": r[3],
                    }
                    matches.append({"ts": str(row.get("as_of")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"band": row.get("band"), "score": row.get("score")}})
            except Exception:
                pass
        else:
            # volume_spike / news_velocity require live time-series the preview
            # doesn't reconstruct. Surface this honestly rather than fabricate.
            return jsonify({"success": True, "data": {
                "matches": [], "count": 0, "days": days,
                "note": f"Preview not available for '{kind}' (requires live time-series replay).",
            }})

        return jsonify({"success": True, "data": {
            "matches": matches[:50], "count": len(matches), "days": days,
        }})
    except Exception as e:
        logger.warning(f"alerts preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/<int:alert_id>", methods=["DELETE"])
def smart_alerts_delete(alert_id: int):
    db = _get_db()
    try:
        from smart_alerts import delete_alert
        ok = delete_alert(db, _user_id(), alert_id)
        if ok:
            try:
                from api import audit_log as _audit
                _audit('alert_delete', f'alert:{alert_id}', None)
            except Exception:
                pass
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
        if ok:
            try:
                from api import audit_log as _audit
                _audit('alert_update', f'alert:{alert_id}', data)
            except Exception:
                pass
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


# ---- /api/movers/spike — universe-wide intraday price-spike scanner ---------
# Catches Mobikwik-like ±10% moves that have NO matching news event yet.
# Without this scanner the front page only shows what news ingestion captured;
# fast-moving small/mid caps slip through silently.

_MOVERS_CACHE: Dict = {"_ts": 0.0, "payload": []}
_MOVERS_TTL_SECS = 300  # 5 min — yfinance bulk pull is slow


def _broad_universe() -> List[str]:
    """Build a broad NSE coverage list. Pulls from scraper config (large/mid
    rotation) plus an explicit small-cap recent-listings list so names like
    MOBIKWIK, ZAGGLE, etc. aren't invisible just because they're new."""
    universe: set = set()
    try:
        # Lazy import — avoids blowing up if the scraper package is missing
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "scraper"))
        from config import (
            MONITORED_STOCKS, TICKER_NEWS_ROTATION, LARGE_CAP, MID_CAP, SMALL_CAP,
        )
        universe.update(MONITORED_STOCKS or [])
        universe.update(TICKER_NEWS_ROTATION or [])
        universe.update(LARGE_CAP or [])
        universe.update(MID_CAP or [])
        universe.update(SMALL_CAP or [])
    except Exception as exc:
        logger.debug(f"movers: scraper config not available: {exc}")
    # Recent listings + niche small/mid caps the scraper rotation may not cover
    extras = [
        # Fintech / payments / new-economy (Mobikwik et al.)
        'MOBIKWIK', 'ZAGGLE', 'CARTRADE', 'PAYTM', 'POLICYBZR', 'CAMS', 'RAILTEL',
        'NYKAA', 'EASEMYTRIP', 'IXIGO', 'DELHIVERY', 'LICI', 'BLACKBOX',
        # Other niche small caps with frequent moves
        'NEULAND', 'BLUESTARCO', 'ELECON', 'ELECTCAST', 'CAPLIPOINT', 'ROUTE',
        'INTELLECT', 'ZENSARTECH', 'SUBROS', 'CARBORUNIV', 'TIMETECHNO',
        # Defence / capex spikes
        'BHARATELE', 'AVANTIFEED', 'KIRLOSBROS', 'GUFICBIO', 'SBICARD',
        # Recently active midcaps
        'IDEAFORGE', 'HAPPSTMNDS', 'TBOTEK', 'SIGNATURE', 'YATHARTH', 'SENCO',
    ]
    universe.update(extras)
    return sorted(universe)


def _ticker_sector_map() -> Dict[str, str]:
    """Return cached TICKER -> SECTOR map from scraper.stock_universe.
    Empty dict if scraper package isn't importable."""
    cache = getattr(_ticker_sector_map, "_cache", None)
    if cache is not None:
        return cache
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "scraper"))
        from stock_universe import TICKER_SECTOR_OVERRIDE  # type: ignore
        cache = dict(TICKER_SECTOR_OVERRIDE)
    except Exception as exc:
        logger.debug(f"sector map unavailable: {exc}")
        cache = {}
    _ticker_sector_map._cache = cache  # type: ignore[attr-defined]
    return cache


def _recent_ticker_news(tickers: List[str], hours: int = 24) -> Dict[str, Dict]:
    """For each ticker, return the freshest matching event row from the DB.
    Output: {TICKER: {title, link, source, published_at}}.

    Used to attach a corroborating headline to mover items so the UI can
    explain why a stock is spiking instead of just "X jumps Y%".
    """
    out: Dict[str, Dict] = {}
    if not tickers:
        return out
    db = _get_db() if _get_db else None
    if not db:
        return out
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT title, link, source, published_at, created_at, companies, sentiment
                FROM events
                WHERE created_at >= datetime('now', '-{int(hours)} hours')
                ORDER BY created_at DESC
                LIMIT 600""")
        wanted = {t.upper() for t in tickers}
        for row in cur.fetchall():
            companies = row["companies"]
            if isinstance(companies, str):
                try:
                    companies = json.loads(companies)
                except Exception:
                    companies = [c.strip() for c in companies.split(",") if c.strip()]
            for c in (companies or []):
                cu = (c or "").upper()
                if cu in wanted and cu not in out:
                    out[cu] = {
                        "title": row["title"],
                        "link": row["link"],
                        "source": row["source"],
                        "published_at": row["published_at"] or row["created_at"],
                        "sentiment": row["sentiment"],
                    }
            if len(out) == len(wanted):
                break
    except Exception as e:
        logger.debug(f"recent_ticker_news lookup failed: {e}")
    return out


def _enrich_movers(raw: List[Dict]) -> List[Dict]:
    """Attach sector, alpha_score, reasoning bullets, and sympathy peers to
    each mover row. Mutates and returns the list.

    Alpha score (0-100) =
        magnitude (40) + volume confirmation (30) + news corroboration (20)
        + sector momentum (10).
    """
    if not raw:
        return raw
    sec_map = _ticker_sector_map()
    tickers = [r.get("ticker") for r in raw if r.get("ticker")]
    news_map = _recent_ticker_news(tickers, hours=24)

    # Sector aggregates: how many movers in each sector and net direction
    sector_movers: Dict[str, List[Dict]] = {}
    for r in raw:
        s = sec_map.get((r.get("ticker") or "").upper())
        r["sector"] = s
        if s:
            sector_movers.setdefault(s, []).append(r)

    for r in raw:
        pct = float(r.get("pct_change") or 0)
        vol_z = r.get("vol_multiple")
        sector = r.get("sector")
        ticker = r.get("ticker")
        direction = "up" if pct >= 0 else "down"

        # ---- alpha score components ----
        s_move = min(40.0, abs(pct) * 4.0)         # 10% → 40
        s_vol = 0.0
        if isinstance(vol_z, (int, float)) and vol_z > 0:
            s_vol = min(30.0, max(0.0, (vol_z - 1.0) * 15.0))  # 3× → 30
        news = news_map.get((ticker or "").upper())
        s_news = 20.0 if news else 0.0
        # Sector momentum: at least 2 OTHER movers in same direction in this sector
        sector_peers_same_dir: List[Dict] = []
        if sector:
            for other in sector_movers.get(sector, []):
                if other is r:
                    continue
                opct = float(other.get("pct_change") or 0)
                if (opct >= 0) == (pct >= 0):
                    sector_peers_same_dir.append(other)
        s_sector = 10.0 if len(sector_peers_same_dir) >= 2 else (5.0 if len(sector_peers_same_dir) == 1 else 0.0)

        alpha = round(s_move + s_vol + s_news + s_sector, 1)
        r["alpha_score"] = alpha
        r["alpha_components"] = {
            "magnitude": round(s_move, 1),
            "volume": round(s_vol, 1),
            "news": round(s_news, 1),
            "sector": round(s_sector, 1),
        }
        r["confidence"] = "high" if alpha >= 65 else ("medium" if alpha >= 40 else "low")

        # ---- reasoning bullets ----
        bullets: List[Dict] = []
        sign = "+" if pct >= 0 else ""
        bullets.append({
            "kind": "move",
            "label": f"Spot move {sign}{pct:.2f}% intraday (last close ₹{r.get('price')})",
        })
        if isinstance(vol_z, (int, float)) and vol_z > 0:
            if vol_z >= 2.5:
                bullets.append({"kind": "volume", "label": f"Volume {vol_z:.1f}× the recent average — strongly confirms the move"})
            elif vol_z >= 1.5:
                bullets.append({"kind": "volume", "label": f"Volume {vol_z:.1f}× average — moderately confirmed"})
            else:
                bullets.append({"kind": "volume", "label": f"Volume only {vol_z:.1f}× average — thin participation, weaker signal"})
        if news:
            headline = (news.get("title") or "")[:140]
            src = news.get("source") or "news"
            bullets.append({
                "kind": "news",
                "label": f"Headline ({src}): {headline}",
                "link": news.get("link"),
            })
        else:
            bullets.append({
                "kind": "no_news",
                "label": "No corroborating headline in last 24h — likely technical/momentum or unannounced flow. Trade with tighter stops.",
            })
        if sector and sector_peers_same_dir:
            peer_names = ", ".join(p.get("ticker") for p in sector_peers_same_dir[:3])
            bullets.append({
                "kind": "sector",
                "label": f"Sector context: {sector} sees {len(sector_peers_same_dir)+1} names moving {direction} today (incl. {peer_names})",
            })
        elif sector:
            bullets.append({
                "kind": "sector",
                "label": f"Idiosyncratic — only {ticker} moving in {sector} today, not a sector trade",
            })
        r["reasoning"] = bullets

        # ---- sympathy / suggested stocks ----
        # Prefer same-direction peers from the live scan; fall back to sector names
        suggested: List[Dict] = []
        for p in sector_peers_same_dir[:4]:
            ppct = float(p.get("pct_change") or 0)
            psign = "+" if ppct >= 0 else ""
            suggested.append({
                "ticker": p.get("ticker"),
                "pct_change": ppct,
                "reason": f"Same sector, also {psign}{ppct:.1f}% today",
            })
        if not suggested and sector:
            # cold sector — pick a couple of well-known peers from the same sector
            sector_universe = [t for t, s in sec_map.items() if s == sector and t != ticker]
            for t in sector_universe[:3]:
                suggested.append({
                    "ticker": t,
                    "pct_change": None,
                    "reason": f"{sector} peer — watch for sympathy",
                })
        r["suggested"] = suggested

    return raw


@bp.route("/api/movers/spike", methods=["GET"])
def movers_spike():
    """Return stocks across the broad NSE universe with abnormal intraday moves.

    A "mover" is any stock whose latest close vs prior close pct change exceeds
    `min_pct` (default 5%). This catches small/mid-cap surges (e.g. Mobikwik
    +15%) that the news pipeline missed because no source indexed them.

    Each row is enriched with: sector, alpha_score (0-100), reasoning bullets,
    and suggested sympathy stocks — so the UI can explain *why* a stock is
    spiking and *what to watch next*, instead of just "X jumps Y%".

    Cached 5 min — yfinance bulk download for 300+ tickers is slow (~6-12s).

    Query params:
        min_pct: float, default 5.0
        limit: int, default 25 (cap to avoid flooding the UI)
    """
    try:
        min_pct = float(request.args.get("min_pct", 5.0))
    except (TypeError, ValueError):
        min_pct = 5.0
    try:
        limit = max(5, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25

    now = time.time()
    if (now - _MOVERS_CACHE["_ts"]) < _MOVERS_TTL_SECS and _MOVERS_CACHE.get("payload"):
        cached = [m for m in _MOVERS_CACHE["payload"] if abs(m.get("pct_change", 0)) >= min_pct]
        return jsonify({"success": True, "data": cached[:limit], "cached": True})

    out: List[Dict] = []
    try:
        import yfinance as yf
        universe = _broad_universe()
        # yfinance NSE suffix
        symbols = [f"{t}.NS" for t in universe]
        # Bulk download in 2-day window so we have prev-close + latest
        data = yf.download(symbols, period="3d", interval="1d",
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=False)

        for sym, ticker in zip(symbols, universe):
            try:
                df = data[sym] if len(symbols) > 1 else data
                if df is None or df.empty:
                    continue
                closes = df["Close"].dropna()
                vols = df["Volume"].dropna() if "Volume" in df.columns else None
                if len(closes) < 2:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                if not prev:
                    continue
                pct = (last - prev) / prev * 100.0
                if abs(pct) < min_pct:
                    continue
                # Pull avg-volume for a z-score-ish signal alongside the move
                vol_z = None
                if vols is not None and len(vols) >= 2:
                    try:
                        latest_vol = float(vols.iloc[-1])
                        avg_vol = float(vols.iloc[:-1].mean()) or 1.0
                        vol_z = round(latest_vol / avg_vol, 2)
                    except Exception:
                        pass
                out.append({
                    "ticker": ticker,
                    "price": round(last, 2),
                    "pct_change": round(pct, 2),
                    "vol_multiple": vol_z,
                    "as_of": datetime.utcnow().isoformat() + "Z",
                })
            except Exception:
                continue
    except Exception as exc:
        logger.warning(f"movers/spike scan failed: {exc}")
        return jsonify({"success": False, "error": str(exc), "data": []}), 503

    # Sort by absolute move desc — biggest signals first
    out.sort(key=lambda r: abs(r.get("pct_change", 0)), reverse=True)
    # Enrich BEFORE caching — alpha_score then becomes the primary sort key so
    # confirmed (high-vol + news + sector-wide) moves bubble above noise.
    try:
        _enrich_movers(out)
        out.sort(key=lambda r: r.get("alpha_score", 0), reverse=True)
    except Exception as exc:
        logger.warning(f"movers/spike enrichment failed: {exc}")
    _MOVERS_CACHE["_ts"] = now
    _MOVERS_CACHE["payload"] = out
    filtered = [m for m in out if abs(m.get("pct_change", 0)) >= min_pct]
    return jsonify({"success": True, "data": filtered[:limit], "cached": False,
                    "scanned": len(out), "as_of": datetime.utcnow().isoformat() + "Z"})


# ---- registration -----------------------------------------------------------

def register(app, get_db: Callable, scraper_status_ref: Optional[Dict] = None):
    """Mount onto a Flask app. Idempotent."""
    global _get_db, _scraper_status, _started_at
    _get_db = get_db
    _scraper_status = scraper_status_ref or {}
    _started_at = time.time()
    if "api_v2" not in app.blueprints:
        app.register_blueprint(bp)
